from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from data_agent.agent import AnalysisResult, DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.credentials import delete_saved_api_key, get_saved_api_key, save_api_key
from data_agent.serialization import to_jsonable
from data_agent.workspace import DataWorkspace

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class SettingsUpdate(BaseModel):
    api_key: str | None = None
    thinking_enabled: bool = True
    reasoning_effort: str = Field(default="high", pattern="^(high|max)$")
    persist_key: bool = True


class AnalyzeRequest(BaseModel):
    task: str = Field(min_length=1, max_length=8000)


class SessionRecord:
    def __init__(self, workspace: DataWorkspace) -> None:
        self.workspace = workspace
        self.chat: list[dict[str, str]] = []
        self.last_result: AnalysisResult | None = None


class SessionRegistry:
    def __init__(self) -> None:
        self._items: dict[str, SessionRecord] = {}
        self._lock = threading.RLock()

    def create(self, workspace: DataWorkspace) -> tuple[str, SessionRecord]:
        session_id = workspace.root.name
        record = SessionRecord(workspace)
        with self._lock:
            self._items[session_id] = record
        return session_id, record

    def get(self, session_id: str) -> SessionRecord:
        with self._lock:
            record = self._items.get(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="分析会话不存在或服务已经重启。")
        return record


registry = SessionRegistry()
runtime_settings = {
    "api_key": "",
    "thinking_enabled": None,
    "reasoning_effort": None,
}

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


def _effective_settings() -> AgentSettings:
    settings = AgentSettings.from_env(provider="deepseek")
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


def _artifact_payload(session_id: str, artifacts: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in artifacts:
        value = dict(item)
        value["download_url"] = f"/api/sessions/{session_id}/artifacts/{item['name']}"
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
    return {"status": "ok", "architecture": "plan-and-execute-react"}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = _effective_settings()
    return {
        "provider": "deepseek",
        "model": settings.model,
        "base_url": settings.base_url,
        "configured": bool(settings.api_key),
        "thinking_enabled": settings.thinking_enabled,
        "reasoning_effort": settings.reasoning_effort,
        "langsmith_tracing": os.getenv("LANGSMITH_TRACING", "false").lower() == "true",
        "langsmith_project": os.getenv("LANGSMITH_PROJECT", "data-analysis-agent"),
    }


@app.put("/api/settings")
def update_settings(update: SettingsUpdate) -> dict[str, Any]:
    if update.api_key is not None:
        value = update.api_key.strip()
        if not value:
            raise HTTPException(status_code=422, detail="API Key 不能为空。")
        runtime_settings["api_key"] = value
        if update.persist_key:
            save_api_key(value)
    runtime_settings["thinking_enabled"] = update.thinking_enabled
    runtime_settings["reasoning_effort"] = update.reasoning_effort
    return get_settings()


@app.delete("/api/settings/key")
def delete_key() -> dict[str, bool]:
    runtime_settings["api_key"] = ""
    delete_saved_api_key()
    return {"configured": bool(AgentSettings.from_env(provider="deepseek").api_key)}


@app.post("/api/sessions", status_code=201)
async def create_session(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件不能超过 200MB。")
    if not content:
        raise HTTPException(status_code=422, detail="上传文件为空。")
    settings = AgentSettings.from_env(provider="deepseek")
    session_id = f"api_{uuid4().hex[:12]}"
    workspace = DataWorkspace(settings.runs_dir, session_id=session_id)
    try:
        saved = workspace.save_upload(file.filename or "dataset.csv", content)
        workspace.load(saved)
    except (ValueError, OSError) as exc:
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
    agent = DataAnalysisAgent(record.workspace, settings)
    try:
        result = agent.run(request.task, history=history)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"分析执行失败：{exc}") from exc
    record.chat.extend(
        [
            {"role": "user", "content": request.task},
            {"role": "assistant", "content": result.response},
        ]
    )
    record.last_result = result
    return _result_payload(session_id, result)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(to_jsonable(data), ensure_ascii=False)}\n\n"


@app.post("/api/sessions/{session_id}/analyze/stream")
def analyze_stream(session_id: str, request: AnalyzeRequest) -> StreamingResponse:
    record = registry.get(session_id)
    settings = _effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    history = _history(record)

    def generate():
        yield _sse("started", {"task": request.task})
        try:
            agent = DataAnalysisAgent(record.workspace, settings)
            final_payload: dict[str, Any] | None = None
            for update in agent.stream(request.task, history=history):
                node = update["node"]
                data = update["data"]
                if node == "finalize":
                    final_payload = data
                yield _sse(node, data)
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
            yield _sse("complete", _result_payload(session_id, result))
        except Exception as exc:
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions/{session_id}/artifacts/{filename}")
def download_artifact(session_id: str, filename: str) -> FileResponse:
    record = registry.get(session_id)
    matches = [item for item in record.workspace.artifacts if item["name"] == Path(filename).name]
    if not matches:
        raise HTTPException(status_code=404, detail="产物不存在。")
    path = Path(matches[0]["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="产物文件已被移除。")
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
