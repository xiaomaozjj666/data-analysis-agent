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
        self.analysis_status = "idle"
        self.current_task = ""
        self.created_at = time.time()


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
        payload = {
            "id": session_id,
            "filename": record.workspace.source_path.name if record.workspace.source_path else "dataset",
            "chat": record.chat[-40:],
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
        workspace.restore_artifacts()
        record = SessionRecord(workspace)
        manifest = root / "session.json"
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                record.chat = [
                    item for item in payload.get("chat", [])
                    if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
                ][-40:]
                record.created_at = float(payload.get("created_at", record.created_at))
            except (OSError, ValueError, TypeError):
                pass
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


def _client_identifier(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Prefer the first hop of ``X-Forwarded-For`` when the request reaches us
    through a trusted reverse proxy (Render, Nginx, etc.). Fall back to the
    direct socket address so the limiter always has a stable key.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    if request.client:
        return request.client.host
    return "unknown"


def _check_rate_limit(request: Request) -> None:
    if request.method not in {"POST", "PUT", "DELETE"} or not request.url.path.startswith("/api/"):
        return
    limit = bootstrap_settings.rate_limit_per_minute
    now = time.monotonic()
    key = _client_identifier(request)
    with request_buckets_lock:
        bucket = request_buckets[key]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试。")
        bucket.append(now)


@app.middleware("http")
async def protect_api(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in {"/api/health", "/api/auth"}:
        try:
            _check_access(request)
            _check_rate_limit(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


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

    preferred = [
        item
        for item in datasets
        if re.search(r"final|result|report|cleaned_data_final|analysis_result", item.get("name", ""), re.I)
    ]
    selected_datasets = preferred[-2:] if preferred else datasets[-1:]
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


@app.post("/api/sessions", status_code=201)
async def create_session(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    settings = AgentSettings.from_env(provider="deepseek")
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
    except (ValueError, OSError) as exc:
        workspace.cleanup()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    record.analysis_status = "running"
    record.current_task = request.task
    try:
        agent = DataAnalysisAgent(record.workspace, settings, cancel_event=record.cancel_event)
        result = agent.run(request.task, history=history)
        record.analysis_status = "completed"
    except AnalysisCancelled as exc:
        record.analysis_status = "cancelled"
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        record.analysis_status = "failed"
        raise HTTPException(status_code=502, detail=f"分析执行失败：{exc}") from exc
    finally:
        record.current_task = ""
        analysis_slots.release()
        record.run_lock.release()
    record.chat.extend(
        [
            {"role": "user", "content": request.task},
            {"role": "assistant", "content": result.response},
        ]
    )
    record.last_result = result
    registry.persist(session_id, record)
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
    record.analysis_status = "running"
    record.current_task = request.task

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    def _run_analysis() -> None:
        try:
            agent = DataAnalysisAgent(
                record.workspace,
                settings,
                cancel_event=record.cancel_event,
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
            registry.persist(session_id, record)
            record.analysis_status = "completed"
            loop.call_soon_threadsafe(queue.put_nowait, ("complete", _result_payload(session_id, result)))
        except AnalysisCancelled:
            record.analysis_status = "cancelled"
            loop.call_soon_threadsafe(queue.put_nowait, ("cancelled", {"message": "分析已取消。"}))
        except Exception as exc:
            record.analysis_status = "failed"
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"message": str(exc)}))
        finally:
            record.current_task = ""
            loop.call_soon_threadsafe(queue.put_nowait, None)
            analysis_slots.release()
            record.run_lock.release()

    worker = threading.Thread(target=_run_analysis, name=f"analysis-{session_id}", daemon=True)

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
            record.analysis_status = "cancelling"
            raise
        finally:
            if worker.is_alive():
                worker.join(timeout=0.1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/cancel")
def cancel_analysis(session_id: str) -> dict[str, str]:
    record = registry.get(session_id)
    if not record.run_lock.locked():
        return {"status": record.analysis_status}
    record.cancel_event.set()
    record.analysis_status = "cancelling"
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


def _standalone_visualization(record: SessionRecord, path: Path) -> str:
    """Inline the shared Plotly runtime when a chart is viewed or downloaded."""
    html_text = path.read_text(encoding="utf-8")
    bundle_path = record.workspace.artifacts_dir / PLOTLY_BUNDLE_NAME
    if not bundle_path.is_file():
        return html_text
    plotly_js = bundle_path.read_text(encoding="utf-8")
    for tag in (
        f"<script src='{PLOTLY_BUNDLE_NAME}'></script>",
        f'<script src="{PLOTLY_BUNDLE_NAME}"></script>',
    ):
        if tag in html_text:
            return html_text.replace(tag, f"<script>{plotly_js}</script>", 1)
    return html_text


@app.get("/api/sessions/{session_id}/artifacts/{filename}/preview")
def preview_artifact(session_id: str, filename: str) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() != ".html":
        raise HTTPException(status_code=415, detail="该产物不支持在线预览。")
    return Response(
        content=_standalone_visualization(record, path),
        media_type="text/html",
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/api/sessions/{session_id}/artifacts/{filename}")
def download_artifact(session_id: str, filename: str) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() == ".html":
        return Response(
            content=_standalone_visualization(record, path),
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
