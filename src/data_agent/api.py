"""FastAPI 服务层：提供数据分析 Agent 的 HTTP API 和 SSE 流式接口。

核心端点：
- POST /api/sessions: 上传数据文件创建分析会话。
- POST /api/sessions/{id}/analyze: 同步执行分析。
- POST /api/sessions/{id}/analyze/stream: SSE 流式分析（前端主用）。
- POST /api/sessions/{id}/cancel: 取消正在运行的分析。
- GET  /api/sessions/{id}/artifacts/{name}/preview: 图表在线预览。
- GET  /api/sessions/{id}/artifacts/{name}: 产物下载。

安全机制：
- APP_ACCESS_TOKEN 环境变量启用 Bearer Token 认证。
- 滑动窗口速率限制（每客户端每分钟 N 次）。
- 安全响应头（CSP、X-Frame-Options、Permissions-Policy 等）。
- 产物预览通过 CSP 沙箱限制为纯脚本+样式文档。

并发模型：
- 分析在独立 daemon 线程中执行，通过 BoundedSemaphore 控制全局并发数。
- SSE 流通过 asyncio.Queue 在工作线程和事件循环之间传递事件。
- 取消通过 threading.Event + CancelCallback 实现亚秒级响应。
"""

from __future__ import annotations

import asyncio
import enum
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from data_agent.agent import AnalysisCancelled, AnalysisResult, DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.credentials import delete_saved_api_key, get_saved_api_key, save_api_key
from data_agent.serialization import to_jsonable
from data_agent.storage import LocalSessionStorage, SessionStorage, build_session_storage
from data_agent.tools import _PLOTLY_DARK_MODE_SCRIPT
from data_agent.workspace import (
    ECHARTS_BUNDLE_NAME,
    PLOTLY_BUNDLE_NAME,
    SUPPORTED_EXTENSIONS,
    DataWorkspace,
    _atomic_write_text,
)

logger = logging.getLogger(__name__)


class SettingsUpdate(BaseModel):
    api_key: str | None = None
    thinking_enabled: bool = True
    reasoning_effort: str = Field(default="high", pattern="^(high|max)$")
    persist_key: bool = True


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


bootstrap_settings = AgentSettings.from_env(provider="deepseek")
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
request_buckets: dict[str, deque[float]] = defaultdict(deque)
request_buckets_lock = threading.Lock()
analysis_slots = threading.BoundedSemaphore(bootstrap_settings.max_concurrent_analyses)

# SSE 事件队列最大容量。worker 线程生产事件，事件循环消费推送给客户端。
# 客户端网络慢或浏览器卡顿时消费跟不上生产，无界队列会积累数万条事件（每条
# 携带 token 字符串），内存占用可达数百 MB。设 500 上限后，超限的
# thinking_chunk（过程信息，可丢）会被丢弃，report_chunk/tool_call（结果信息）保留。
SSE_QUEUE_MAXSIZE = 500


def _safe_emit(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, item: tuple[str, Any] | None) -> None:
    """跨线程安全推送 SSE 事件到事件循环的队列。

    loop.call_soon_threadsafe 在事件循环已关闭时抛 RuntimeError（进程关闭、
    ASGI worker 被 kill、客户端断连后 cleanup 等场景）。本函数捕获该异常
    避免崩溃——worker 线程的 finally 块仍需正常释放 slot 和 lock，若因
    call_soon_threadsafe 抛错而跳过 release，会导致 analysis_slots 和
    run_lock 永久泄漏（max_concurrent_analyses=2 时泄漏 2 次后服务死锁）。

    队列满时（QueueFull）按事件类型决策：thinking_chunk / report_chunk 等
    流式 token 可丢弃（过程信息），complete / error / cancelled 等终态事件
    强制入队（即使需淘汰最旧的 thinking_chunk 腾位置）。

    注意：asyncio.Queue.full() 不是线程安全的，不能在 worker 线程直接调用。
    改为在 call_soon_threadsafe 回调内部 catch QueueFull 来安全处理满队列。
    """
    event_type = item[0] if item else None
    droppable = event_type in ("thinking_chunk", "report_chunk", "chat_chunk")

    def _put() -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            if not droppable:
                # 终态事件不能丢：淘汰最旧元素后重试一次
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass  # 极端情况：队列已被其他回调清空或仍满，放弃本次写入

    try:
        loop.call_soon_threadsafe(_put)
    except RuntimeError:
        # 事件循环已关闭，丢弃事件但不崩溃，确保 finally 中的 release 能执行
        pass


app = FastAPI(
    title="Data Analysis Agent API",
    version="2.0.0",
    description="Plan-and-Execute + ReAct data analysis service",
)

allowed_origins = [
    item.strip()
    for item in os.getenv(
        "DATA_AGENT_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _check_access(request: Request) -> None:
    expected = os.getenv("APP_ACCESS_TOKEN", "").strip()
    if not expected:
        return
    provided = request.headers.get("x-app-token", "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:]
    if not provided:
        provided = request.headers.get("authorization", "")
        if provided.lower().startswith("bearer "):
            provided = provided[7:]
    if not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=401, detail="需要有效的应用访问令牌。")


# Rate-limit bucket dictionary hard cap. An attacker spoofing X-Forwarded-For
# can otherwise manufacture unlimited unique keys and OOM the process. 10K
# entries × ~60 floats each stays under 5MB and is plenty for legitimate load.
MAX_RATE_LIMIT_BUCKETS = 10_000

# Number of trusted reverse-proxy hops in front of this process. Each trusted
# proxy appends the IP it received the request from to X-Forwarded-For. We
# therefore trust the Nth-from-last entry; taking the leftmost entry when no
# proxy is declared would let attackers forge the header and bypass limits.
# Default 0 means "direct exposure, ignore XFF"; set 1 for Render/Nginx.
_trusted_proxy_hops = max(0, int(os.getenv("DATA_AGENT_TRUSTED_PROXY_HOPS", "0")))


def _client_identifier(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Only consults ``X-Forwarded-For`` when ``DATA_AGENT_TRUSTED_PROXY_HOPS`` is
    set, taking the Nth-from-last entry so an attacker cannot forge a header
    to spin up fresh identities. Falls back to the direct socket address so
    the limiter always has a stable key.
    """
    if _trusted_proxy_hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            parts = [item.strip() for item in forwarded.split(",") if item.strip()]
            if len(parts) >= _trusted_proxy_hops:
                return parts[-_trusted_proxy_hops]
            # XFF has fewer entries than declared hops — likely an internal
            # health check that bypassed the proxy. Fall through to direct host.
    if request.client:
        return request.client.host
    return "unknown"


def _prune_rate_buckets_locked(now: float) -> None:
    """Bound the rate-limit dict size. Caller must hold request_buckets_lock."""
    if len(request_buckets) < MAX_RATE_LIMIT_BUCKETS:
        return
    # Drop buckets with no activity in the last 60s (the common case).
    stale = [
        key for key, bucket in request_buckets.items()
        if not bucket or now - bucket[-1] >= 60
    ]
    for key in stale:
        del request_buckets[key]
    # If still over the cap, evict the oldest 25% by last-seen timestamp to
    # amortise the cost across many writes instead of scanning every request.
    if len(request_buckets) >= MAX_RATE_LIMIT_BUCKETS:
        ordered = sorted(
            request_buckets.items(),
            key=lambda kv: kv[1][-1] if kv[1] else 0.0,
        )
        excess = len(request_buckets) - MAX_RATE_LIMIT_BUCKETS // 2
        for key, _ in ordered[:max(excess, 0)]:
            del request_buckets[key]


def _check_rate_limit(request: Request) -> None:
    if request.method not in {"POST", "PUT", "DELETE"} or not request.url.path.startswith("/api/"):
        return
    limit = bootstrap_settings.rate_limit_per_minute
    now = time.monotonic()
    key = _client_identifier(request)
    with request_buckets_lock:
        _prune_rate_buckets_locked(now)
        bucket = request_buckets[key]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试。")
        bucket.append(now)


@app.middleware("http")
async def protect_api(request: Request, call_next):
    response = None
    # CORS preflight requests do not carry the application token. Let
    # CORSMiddleware answer OPTIONS before enforcing auth on the real request.
    if (
        request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path not in {"/api/health", "/api/auth"}
    ):
        try:
            _check_access(request)
            _check_rate_limit(request)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    if response is None:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    return response


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


def _history(record: SessionRecord) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for item in record.chat[-8:]:
        if item["role"] == "user":
            messages.append(HumanMessage(content=item["content"]))
        else:
            messages.append(AIMessage(content=item["content"]))
    return messages


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "architecture": "plan-and-execute-react",
        "storage_backend": session_storage.backend,
        "persistent_storage": str(session_storage.persistent).lower(),
    }


@app.get("/api/auth")
def auth_status(request: Request) -> dict[str, bool]:
    required = bool(os.getenv("APP_ACCESS_TOKEN", "").strip())
    if not required:
        return {"required": False, "authenticated": True}
    try:
        _check_access(request)
    except HTTPException:
        return {"required": True, "authenticated": False}
    return {"required": True, "authenticated": True}


@app.get("/api/storage/health")
def storage_health() -> dict[str, str | bool]:
    return session_storage.healthcheck()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = _effective_settings()
    storage_status = session_storage.healthcheck()
    return {
        "provider": "deepseek",
        "model": settings.model,
        "base_url": settings.base_url,
        "configured": bool(settings.api_key),
        "thinking_enabled": settings.thinking_enabled,
        "reasoning_effort": settings.reasoning_effort,
        "langsmith_tracing": os.getenv("LANGSMITH_TRACING", "false").lower() == "true",
        "langsmith_project": os.getenv("LANGSMITH_PROJECT", "data-analysis-agent"),
        "storage_backend": session_storage.backend,
        "persistent_storage": session_storage.persistent,
        "storage_status": storage_status.get("status", "unknown"),
        "storage_message": storage_status.get("message", ""),
    }


@app.put("/api/settings")
def update_settings(update: SettingsUpdate) -> dict[str, Any]:
    keyring_warning = ""
    if update.api_key is not None:
        value = update.api_key.strip()
        if not value:
            raise HTTPException(status_code=422, detail="API Key 不能为空。")
        persisted = _save_runtime_api_key(value, update.persist_key)
        if update.persist_key and not persisted:
            keyring_warning = "系统凭据存储不可用，本次 Key 仅保留在服务进程内存中，重启后需重新填写。"
    with runtime_settings_lock:
        runtime_settings["thinking_enabled"] = update.thinking_enabled
        runtime_settings["reasoning_effort"] = update.reasoning_effort
    payload = get_settings()
    if keyring_warning:
        payload["warning"] = keyring_warning
    return payload


@app.delete("/api/settings/key")
def delete_key() -> dict[str, bool]:
    with runtime_settings_lock:
        runtime_settings["api_key"] = ""
    delete_saved_api_key()
    configured = bool(_effective_settings().api_key)
    return {"configured": configured}


@app.get("/api/sessions")
def list_sessions(limit: int = 30) -> dict[str, Any]:
    """List recent sessions for the sidebar history panel.

    仅返回 manifest 摘要（id、filename、status、created_at、has_result），
    不加载 DataFrame，确保接口在 runs/ 有几十上百个会话时仍然很快。
    """
    capped = max(1, min(int(limit), 100))
    return {"sessions": registry.list_recent(limit=capped)}


@app.post("/api/sessions", status_code=201)
async def create_session(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    # Resource limits and the run directory are process-level deployment
    # settings. Reuse the startup snapshot so uploads cannot drift into a
    # different directory when the environment changes mid-process.
    settings = bootstrap_settings
    session_id = f"api_{uuid4().hex[:12]}"
    workspace = DataWorkspace(settings.runs_dir, session_id=session_id)
    try:
        saved = workspace.save_upload_stream(file.filename or "dataset.csv", file.file, settings.max_upload_bytes)
        if saved.stat().st_size == 0:
            raise ValueError("上传文件为空。")
        workspace.load(saved)
        rows, columns = len(workspace.dataframe), len(workspace.dataframe.columns)
        if rows > settings.max_rows or rows * columns > settings.max_cells:
            raise ValueError(f"数据规模超过限制：最多 {settings.max_rows:,} 行或 {settings.max_cells:,} 个单元格。")
    except Exception as exc:
        # pd.read_parquet 损坏文件抛 pyarrow.ArrowInvalid，pd.read_excel 抛
        # openpyxl.exceptions.InvalidFileException，都不在 ValueError/OSError 子类内。
        # 之前只捕获 (ValueError, OSError) 会漏掉这些异常，留下孤儿 workspace 目录
        # （registry.create 未执行，TTL prune 也清理不到）。通用 Exception 兜底确保
        # 任何 load 失败都会清理临时目录。
        workspace.cleanup()
        # 已知的业务错误返回 422，未知异常返回 500 避免暴露内部细节。
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="数据文件解析失败，请检查格式。") from exc
    actual_id, record = registry.create(workspace)
    return _session_payload(actual_id, record)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return _session_payload(session_id, registry.get(session_id))


@app.post("/api/sessions/{session_id}/analyze")
def analyze(session_id: str, request: AnalyzeRequest) -> dict[str, Any]:
    record = registry.get(session_id)
    settings = _effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    history = _history(record)
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话已有分析正在运行。")
    if not analysis_slots.acquire(blocking=False):
        record.run_lock.release()
        raise HTTPException(status_code=429, detail="当前服务正在处理其他分析，请稍后再试。")
    record.cancel_event.clear()
    record.set_running()
    record.current_task = request.task
    try:
        agent = DataAnalysisAgent(record.workspace, settings, cancel_event=record.cancel_event)
        result = agent.run(request.task, history=history, resume_from=request.resume_from)
        # 在持有 run_lock 的窗口内完成 chat/last_result/persist，避免另一线程
        # 在 release 与 persist 之间拿到锁并基于旧 chat 启动新分析。cancelled
        # 和 failed 分支没有 result，不需要写 chat，直接落到对应 except 持久化。
        record.chat.extend(
            [
                {"role": "user", "content": request.task},
                {"role": "assistant", "content": result.response},
            ]
        )
        record.last_result = result
        record.set_finished("completed")
        registry.persist(session_id, record)
    except AnalysisCancelled as exc:
        record.set_finished("cancelled")
        registry.persist(session_id, record)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        record.set_finished("failed")
        registry.persist(session_id, record)
        raise HTTPException(status_code=502, detail=f"分析执行失败：{exc}") from exc
    finally:
        record.current_task = ""
        record.cancel_event.clear()
        analysis_slots.release()
        record.run_lock.release()
    return _result_payload(session_id, result)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(to_jsonable(data), ensure_ascii=False)}\n\n"


@app.post("/api/sessions/{session_id}/analyze/stream")
async def analyze_stream(session_id: str, request: AnalyzeRequest) -> StreamingResponse:
    record = registry.get(session_id)
    settings = _effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    history = _history(record)
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话已有分析正在运行。")
    if not analysis_slots.acquire(blocking=False):
        record.run_lock.release()
        raise HTTPException(status_code=429, detail="当前服务正在处理其他分析，请稍后再试。")
    record.cancel_event.clear()
    record.set_running()
    record.current_task = request.task

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE)

    def _emit_progress(node: str, title: str) -> None:
        # Called from the worker thread at node entry; hop back to the event
        # loop so the SSE generator can flush a progress frame immediately
        # instead of waiting for the node to finish.
        _safe_emit(loop, queue, ("progress", {"node": node, "title": title}))

    def _emit_event(event_type: str, payload: Any) -> None:
        # 通用事件通道：推送 report_chunk / tool_call / tool_result 等细粒度
        # 事件。与 _emit_progress 并存，前者保持后向兼容（progress_callback
        # 签名不变），后者承载新的流式体验。同样需要 call_soon_threadsafe
        # 跨线程回到事件循环。
        _safe_emit(loop, queue, (event_type, payload))

    def _run_analysis() -> None:
        try:
            agent = DataAnalysisAgent(
                record.workspace,
                settings,
                cancel_event=record.cancel_event,
                progress_callback=_emit_progress,
                event_callback=_emit_event,
            )
            final_payload: dict[str, Any] | None = None
            # plan_only 模式下记录 plan_analysis 节点输出，用于待审批计划
            # 持久化和 plan_ready 事件推送。完整执行模式下保持为 None，
            # 不影响原有 finalize 完成路径。
            plan_payload: dict[str, Any] | None = None
            for update in agent.stream(
                request.task,
                history=history,
                resume_from=request.resume_from,
                plan_only=request.plan_only,
            ):
                node = update["node"]
                data = update["data"]
                if node == "finalize":
                    final_payload = data
                if node == "plan_analysis":
                    plan_payload = data
                _safe_emit(loop, queue, (node, data))
            if request.plan_only:
                # 仅规划模式：不进入 finalize，提取计划并切换到待审批态。
                # 前端收到 plan_ready 事件后展示审批面板，用户确认后用
                # resume_from 注入 pending_plan 发起执行请求。
                if plan_payload is None:
                    raise RuntimeError("规划阶段未返回计划。")
                record.pending_plan = plan_payload.get("plan", [])
                record.set_awaiting_approval()
                registry.persist(session_id, record)
                _safe_emit(loop, queue, ("plan_ready", {
                    "plan": plan_payload.get("plan", []),
                    "objective": plan_payload.get("objective", ""),
                }))
            else:
                if final_payload is None:
                    raise RuntimeError("工作流没有返回最终结果。")
                result = AnalysisResult(
                    response=final_payload["response"],
                    trace=final_payload.get("trace", []),
                    artifacts=final_payload.get("artifacts", []),
                    dataset_profile=final_payload["dataset_profile"],
                    plan=final_payload.get("plan", []),
                    completed_steps=final_payload.get("completed_steps", []),
                    usage=final_payload.get("usage"),
                    reasoning=final_payload.get("reasoning", ""),
                )
                record.chat.extend(
                    [
                        {"role": "user", "content": request.task},
                        {"role": "assistant", "content": result.response},
                    ]
                )
                record.last_result = result
                record.set_finished("completed")
                registry.persist(session_id, record)
                _safe_emit(loop, queue, ("complete", _result_payload(session_id, result)))
        except AnalysisCancelled:
            record.set_finished("cancelled")
            registry.persist(session_id, record)
            _safe_emit(loop, queue, ("cancelled", {"message": "分析已取消。"}))
        except Exception as exc:
            record.set_finished("failed")
            registry.persist(session_id, record)
            _safe_emit(loop, queue, ("error", {"message": str(exc)}))
        finally:
            record.current_task = ""
            # Always clear the cancel event so the next analysis on this
            # session starts from a clean state, even if the client aborted
            # mid-stream and set the event after the worker already finished.
            record.cancel_event.clear()
            # 关键：先释放 slot 和 lock，再 call_soon_threadsafe。
            # call_soon_threadsafe 在事件循环已关闭时会抛 RuntimeError
            # （进程关闭、ASGI worker 被 kill 等场景），若放在 release 之前，
            # 异常会跳过 release 导致 analysis_slots 和 run_lock 永久泄漏
            # —— max_concurrent_analyses=2 时泄漏 2 次后整个服务无法启动新分析。
            analysis_slots.release()
            record.run_lock.release()
            # 发送哨兵值 None 通知 SSE 生成器结束循环。_safe_emit 内部已处理
            # RuntimeError（loop 关闭）和 QueueFull，无需再 try/except。
            _safe_emit(loop, queue, None)

    worker = threading.Thread(target=_run_analysis, name=f"analysis-{session_id}", daemon=True)

    async def _await_worker_exit(timeout: float) -> bool:
        """Poll worker.is_alive() without blocking the event loop.

        ``threading.Thread.join`` is a blocking call; invoking it directly in a
        coroutine stalls the event loop for the entire timeout window, which
        means other HTTP requests (history polling, new uploads, cancels) are
        frozen while we wait for a possibly-stuck LLM call to unwind. Polling
        with ``asyncio.sleep`` keeps the loop responsive.
        """
        deadline = loop.time() + timeout
        while worker.is_alive() and loop.time() < deadline:
            await asyncio.sleep(0.1)
        return not worker.is_alive()

    async def generate():
        yield _sse("started", {"task": request.task})
        worker.start()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield _sse("heartbeat", {"status": record.analysis_status})
                    continue
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        except asyncio.CancelledError:
            record.cancel_event.set()
            # CAS 式转换：只在当前仍是 running 时才写 cancelling 过渡态。
            # 若 worker 已先于本块写入 completed/cancelled/failed 终态，不能覆盖。
            # 之前的实现无条件赋值 cancelling，会把已完成的会话回退到 cancelling，
            # 而 worker 已退出不会推进到 cancelled，会话永久卡死。
            with record._status_lock:
                already_terminal = record._analysis_status not in {"running", "cancelling"}
                if not already_terminal:
                    record._analysis_status = "cancelling"
            if not already_terminal:
                registry.persist(session_id, record)
            # 给 worker 最多 5 秒优雅退出时间。单次 LLM 调用可能 60+ 秒，
            # 5 秒内未退出属正常——worker 是 daemon 线程，会通过自己的 finally
            # 释放 slot/lock 并 persist 终态，无需本协程继续等待。
            # 用 asyncio.sleep 轮询而非 worker.join，避免阻塞事件循环导致
            # 其他请求（历史上传、取消、健康检查）被冻结 5 秒。
            exited = await _await_worker_exit(timeout=5.0)
            if not exited:
                logger.warning(
                    "Analysis worker for session %s did not exit within 5s of cancel; "
                    "slot will be released when the current LLM call returns.",
                    session_id,
                )
            raise
        finally:
            # 正常完成路径下 worker 已通过 finally put None 退出，无需 join。
            # CancelledError 路径已在 except 内等待 5s，这里不再重复等待。
            if worker.is_alive():
                logger.debug(
                    "Worker still running at stream teardown for session %s; "
                    "daemon thread will release resources via its own finally.",
                    session_id,
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, request: AnalyzeRequest) -> StreamingResponse:
    """轻量追问 SSE 端点：不走 plan-and-execute，直接用 ReAct 执行器回答。

    与 ``analyze/stream`` 的区别：
    - 不触发 validate→plan→execute→finalize 完整工作流，单次 ReAct 循环即可回答。
    - 不占用 ``analysis_slots``（追问更轻量，不与全量分析竞争全局并发槽）。
    - 仍持有 ``run_lock``，防止追问与全量分析在同一会话并发写 workspace。
    - 事件类型用 ``chat_chunk``（而非 ``report_chunk``），前端渲染到对话气泡。
    - 终态事件为 ``chat_done``（而非 ``complete``），携带回答文本和新增产物。

    适合场景：基于已有分析结果的快速追问，如"把刚才那张图改成红色"、
    "解释一下这个 p 值"、"再做一个年龄分布图"。
    """
    record = registry.get(session_id)
    settings = _effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    if record.analysis_status == "running":
        raise HTTPException(status_code=409, detail="当前会话有分析正在运行，请等待完成后再追问。")
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话正在处理其他请求，请稍后再试。")
    history = _history(record)
    record.cancel_event.clear()
    record.current_task = request.task

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE)

    def _emit_event(event_type: str, payload: Any) -> None:
        _safe_emit(loop, queue, (event_type, payload))

    def _run_chat() -> None:
        try:
            agent = DataAnalysisAgent(
                record.workspace,
                settings,
                cancel_event=record.cancel_event,
                event_callback=_emit_event,
            )
            response_text, new_artifacts = agent.chat(request.task, history=history)
            record.chat.extend(
                [
                    {"role": "user", "content": request.task},
                    {"role": "assistant", "content": response_text},
                ]
            )
            registry.persist(session_id, record)
            _safe_emit(
                loop,
                queue,
                (
                    "chat_done",
                    {
                        "response": response_text,
                        "artifacts": _artifact_payload(session_id, new_artifacts),
                        "usage": agent._last_usage,
                        "reasoning": agent._last_reasoning,
                    },
                ),
            )
        except AnalysisCancelled:
            registry.persist(session_id, record)
            _safe_emit(loop, queue, ("cancelled", {"message": "追问已取消。"}))
        except Exception as exc:
            logger.exception("Chat worker failed for session %s", session_id)
            _safe_emit(loop, queue, ("error", {"message": f"追问失败：{exc}"}))
        finally:
            record.current_task = ""
            record.cancel_event.clear()
            record.run_lock.release()
            _safe_emit(loop, queue, None)

    worker = threading.Thread(target=_run_chat, name=f"chat-{session_id}", daemon=True)

    async def generate():
        yield _sse("started", {"task": request.task})
        worker.start()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield _sse("heartbeat", {"status": record.analysis_status})
                    continue
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        except asyncio.CancelledError:
            record.cancel_event.set()
            raise
        finally:
            if worker.is_alive():
                logger.debug(
                    "Chat worker still running at stream teardown for session %s; "
                    "daemon thread will release run_lock via its own finally.",
                    session_id,
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/cancel")
def cancel_analysis(session_id: str) -> dict[str, str]:
    record = registry.get(session_id)
    # CAS 式状态转换：在 _status_lock 内原子地检查并切换 running → cancelling，
    # 避免"检查通过后 worker 已 set_finished('completed') 覆盖终态"的 TOCTOU 竞态。
    # 直接用 record.analysis_status = "cancelling" 会在 worker 已写入 completed 后
    # 把状态回退到 cancelling，而此时 worker 已退出，没有任何线程会再推进到 cancelled，
    # 导致会话永久卡在 cancelling。
    with record._status_lock:
        if record._analysis_status != "running":
            return {"status": record._analysis_status}
        record._analysis_status = "cancelling"
    # cancel_event 必须在锁外 set：worker 等待 event 时不会持有 _status_lock，
    # 但持锁调用 event.set() 不会带来收益，反而拉长锁持有时间。
    record.cancel_event.set()
    # Persist so a restart between this call and the worker's unwind does
    # not leave the manifest stuck on "running"; the retry poller relies on
    # seeing "cancelling" to recognize an interrupted analysis.
    try:
        registry.persist(session_id, record)
    except Exception:
        logger.exception("Failed to persist cancelling status for %s — may show stale state on restart", session_id)
    return {"status": "cancelling"}


@app.get("/api/sessions/{session_id}/export")
def export_session(session_id: str) -> StreamingResponse:
    """导出会话完整状态为 ZIP 归档。

    将会话工作区根目录下所有文件（input/、artifacts/、session.json、
    workspace_state.parquet 等）打包成 ZIP 流式返回。前端下载后可在
    其他实例通过 /api/sessions/import 导入恢复完整会话状态。

    安全：仅打包会话根目录内的文件，不跟随符号链接；rglob("*") 遍历
    后通过 path.is_file() 过滤掉目录，避免 ZipFile 写入空目录条目。
    """
    import io
    import zipfile

    record = registry.get(session_id)
    root = record.workspace.root
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(root))
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.zip"'},
    )


@app.post("/api/sessions/import", status_code=201)
async def import_session(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """导入会话 ZIP 归档，创建新会话。

    接收前端通过 /export 下载的 ZIP，解压到新的 runs/<session_id> 目录，
    调用 ``restore_from_directory`` 读取 manifest 并恢复工作区状态。

    安全：
    - 路径遍历防护：解压前遍历归档成员，校验每个解压目标必须位于
      会话根目录内，防止 ``../`` 等恶意路径逃逸。
    - 无效 ZIP 返回 400；解压后 manifest 读取失败返回 400 并清理目录。
    - 生成新的 session_id，避免与已有会话冲突。
    """
    import io
    import shutil
    import zipfile

    session_id = f"api_{uuid4().hex[:12]}"
    root = bootstrap_settings.runs_dir / session_id
    root.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            # 路径遍历防护：先校验所有成员再解压，避免半解压后才发现
            # 恶意路径。target.resolve() 后检查 root 是否在其父链上。
            for member in bundle.infolist():
                target = (root / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise HTTPException(status_code=400, detail="归档包含不安全路径。")
            bundle.extractall(root)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="无效的 ZIP 文件。") from exc
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    record = registry.restore_from_directory(session_id)
    if record is None:
        # manifest 损坏或缺少 input 文件：清理临时目录并返回 400。
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="导入的会话归档无效。")
    return _session_payload(session_id, record)


def _artifact_file(session_id: str, filename: str) -> tuple[SessionRecord, Path]:
    record = registry.get(session_id)
    matches = [item for item in record.workspace.artifacts if item["name"] == Path(filename).name]
    if not matches:
        raise HTTPException(status_code=404, detail="产物不存在。")
    path = Path(matches[0]["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="产物文件已被移除。")
    return record, path


_PLOTLY_TAG_PATTERN = re.compile(
    r"<script\s+src=['\"]plotly\.min\.js['\"]\s*></script>",
    flags=re.IGNORECASE,
)

# ECharts bundle 内联正则：匹配相对路径或 CDN URL 的 echarts.min.js 引用，
# 用于把预览/下载 HTML 内联成自包含文档。
_ECHARTS_TAG_PATTERN = re.compile(
    r"<script\s+src=['\"](?:echarts\.min\.js|https?://[^'\"]*echarts[^'\"]*\.js)['\"]\s*></script>",
    flags=re.IGNORECASE,
)


def _inline_echarts_bundle(record: SessionRecord, html_text: str) -> str:
    """Replace the ECharts ``<script src>`` tag with the full source so previews
    and downloads stay self-contained when the bundle was downloaded locally.

    若 echarts.min.js 未下载到 artifacts_dir（离线场景），保持原 CDN 引用
    不变（在线场景可用），不报错。
    """
    bundle_path = record.workspace.artifacts_dir / ECHARTS_BUNDLE_NAME
    if not bundle_path.is_file():
        return html_text
    try:
        echarts_js = bundle_path.read_text(encoding="utf-8")
    except OSError:
        return html_text
    return _ECHARTS_TAG_PATTERN.sub(
        lambda _match: f"<script>{echarts_js}</script>", html_text, count=1
    )

_PREVIEW_CSP = (
    "default-src 'none'; "
    # ECharts 离线 fallback 时需引用 jsdelivr CDN，加入白名单。
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-src 'none'; "
    "manifest-src 'none'"
)


def _inline_plotly_bundle(record: SessionRecord, html_text: str) -> str:
    """Replace the shared ``<script src='plotly.min.js'>`` tag with the full
    Plotly.js source so previews and downloads stay self-contained."""
    bundle_path = record.workspace.artifacts_dir / PLOTLY_BUNDLE_NAME
    if not bundle_path.is_file():
        return html_text
    try:
        plotly_js = bundle_path.read_text(encoding="utf-8")
    except OSError:
        return html_text
    # Use a lambda replacement so backslashes in plotly_js (e.g. "\s" inside
    # the minified source) are treated literally instead of as regex escapes.
    return _PLOTLY_TAG_PATTERN.sub(lambda _match: f"<script>{plotly_js}</script>", html_text, count=1)


def _harden_preview_document(html_text: str) -> str:
    """Confine generated chart HTML to a script-only, offline document."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{_PREVIEW_CSP}">'
    head_pattern = re.compile(r"<head(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if head_pattern.search(html_text):
        return head_pattern.sub(lambda match: f"{match.group(0)}{meta}", html_text, count=1)
    # 如果原文已是完整文档但缺少 <head>（罕见），直接在 <html> 后注入 <head>。
    html_tag_pattern = re.compile(r"<html(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if html_tag_pattern.search(html_text):
        return html_tag_pattern.sub(
            lambda match: f"{match.group(0)}<head>{meta}</head>", html_text, count=1
        )
    # 原文是 body 片段，包一层完整文档。先检测是否已带 doctype，避免重复声明
    # 导致浏览器进入怪异模式。
    if re.match(r"\s*<!doctype", html_text, flags=re.IGNORECASE):
        return html_text
    return f"<!doctype html><html><head>{meta}</head><body>{html_text}</body></html>"


@app.get("/api/sessions/{session_id}/artifacts/{filename}/preview")
def preview_artifact(session_id: str, filename: str) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() != ".html":
        raise HTTPException(status_code=415, detail="该产物不支持在线预览。")
    html_text = _inline_plotly_bundle(record, path.read_text(encoding="utf-8"))
    html_text = _inline_echarts_bundle(record, html_text)
    html_text = _harden_preview_document(html_text)
    return Response(
        content=html_text,
        media_type="text/html",
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/sessions/{session_id}/artifacts/{filename}")
def download_artifact(session_id: str, filename: str) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() == ".html":
        # Downloads must remain self-contained so they open offline.
        html_text = _inline_plotly_bundle(record, path.read_text(encoding="utf-8"))
        html_text = _inline_echarts_bundle(record, html_text)
        return Response(
            content=html_text,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"},
        )
    return FileResponse(path, filename=path.name)


@app.get("/api/sessions/{session_id}/artifacts/{filename}/thumbnail")
def get_chart_thumbnail(session_id: str, filename: str) -> FileResponse:
    """生成图表缩略图 PNG（best-effort，依赖 Kaleido）。

    优先返回已缓存的 ``{stem}_thumb.png``；缓存不存在时从 ``{stem}.plotly.json``
    重新渲染。Kaleido 未安装时返回 503，其他渲染异常返回 500。缩略图尺寸
    400×250，去掉 margin 节省空间。
    """
    record = registry.get(session_id)
    workspace = record.workspace
    # 取基名防止路径遍历，并去掉可能的 .html 后缀得到原始 stem。
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    # 先查是否已有缓存的缩略图
    thumb_path = workspace.artifacts_dir / f"{stem}_thumb.png"
    if thumb_path.is_file():
        return FileResponse(thumb_path, media_type="image/png")
    # 从 .plotly.json 重新生成
    json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="图表数据文件不存在。")
    try:
        import plotly.graph_objects as go

        fig_dict = json.loads(json_path.read_text(encoding="utf-8"))
        fig = go.Figure(fig_dict)
        # 缩略图尺寸 400x250，去掉 margin 节省空间
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), showlegend=False)
        fig.write_image(str(thumb_path), width=400, height=250, scale=1)
        return FileResponse(thumb_path, media_type="image/png")
    except ImportError:
        raise HTTPException(status_code=503, detail="服务器未安装图片渲染依赖（kaleido）。") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"缩略图生成失败：{exc}") from exc


@app.put("/api/sessions/{session_id}/artifacts/{filename}/edit")
def edit_chart(session_id: str, filename: str, request: ChartEditRequest) -> dict[str, Any]:
    """编辑已有图表：修改标题或配色，基于 .plotly.json 重新生成 HTML。

    流程：
    1. 根据传入的 filename（可为 ``xxx.html`` 或 ``xxx``）定位同名
       ``xxx.plotly.json`` 数据文件。
    2. 读取 fig_dict，应用 title/color 修改。
    3. 用与 tools.py 一致的 HTML 模板（含暗色模式脚本、XSS 转义、
       原子写）重新生成 HTML，覆盖原文件。
    4. 同步更新 .plotly.json，保证后续编辑基于最新数据。

    安全：
    - filename 经 Path(filename).name 取基名，防止路径遍历。
    - HTML 生成复用 tools.py 的 ``</script>`` → ``<\\/script>`` 转义。
    """
    from html import escape

    import plotly.graph_objects as go

    record = registry.get(session_id)
    workspace = record.workspace
    # 取基名防止路径遍历，并去掉可能的 .html 后缀得到原始 stem。
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
    html_path = workspace.artifacts_dir / f"{stem}.html"
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="图表数据文件不存在，无法编辑。")
    try:
        fig_dict = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"图表数据读取失败：{exc}") from exc

    # 应用修改：title 写入 layout.title.text（保持 Plotly 标准结构）；
    # color 应用到所有 trace 的 marker.color，无 marker 的 trace 自动创建。
    if request.title is not None:
        fig_dict.setdefault("layout", {})["title"] = {"text": request.title}
    if request.color is not None:
        for trace in fig_dict.get("data", []):
            if "marker" in trace and isinstance(trace["marker"], dict):
                trace["marker"]["color"] = request.color
            else:
                trace["marker"] = {"color": request.color}

    # 重新生成 HTML：与 tools.py 保持一致的模板和转义逻辑，
    # 确保编辑后的图表预览/下载体验与原始生成一致。
    try:
        fig = go.Figure(fig_dict)
        shared_plotly = workspace.ensure_plotly_bundle()
        relative_script = (
            shared_plotly.relative_to(workspace.artifacts_dir).as_posix()
            if shared_plotly
            else None
        )
        display_title = (
            (fig_dict.get("layout", {}) or {}).get("title", {}).get("text", "")
            or stem
        )
        if relative_script:
            html_template = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>{title}</title><script src='{script}'></script>"
                "<style>html,body{{width:100%;height:100%;margin:0;background:#fbfaf5;overflow:hidden}}"
                ".plotly-graph-div{{width:100% !important;height:100% !important;min-height:560px}}</style>"
                "</head><body>{div}{dark_script}</body></html>"
            )
            div = fig.to_html(
                full_html=False,
                include_plotlyjs=False,
                default_width="100%",
                default_height="100%",
                config={
                    "responsive": True,
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                },
            )
            # XSS 防护：与 tools.py 一致，转义 </script> 避免 Plotly
            # 序列化数据中的 </script> 提前关闭 script 块导致注入。
            div = div.replace("</script>", "<\\/script>")
            _atomic_write_text(
                html_path,
                html_template.format(
                    title=escape(display_title),
                    script=relative_script,
                    div=div,
                    dark_script=_PLOTLY_DARK_MODE_SCRIPT,
                ),
            )
        else:
            # plotly bundle 不可用（极少见）时回退到内联 plotlyjs 的完整 HTML。
            fig.write_html(html_path, include_plotlyjs=True, full_html=True)
        # 同步更新 .plotly.json，保证后续编辑基于最新数据。
        fig.write_json(json_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"图表重新生成失败：{exc}") from exc
    return {"status": "ok", "message": "图表已更新。"}


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    assets_dir = frontend_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_app(full_path: str) -> FileResponse:
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            raise HTTPException(status_code=404, detail="Not found")
        requested = (frontend_dist / full_path).resolve()
        if frontend_dist.resolve() in requested.parents and requested.is_file():
            return FileResponse(requested)
        return FileResponse(frontend_dist / "index.html")
