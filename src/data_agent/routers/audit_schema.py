"""会话级核验口径：登记声明式指标，供核验器独立复算。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from data_agent.audit_schema import (
    HISTORY_FILENAME,
    approve_schema,
    load_schema,
    load_schema_document,
    parse_schema,
    save_schema,
    schema_fingerprint,
    serialize_metrics,
)

router = APIRouter()


class AuditSchemaPayload(BaseModel):
    metrics: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    author: str = Field(default="", max_length=80)


class ApproveSchemaPayload(BaseModel):
    approved_by: str = Field(min_length=1, max_length=80)


@router.get("/api/sessions/{session_id}/audit-schema")
def get_audit_schema(session_id: str) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        document = load_schema_document(workspace.root)
        specs = load_schema(workspace.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "metrics": serialize_metrics(specs or []),
        "source": None if document is None else "workspace",
        "status": (document or {}).get("status"),
        "author": (document or {}).get("author"),
        "approved_by": (document or {}).get("approved_by"),
        "approved_at": (document or {}).get("approved_at"),
    }


@router.get("/api/sessions/{session_id}/audit-schema/history")
def get_audit_schema_history(session_id: str, limit: int = 20) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    path = workspace.root / HISTORY_FILENAME
    if not path.is_file():
        return {"entries": [], "consistent": True}
    entries: list[dict[str, Any]] = []
    consistent = True
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            consistent = False
            continue
        try:
            fingerprint = schema_fingerprint(
                parse_schema({"metrics": entry.get("metrics", [])})
            )
        except ValueError:
            fingerprint = None
        entry["fingerprint_valid"] = fingerprint == entry.get("sha256")
        consistent = consistent and entry["fingerprint_valid"]
        entries.append(entry)
    return {"entries": entries[-max(1, min(limit, 100)) :], "consistent": consistent}


@router.post("/api/sessions/{session_id}/audit-schema/approve")
def approve_audit_schema(session_id: str, payload: ApproveSchemaPayload) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        specs = approve_schema(workspace.root, payload.approved_by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "metrics": serialize_metrics(specs),
        "status": "approved",
        "approved_by": payload.approved_by,
    }


@router.put("/api/sessions/{session_id}/audit-schema")
def put_audit_schema(session_id: str, payload: AuditSchemaPayload) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        specs = save_schema(workspace.root, payload.model_dump(), author=payload.author)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"metrics": serialize_metrics(specs), "source": "workspace", "status": "draft"}
