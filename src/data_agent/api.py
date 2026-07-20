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
from data_agent.workspace import PLOTLY_BUNDLE_NAME, SUPPORTED_EXTENSIONS, DataWorkspace

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
        record.analysis_status = saved_status if saved_status in {"completed", "cancelled", "failed"} else "idle"
        # 恢复墙钟时间；若 manifest 缺字段则基于 created_at 退化为 None，
        # 前端会判断没有 elapsed 数据时不显示。
        started_raw = payload.get("analysis_started_at")
        completed_raw = payload.get("analysis_completed_at")
        record.analysis_started_at = float(started_raw) if isinstance(started_raw, (int, float)) else None
        record.analysis_completed_at = float(completed_raw) if isinstance(completed_raw, (int, float)) else None
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
    def _dataset_priority(name: str) -> int:
        lowered = name.lower()
        if re.search(r"cleaned_data_final|analysis_result", lowered):
            return 0
        if re.search(r"(^|[_/])cleaned_data\.csv$", lowered):
            return 1
        if re.search(r"final|result|report", lowered):
            return 2
        if re.search(r"(^|[_/])transformed_data\.csv$", lowered):
            return 4
        return 3

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
        result = agent.run(request.task, history=history)
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
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    def _emit_progress(node: str, title: str) -> None:
        # Called from the worker thread at node entry; hop back to the event
        # loop so the SSE generator can flush a progress frame immediately
        # instead of waiting for the node to finish.
        loop.call_soon_threadsafe(queue.put_nowait, ("progress", {"node": node, "title": title}))

    def _run_analysis() -> None:
        try:
            agent = DataAnalysisAgent(
                record.workspace,
                settings,
                cancel_event=record.cancel_event,
                progress_callback=_emit_progress,
            )
            final_payload: dict[str, Any] | None = None
            for update in agent.stream(request.task, history=history):
                node = update["node"]
                data = update["data"]
                if node == "finalize":
                    final_payload = data
                loop.call_soon_threadsafe(queue.put_nowait, (node, data))
            if final_payload is None:
                raise RuntimeError("工作流没有返回最终结果。")
            result = AnalysisResult(
                response=final_payload["response"],
                trace=final_payload.get("trace", []),
                artifacts=final_payload.get("artifacts", []),
                dataset_profile=final_payload["dataset_profile"],
                plan=final_payload.get("plan", []),
                completed_steps=final_payload.get("completed_steps", []),
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
            loop.call_soon_threadsafe(queue.put_nowait, ("complete", _result_payload(session_id, result)))
        except AnalysisCancelled:
            record.set_finished("cancelled")
            registry.persist(session_id, record)
            loop.call_soon_threadsafe(queue.put_nowait, ("cancelled", {"message": "分析已取消。"}))
        except Exception as exc:
            record.set_finished("failed")
            registry.persist(session_id, record)
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"message": str(exc)}))
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
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:
                # 事件循环已关闭（客户端断连后 ASGI 关闭 loop），队列无人消费，
                # 直接吞掉异常——slot 和 lock 已释放，状态已 persist，无副作用。
                pass

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
                except TimeoutError:
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
    registry.persist(session_id, record)
    return {"status": "cancelling"}


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

_PREVIEW_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
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
        return Response(
            content=html_text,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"},
        )
    return FileResponse(path, filename=path.name)


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
