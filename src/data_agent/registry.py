"""会话注册表与共享状态。

本模块从 ``api.py`` 拆分而来，集中持有：

- ``SessionRecord`` / ``SessionRegistry``：会话内存状态与磁盘持久化。
- 进程级单例：``bootstrap_settings``、``session_storage``、``registry``、
  ``runtime_settings``、``analysis_slots`` 等，在 import 时初始化。
- 产物 / 会话载荷构造辅助函数：``_session_payload``、``_artifact_payload``、
  ``_elapsed_seconds``、``_result_payload``、``_history``、``_artifact_file``。
- 运行时配置：``_save_runtime_api_key``、``_effective_settings``。
- 请求模型与模块级常量（``API_VERSION`` 等）。

注意：``_artifact_file`` 通过延迟导入 ``data_agent.api`` 引用 ``api.registry``，
以便测试通过 ``monkeypatch.setattr(api, "registry", ...)`` 替换注册表时此处也能
感知到——其余单例同理（路由层均经 ``api.<name>`` 访问以兼容 monkeypatch）。
"""

from __future__ import annotations

import enum
import json
import logging
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from data_agent.agent import AnalysisResult
from data_agent.config import AgentSettings
from data_agent.credentials import get_saved_api_key, save_api_key
from data_agent.serialization import to_jsonable
from data_agent.storage import LocalSessionStorage, SessionStorage, build_session_storage
from data_agent.workspace import SUPPORTED_EXTENSIONS, DataWorkspace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API 版本管理：所有响应携带 X-API-Version 头，SSE 事件携带 v 字段，
# /api/version 端点返回当前版本与最低兼容客户端版本。URL 前缀保持 /api/...
# 不变，仅通过头部与载荷声明版本，便于前端做优雅降级与兼容性判断。
# ---------------------------------------------------------------------------
API_VERSION = "2"
API_VERSION_INT = 2
MIN_CLIENT_VERSION = "2"

# SSE 事件队列最大容量。worker 线程生产事件，事件循环消费推送给客户端。
# 客户端网络慢或浏览器卡顿时消费跟不上生产，无界队列会积累数万条事件（每条
# 携带 token 字符串），内存占用可达数百 MB。设 500 上限后，超限的
# thinking_chunk（过程信息，可丢）会被丢弃，report_chunk/tool_call（结果信息）保留。
SSE_QUEUE_MAXSIZE = 500


class SettingsUpdate(BaseModel):
    api_key: str | None = None
    thinking_enabled: bool = True
    reasoning_effort: str = Field(default="high", pattern="^(high|max)$")
    persist_key: bool = True


class AnalyzeRequest(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    # 断点续跑：提供 plan + completed_steps 时，跳过已完成步骤从中断处继续。
    resume_from: dict[str, Any] | None = None
    # 仅规划模式：为 True 时只执行到 plan_analysis 节点即停止，
    # 返回 plan/objective 等待用户审批。审批通过后前端用 resume_from
    # 注入已确认的计划重新发起 analyze/stream 启动执行阶段。
    plan_only: bool = False


class ChartEditRequest(BaseModel):
    """图表编辑请求：基于 .plotly.json 重新生成 HTML。

    支持的修改字段：
    - ``title``：新标题文本，写入 ``layout.title.text``。
    - ``color``：十六进制颜色（如 ``#245C55``），应用到所有 trace 的
      ``marker.color``。无 marker 的 trace 自动创建。
    至少提供一个字段；未提供的字段保持原值不变。
    """

    title: str | None = None
    color: str | None = None


def _save_runtime_api_key(value: str, persist_key: bool) -> bool:
    """Store the key in memory and try to persist it via the OS keyring.

    Returns True when persistence succeeded (or was not requested), False when
    the OS credential backend is unavailable so the key only lives in memory.
    """
    with runtime_settings_lock:
        runtime_settings["api_key"] = value
    if not persist_key:
        return True
    return save_api_key(value)


class SessionRecord:
    def __init__(self, workspace: DataWorkspace) -> None:
        self.workspace = workspace
        self.chat: list[dict[str, str]] = []
        self.last_result: AnalysisResult | None = None
        self.last_access = time.monotonic()
        self.run_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self._status_lock = threading.Lock()
        self._analysis_status = "idle"
        self.current_task = ""
        self.created_at = time.time()
        # 分析开始/结束的墙钟时间，用于前端显示"已耗时 / 总耗时"。
        # 使用 _status_lock 与 status 一起更新，避免读到一个新 status
        # 但旧 started_at 的瞬间状态。
        self.analysis_started_at: float | None = None
        self.analysis_completed_at: float | None = None
        # plan_only 模式产出的待审批计划。前端展示给用户确认后，
        # 通过 resume_from 注入该计划启动执行阶段；为 None 表示当前
        # 没有待审批计划（已完成或从未进入 plan_only 模式）。
        self.pending_plan: list[dict[str, Any]] | None = None
        # 用户自定义会话标题（覆盖默认 filename 展示）。空串/None 视为
        # 未设置，前端回退到 filename。PATCH /api/sessions/{id} 写入。
        self.title: str | None = None

    @property
    def analysis_status(self) -> str:
        with self._status_lock:
            return self._analysis_status

    @analysis_status.setter
    def analysis_status(self, value: str) -> None:
        with self._status_lock:
            self._analysis_status = value

    def set_running(self) -> None:
        with self._status_lock:
            self._analysis_status = "running"
            self.analysis_started_at = time.time()
            self.analysis_completed_at = None

    def set_finished(self, status: str) -> None:
        with self._status_lock:
            self._analysis_status = status
            self.analysis_completed_at = time.time()

    def set_awaiting_approval(self) -> None:
        """标记会话进入"等待计划审批"状态。

        plan_only 模式下规划阶段产出计划后调用本方法：
        - 状态切到 ``awaiting_approval``，``is_running()`` 返回 False，
          释放 run_lock 让用户可以重新发起执行请求。
        - 记录完成时间用于前端显示"规划耗时"。
        - 前端据此渲染审批面板，用户确认后用 resume_from 注入
          ``pending_plan`` 发起 analyze/stream 进入执行阶段。
        """
        with self._status_lock:
            self._analysis_status = "awaiting_approval"
            self.analysis_completed_at = time.time()

    def is_running(self) -> bool:
        """Whether an analysis is currently active (running or being cancelled)."""
        with self._status_lock:
            return self._analysis_status in {"running", "cancelling"}


class SessionRegistry:
    def __init__(
        self,
        runs_dir: Path,
        max_sessions: int,
        ttl_hours: float,
        storage: SessionStorage | None = None,
    ) -> None:
        self._items: dict[str, SessionRecord] = {}
        # 正在删除的 session_id 集合：delete() 在锁内标记后，锁外执行 rmtree。
        # 期间若 get() 尝试 _restore_locked，会被这个集合拦住，防止恢复出
        # 指向即将被删除目录的 record（H9 竞态修复）。
        self._deleting: set[str] = set()
        self._lock = threading.RLock()
        self.runs_dir = runs_dir.resolve()
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_hours * 3600
        self.storage = storage or LocalSessionStorage()

    def _prune_locked(self, reserve: int = 0) -> list[str]:
        now = time.monotonic()
        removed_ids: list[str] = []
        expired = [
            (session_id, record)
            for session_id, record in self._items.items()
            if now - record.last_access > self.ttl_seconds and not record.run_lock.locked()
        ]
        for session_id, record in expired:
            self._items.pop(session_id, None)
            removed_ids.append(session_id)
            try:
                record.workspace.cleanup()
            except OSError:
                pass
        allowed = max(self.max_sessions - reserve, 0)
        if len(self._items) <= allowed:
            return removed_ids
        candidates = sorted(
            ((record.last_access, session_id, record) for session_id, record in self._items.items() if not record.run_lock.locked()),
            key=lambda item: item[0],
        )
        for _, session_id, record in candidates[: max(0, len(self._items) - allowed)]:
            self._items.pop(session_id, None)
            removed_ids.append(session_id)
            try:
                record.workspace.cleanup()
            except OSError:
                pass
        return removed_ids

    def _cleanup_remote(self, session_ids: list[str]) -> None:
        for session_id in session_ids:
            try:
                self.storage.delete_session(session_id)
            except Exception:
                logger.exception("Session storage cleanup failed for %s", session_id)

    def _manifest_path(self, record: SessionRecord) -> Path:
        return record.workspace.root / "session.json"

    def _persist_locked(self, session_id: str, record: SessionRecord) -> None:
        record.workspace.save_checkpoint()
        last_result = None
        if record.last_result is not None:
            # trace 可能包含大段 LLM 输出和工具调用细节，多轮分析后会让
            # manifest 膨胀到 MB 级。只保留最近 20 条，足够恢复时展示上下文。
            trimmed_trace = list(record.last_result.trace or [])[-20:]
            last_result = to_jsonable(
                {
                    "response": record.last_result.response,
                    "trace": trimmed_trace,
                    "dataset_profile": record.last_result.dataset_profile,
                    "plan": record.last_result.plan,
                    "completed_steps": record.last_result.completed_steps,
                }
            )
        payload = {
            "id": session_id,
            "filename": record.workspace.source_path.name if record.workspace.source_path else "dataset",
            "chat": record.chat[-40:],
            "analysis_status": record.analysis_status,
            "analysis_started_at": record.analysis_started_at,
            "analysis_completed_at": record.analysis_completed_at,
            "last_result": last_result,
            "artifacts": [
                {
                    "name": item["name"],
                    "kind": item["kind"],
                    "description": item["description"],
                }
                for item in record.workspace.artifacts
            ],
            "created_at": record.created_at,
            "updated_at": time.time(),
            # plan_only 模式产出的待审批计划，恢复时回填到 record.pending_plan，
            # 让前端在服务重启后仍能展示待审批计划供用户确认。
            "pending_plan": record.pending_plan,
            # 用户自定义标题，恢复时回填到 record.title。
            "title": record.title,
        }
        target = self._manifest_path(record)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def _sync_storage(self, session_id: str, root: Path) -> None:
        try:
            self.storage.sync_session(session_id, root)
        except Exception:
            # Object storage is a durability layer; it must not turn a valid
            # upload or completed analysis into a 500 when the provider is down.
            logger.exception("Session storage sync failed for %s", session_id)

    def persist(self, session_id: str, record: SessionRecord) -> None:
        with self._lock:
            self._persist_locked(session_id, record)
        self._sync_storage(session_id, record.workspace.root)

    def _restore_locked(self, session_id: str) -> SessionRecord | None:
        # 若 session 正在删除中（delete 已 pop 但 rmtree 尚未完成），
        # 拒绝恢复，防止返回指向已删目录的 record（H9 竞态修复）。
        if session_id in self._deleting:
            return None
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session_id):
            return None
        root = (self.runs_dir / session_id).resolve()
        if self.runs_dir not in root.parents:
            return None
        input_dir = root / "input"
        has_local_input = input_dir.is_dir() and any(
            path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
            for path in input_dir.iterdir()
        )
        if not has_local_input:
            try:
                self.storage.restore_session(session_id, root)
            except Exception:
                logger.exception("Session storage restore failed for %s", session_id)
                return None
        if not root.is_dir():
            return None
        input_files = [
            path for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ] if input_dir.is_dir() else []
        if not input_files:
            return None
        workspace = DataWorkspace(self.runs_dir, session_id=session_id)
        try:
            workspace.load(input_files[0])
        except (OSError, ValueError):
            return None
        manifest = root / "session.json"
        payload: dict[str, Any] = {}
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = {}
        try:
            workspace.restore_checkpoint()
        except (OSError, ValueError):
            logger.exception("Workspace checkpoint restore failed for %s", session_id)
        workspace.restore_artifacts(payload.get("artifacts"))
        record = SessionRecord(workspace)
        record.chat = [
            item
            for item in payload.get("chat", [])
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
        ][-40:]
        record.created_at = float(payload.get("created_at", record.created_at))
        saved_status = str(payload.get("analysis_status", "idle"))
        # awaiting_approval 是 plan_only 模式产出的待审批态，恢复后仍需保持，
        # 让前端继续展示待审批计划供用户确认；其他非终态统一回退到 idle。
        record.analysis_status = saved_status if saved_status in {"completed", "cancelled", "failed", "awaiting_approval"} else "idle"
        # 恢复墙钟时间；若 manifest 缺字段则基于 created_at 退化为 None，
        # 前端会判断没有 elapsed 数据时不显示。
        started_raw = payload.get("analysis_started_at")
        completed_raw = payload.get("analysis_completed_at")
        record.analysis_started_at = float(started_raw) if isinstance(started_raw, (int, float)) else None
        record.analysis_completed_at = float(completed_raw) if isinstance(completed_raw, (int, float)) else None
        # 恢复待审批计划，让前端在服务重启后仍能展示并让用户确认执行。
        record.pending_plan = payload.get("pending_plan")
        # 恢复用户自定义标题（空串视为未设置）。
        saved_title = payload.get("title")
        record.title = saved_title if isinstance(saved_title, str) and saved_title.strip() else None
        saved_result = payload.get("last_result")
        if isinstance(saved_result, dict) and isinstance(saved_result.get("response"), str):
            record.last_result = AnalysisResult(
                response=saved_result["response"],
                trace=saved_result.get("trace", []),
                artifacts=list(workspace.artifacts),
                dataset_profile=saved_result.get("dataset_profile", workspace.profile()),
                plan=saved_result.get("plan", []),
                completed_steps=saved_result.get("completed_steps", []),
            )
        self._items[session_id] = record
        return record

    def create(self, workspace: DataWorkspace) -> tuple[str, SessionRecord]:
        session_id = workspace.root.name
        record = SessionRecord(workspace)
        with self._lock:
            removed_ids = self._prune_locked(reserve=1)
            self._items[session_id] = record
            self._persist_locked(session_id, record)
        self._cleanup_remote(removed_ids)
        self._sync_storage(session_id, record.workspace.root)
        return session_id, record

    def get(self, session_id: str) -> SessionRecord:
        with self._lock:
            removed_ids = self._prune_locked()
            record = self._items.get(session_id)
            if record is None:
                record = self._restore_locked(session_id)
            if record is not None:
                record.last_access = time.monotonic()
        self._cleanup_remote(removed_ids)
        if record is None:
            raise HTTPException(status_code=404, detail="分析会话不存在或服务已经重启。")
        return record

    def restore_from_directory(self, session_id: str) -> SessionRecord | None:
        """从磁盘目录恢复会话到内存（用于导入）。

        导入流程：ZIP 已解压到 ``self.runs_dir / session_id``，本方法
        调用 ``_restore_locked`` 读取 manifest 和工作区数据，注册到
        ``self._items`` 并同步到对象存储。返回恢复的 SessionRecord，
        若目录无效或损坏返回 None 由调用方清理临时目录。

        与 ``get`` 的区别：``get`` 在 session 不存在时抛 404，本方法
        返回 None 让导入端点能区分"目录无效"与"会话不存在"两种情况，
        并给出更具体的 400 错误提示。
        """
        with self._lock:
            if session_id in self._items:
                return self._items[session_id]
            self._prune_locked(reserve=1)
            record = self._restore_locked(session_id)
            if record is not None:
                self._items[session_id] = record
                record.last_access = time.monotonic()
        if record is not None:
            self._sync_storage(session_id, record.workspace.root)
        return record

    def list_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """Return metadata of recent sessions for the history sidebar.

        扫描 runs_dir 下所有 ``api_*`` 子目录的 session.json，按 created_at
        降序返回。优先复用内存中已 restore 的 SessionRecord，避免每次都
        反序列化 manifest；对未在内存中的会话仅读取 manifest 字段，不
        恢复 DataFrame/checkpoint，保持列表接口轻量。

        锁策略：仅在取内存快照和目录列表时持锁，磁盘 manifest 读取在锁外
        执行——几十个会话 × 1-50KB manifest 的 I/O 若持 RLock 会阻塞所有
        get/create 调用。session 被并发 prune 删除时 manifest 读会失败，
        try/except 已兜底，下一次轮询自然消失。
        """
        if limit <= 0:
            return []
        with self._lock:
            removed_ids = self._prune_locked()
            # 内存中已有的会话先取一份快照，避免磁盘上的 manifest 与活动
            # 状态不一致（例如正在 running 的会话 manifest 还是 idle）。
            in_memory: dict[str, dict[str, Any]] = {}
            for session_id, record in self._items.items():
                in_memory[session_id] = {
                    "id": session_id,
                    "filename": record.workspace.source_path.name if record.workspace.source_path else "dataset",
                    "title": record.title,
                    "analysis_status": record.analysis_status,
                    "created_at": record.created_at,
                    "has_result": record.last_result is not None,
                    "artifact_count": len(record.workspace.artifacts),
                    "updated_at": record.last_access,
                    "in_memory": True,
                }
            # 锁内仅取目录名列表（iterdir 是 O(1) 系统调用），避免锁外
            # 再 iterdir 时遇到刚被 prune 的目录抛 FileNotFoundError。
            disk_session_ids: list[str] = []
            if self.runs_dir.is_dir():
                for entry in self.runs_dir.iterdir():
                    if entry.is_dir() and entry.name.startswith("api_"):
                        disk_session_ids.append(entry.name)
        # 锁外读 manifest：几十个 JSON 文件的 I/O 不再阻塞 get/create。
        seen_ids = set(in_memory.keys())
        disk_results: list[dict[str, Any]] = []
        for session_id in disk_session_ids:
            if session_id in seen_ids:
                continue
            manifest = self.runs_dir / session_id / "session.json"
            if not manifest.is_file():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                # 并发 prune 可能已删除 manifest；跳过，下次轮询不再列出。
                continue
            if not isinstance(payload, dict):
                continue
            artifacts = payload.get("artifacts") or []
            last_result = payload.get("last_result")
            disk_results.append({
                "id": session_id,
                "filename": str(payload.get("filename") or "dataset"),
                "title": payload.get("title"),
                "analysis_status": str(payload.get("analysis_status") or "idle"),
                "created_at": float(payload.get("created_at") or 0.0),
                "has_result": isinstance(last_result, dict) and isinstance(last_result.get("response"), str),
                "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
                "updated_at": float(payload.get("updated_at") or payload.get("created_at") or 0.0),
                "in_memory": False,
            })
        results: list[dict[str, Any]] = list(in_memory.values()) + disk_results
        results.sort(key=lambda item: item.get("created_at") or 0.0, reverse=True)
        # S3/R2 远端清理放在锁外执行（网络 I/O 不持锁），与 get/create 保持一致。
        # 之前丢弃 _prune_locked 返回值会导致被裁剪的会话在远端永久残留。
        if removed_ids:
            self._cleanup_remote(removed_ids)
        return results[:limit]

    def delete(self, session_id: str) -> None:
        """删除会话：移除内存记录、清理工作区目录与远端归档。

        运行中的会话（run_lock 锁定）拒绝删除并抛出 409，避免 worker
        正在写文件时清理目录导致状态不一致。调用方应先取消运行中的
        分析再发起删除。

        路径校验与 ``_restore_locked`` 一致：session_id 必须匹配
        ``[a-zA-Z0-9_-]{1,80}``，且 resolve 后的 root 必须在 runs_dir
        下，防止路径穿越删除任意目录。
        """
        import shutil

        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session_id):
            raise HTTPException(status_code=404, detail="会话不存在。")
        root = (self.runs_dir / session_id).resolve()
        if self.runs_dir not in root.parents:
            raise HTTPException(status_code=404, detail="会话不存在。")
        with self._lock:
            record = self._items.get(session_id)
            if record is not None and record.run_lock.locked():
                raise HTTPException(status_code=409, detail="会话正在运行，请先取消分析再删除。")
            self._items.pop(session_id, None)
            # 标记为删除中：锁外 rmtree 期间，get() 的 _restore_locked
            # 会检查此集合并拒绝恢复，防止竞态产生悬空 record（H9 修复）。
            self._deleting.add(session_id)
        # 锁外清理目录（shutil.rmtree 是 I/O 密集，不持锁）
        try:
            shutil.rmtree(root, ignore_errors=True)
        except OSError:
            logger.exception("Failed to remove session directory %s", session_id)
        # 清理远端归档：与 _cleanup_remote 一致，失败仅记录不抛出
        try:
            self.storage.delete_session(session_id)
        except Exception:
            logger.exception("Session storage delete failed for %s", session_id)
        finally:
            with self._lock:
                self._deleting.discard(session_id)

    def rename(self, session_id: str, title: str) -> str:
        """重命名会话：更新 title 字段并持久化到 session.json + 远端归档。

        title 经清洗去除首尾空白、限制 80 字符，空串视为清除自定义标题
        （回退显示 filename）。会话不存在抛 404。
        """
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session_id):
            raise HTTPException(status_code=404, detail="会话不存在。")
        cleaned = title.strip()[:80]
        with self._lock:
            record = self._items.get(session_id)
            if record is None:
                # 内存没有则尝试从磁盘恢复（用户可能在另一进程重命名）
                record = self._restore_locked(session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="会话不存在。")
            record.title = cleaned or None
            record.last_access = time.monotonic()
            self._persist_locked(session_id, record)
        self._sync_storage(session_id, record.workspace.root)
        return cleaned


# ---------------------------------------------------------------------------
# 进程级单例：在 import 时初始化。bootstrap_settings 来自环境变量，
# registry 与 analysis_slots 依赖它。api.py 会 re-export 这些单例，路由层
# 通过 ``api.registry`` / ``api.analysis_slots`` 等访问以兼容测试 monkeypatch。
# ---------------------------------------------------------------------------
bootstrap_settings = AgentSettings.from_env(provider="deepseek")
# 启动时校验关键资源限制：max_concurrent_analyses <= 0 会导致
# BoundedSemaphore(0) 让所有 acquire() 永久阻塞，服务启动正常但无法
# 执行任何分析。不调用完整的 validate_for_model（需要 api_key），
# 仅校验不影响 LLM 连接但会导致服务假死的资源参数。
if bootstrap_settings.max_concurrent_analyses <= 0:
    # 已由 tests/test_coverage_fill.py::test_registry_import_rejects_zero_concurrency
    # 在子进程中验证（reload 会污染当前进程的单例，无法在主进程覆盖）。
    raise ValueError(  # pragma: no cover - 子进程验证
        "DATA_AGENT_MAX_CONCURRENT_ANALYSES 必须大于 0，"
        f"当前值 {bootstrap_settings.max_concurrent_analyses} 会导致服务无法执行分析。"
    )
session_storage = build_session_storage()
registry = SessionRegistry(
    bootstrap_settings.runs_dir,
    bootstrap_settings.max_active_sessions,
    bootstrap_settings.session_ttl_hours,
    storage=session_storage,
)
runtime_settings = {
    "api_key": "",
    "thinking_enabled": None,
    "reasoning_effort": None,
}
runtime_settings_lock = threading.RLock()
analysis_slots = threading.BoundedSemaphore(bootstrap_settings.max_concurrent_analyses)


# ---------------------------------------------------------------------------
# 运行时设置合成：env 配置 + 进程内覆盖（runtime_settings）+ OS 凭据存储。
# ---------------------------------------------------------------------------
def _effective_settings() -> AgentSettings:
    settings = AgentSettings.from_env(provider="deepseek")
    with runtime_settings_lock:
        settings.api_key = (
            str(runtime_settings["api_key"] or "")
            or settings.api_key
            or get_saved_api_key()
        )
        if runtime_settings["thinking_enabled"] is not None:
            settings.thinking_enabled = bool(runtime_settings["thinking_enabled"])
        if runtime_settings["reasoning_effort"] is not None:
            settings.reasoning_effort = str(runtime_settings["reasoning_effort"])
    return settings


# ---------------------------------------------------------------------------
# 产物 / 会话载荷构造。
# ---------------------------------------------------------------------------
class _ArtifactPriority(enum.IntEnum):
    """数据集产物的展示优先级（数字越小优先级越高）。

    ``_curate_artifacts`` 保留优先级最高的两个数据集产物展示给用户。
    ``transformed_data`` 故意排在 ``DEFAULT`` 之后，仅在没有更权威产物时
    才展示，确保清洗步骤始终在 ArtifactCenter 中有可见产物。
    """

    #: 显式 "final" / "result" 导出（如 cleaned_data_final.csv、analysis_result.csv）
    EXPLICIT_FINAL = 0
    #: clean_data 工具产出的 cleaned_data.csv
    CLEANER_OUTPUT = 1
    #: 其他含 final / result / report 关键词的导出
    OTHER_FINAL = 2
    #: 默认数据集（无特殊标记的普通导出）
    DEFAULT = 3
    #: transform_data 的非破坏性视图，仅在无更权威产物时展示
    TRANSFORMED_VIEW = 4


#: 数据集产物优先级匹配表：按顺序匹配，首个命中即决定优先级。
#: 每个条目为 ``(预编译正则, 优先级, 可读标签)``。未命中任何模式的产物
#: 回退到 ``_ArtifactPriority.DEFAULT``。
_ARTIFACT_PATTERNS: list[tuple[re.Pattern[str], _ArtifactPriority, str]] = [
    (re.compile(r"cleaned_data_final|analysis_result"), _ArtifactPriority.EXPLICIT_FINAL, "explicit final export"),
    (re.compile(r"(^|[_/])cleaned_data\.csv$"), _ArtifactPriority.CLEANER_OUTPUT, "cleaner output"),
    (re.compile(r"final|result|report"), _ArtifactPriority.OTHER_FINAL, "other final/result/report"),
    (re.compile(r"(^|[_/])transformed_data\.csv$"), _ArtifactPriority.TRANSFORMED_VIEW, "transformed view"),
]


def _dataset_priority(name: str) -> _ArtifactPriority:
    """Return the curation priority for a dataset artifact by its filename.

    遍历 ``_ARTIFACT_PATTERNS`` 配置表，首个匹配的模式决定优先级；
    未匹配时回退到 ``_ArtifactPriority.DEFAULT``。
    """
    lowered = name.lower()
    for pattern, priority, _label in _ARTIFACT_PATTERNS:
        if pattern.search(lowered):
            return priority
    return _ArtifactPriority.DEFAULT


def _curate_artifacts(artifacts: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return a concise, user-facing result set instead of every intermediate file."""
    latest_visualizations: dict[str, dict[str, str]] = {}
    images: dict[str, dict[str, str]] = {}
    datasets: list[dict[str, str]] = []
    documents: list[dict[str, str]] = []
    for item in artifacts:
        kind = item.get("kind", "dataset")
        if kind == "chart_data":
            continue
        description = re.sub(r"\s+", " ", item.get("description", "").strip().lower())
        semantic_title = re.split(r"[（(]", description, maxsplit=1)[0]
        semantic_title = semantic_title.replace("相关系数", "相关").replace("相关性", "相关")
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", semantic_title)
        key = key or Path(item.get("name", "artifact")).stem.lower()
        if kind == "visualization":
            latest_visualizations[key] = item
        elif kind == "image":
            images[key] = item
        elif kind == "dataset":
            datasets.append(item)
        else:
            documents.append(item)

    # Prefer explicit "final" / "result" exports, then the cleaner's output,
    # then a non-destructive transformed view. ``transformed_data.csv`` is only
    # surfaced when nothing more authoritative exists, so that a cleaning step
    # always shows up in the ArtifactCenter even if the agent never called
    # export_data with a "final" filename.
    selected_datasets = sorted(datasets, key=lambda item: (_dataset_priority(item.get("name", "")),))[-2:]
    return [
        *list(latest_visualizations.values())[-6:],
        *list(images.values())[-3:],
        *selected_datasets,
        *documents[-2:],
    ]


def _artifact_payload(session_id: str, artifacts: list[dict[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _curate_artifacts(artifacts):
        value = dict(item)
        value["download_url"] = f"/api/sessions/{session_id}/artifacts/{item['name']}"
        value["previewable"] = item.get("kind") == "visualization"
        if item.get("kind") == "visualization":
            value["preview_url"] = f"/api/sessions/{session_id}/artifacts/{item['name']}/preview"
            # 图表缩略图与引擎标识：仅 Plotly 图表（存在 .plotly.json）提供缩略图，
            # ECharts 图表无 JSON 数据文件，回退到图标展示。
            artifact_name = item.get("name", "")
            if artifact_name.endswith(".html"):
                stem = artifact_name[: -len(".html")]
                raw_path = item.get("path")
                artifacts_dir = Path(raw_path).parent if raw_path else None
                has_plotly_json = bool(
                    artifacts_dir and (artifacts_dir / f"{stem}.plotly.json").is_file()
                )
                if has_plotly_json:
                    value["thumbnail_url"] = (
                        f"/api/sessions/{session_id}/artifacts/{artifact_name}/thumbnail"
                    )
                    value["engine"] = "plotly"
                else:
                    value["engine"] = "echarts"
        try:
            value["size_bytes"] = Path(item["path"]).stat().st_size
        except (OSError, KeyError):
            value["size_bytes"] = 0
        value.pop("path", None)
        result.append(value)
    return result


def _elapsed_seconds(record: SessionRecord) -> float | None:
    """Return the analysis duration in seconds, or None when no timing data.

    - running / cancelling: now - started_at
    - completed / cancelled / failed: completed_at - started_at
    - idle: None
    """
    with record._status_lock:  # noqa: SLF001 - 同模块内访问
        status = record._analysis_status  # noqa: SLF001
        started = record.analysis_started_at
        completed = record.analysis_completed_at
    if not started:
        return None
    if status in {"running", "cancelling"}:
        return max(0.0, time.time() - started)
    if completed:
        return max(0.0, completed - started)
    return None


def _session_payload(session_id: str, record: SessionRecord) -> dict[str, Any]:
    workspace = record.workspace
    profile = workspace.profile(sample_rows=8)
    return {
        "id": session_id,
        "filename": workspace.source_path.name if workspace.source_path else "dataset",
        "title": record.title,
        "profile": profile,
        "preview": to_jsonable(workspace.dataframe.head(100)),
        "chat": record.chat,
        "artifacts": _artifact_payload(session_id, workspace.artifacts),
        "analysis_status": record.analysis_status,
        "analysis_started_at": record.analysis_started_at,
        "analysis_completed_at": record.analysis_completed_at,
        "elapsed_seconds": _elapsed_seconds(record),
        "last_result": (
            _result_payload(session_id, record.last_result)
            if record.last_result is not None
            else None
        ),
        # plan_only 模式产出的待审批计划，前端据此渲染审批面板；
        # 为 None 表示当前没有待审批计划。
        "pending_plan": record.pending_plan,
    }


def _result_payload(session_id: str, result: AnalysisResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["artifacts"] = _artifact_payload(session_id, result.artifacts)
    return to_jsonable(payload)


#: 历史消息注入 LLM 上下文时每条内容的最大字符数。assistant 回复是
#: 完整分析报告（结论速览在最前），截断保留头部即可保住核心结论；
#: 避免 8 条长报告在每步 ReAct 调用中重复吃掉上万 token。
_HISTORY_MESSAGE_MAX_CHARS = 2_000


def _trim_history_content(content: str) -> str:
    if len(content) <= _HISTORY_MESSAGE_MAX_CHARS:
        return content
    return content[:_HISTORY_MESSAGE_MAX_CHARS] + "\n…（历史消息过长，已截断）"


def _history(record: SessionRecord) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for item in record.chat[-8:]:
        content = _trim_history_content(item["content"])
        if item["role"] == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


def _artifact_file(session_id: str, filename: str) -> tuple[SessionRecord, Path]:
    # 延迟导入：_artifact_file 通过 ``api.registry`` 访问注册表，以便测试
    # 用 monkeypatch.setattr(api, "registry", ...) 替换时此处也能感知。
    from data_agent import api

    record = api.registry.get(session_id)
    matches = [item for item in record.workspace.artifacts if item["name"] == Path(filename).name]
    if not matches:
        raise HTTPException(status_code=404, detail="产物不存在。")
    path = Path(matches[0]["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="产物文件已被移除。")
    return record, path


# 内置示例数据集：让新用户无需自备文件即可体验完整分析流程。
# 采用销售主题的小型 CSV（订单/地区/品类/金额/数量/日期），覆盖数值、
# 文本、日期三类字段，足以触发清洗、统计、图表等典型工具链。
_SAMPLE_SALES_CSV = (
    "order_id,region,category,product,sales,quantity,order_date,customer_segment\n"
    "1001,华东,电子产品,无线耳机,1280.5,2,2024-01-15,企业\n"
    "1002,华南,办公用品,打印纸,320.0,10,2024-01-18,零售\n"
    "1003,华北,电子产品,机械键盘,890.0,3,2024-01-22,企业\n"
    "1004,华东,家具,人体工学椅,2100.0,1,2024-02-03,政府\n"
    "1005,西南,办公用品,签字笔,75.5,50,2024-02-11,零售\n"
    "1006,华南,电子产品,移动硬盘,560.0,4,2024-02-14,企业\n"
    "1007,华东,家具,书架,780.0,2,2024-02-20,零售\n"
    "1008,华北,电子产品,智能手环,430.0,5,2024-03-01,企业\n"
    "1009,西南,家具,折叠桌,620.0,3,2024-03-05,政府\n"
    "1010,华南,办公用品,文件夹,45.0,100,2024-03-10,零售\n"
    "1011,华东,电子产品,蓝牙音箱,720.0,3,2024-03-15,企业\n"
    "1012,华北,家具,办公沙发,3500.0,1,2024-03-22,政府\n"
    "1013,西南,电子产品,充电宝,210.0,8,2024-04-02,零售\n"
    "1014,华南,办公用品,订书机,38.0,20,2024-04-08,企业\n"
    "1015,华东,家具,储物柜,950.0,2,2024-04-12,零售\n"
    "1016,华北,电子产品,显示器,1800.0,2,2024-04-18,企业\n"
    "1017,西南,办公用品,计算器,65.0,15,2024-04-25,政府\n"
    "1018,华南,家具,会议桌,2800.0,1,2024-05-03,企业\n"
    "1019,华东,电子产品,键盘膜,28.0,30,2024-05-09,零售\n"
    "1020,华北,办公用品,胶带,12.0,200,2024-05-15,零售\n"
)
