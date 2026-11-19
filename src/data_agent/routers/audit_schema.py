"""会话级核验口径：登记声明式指标，供核验器独立复算。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from data_agent.audit_schema import load_schema, save_schema

router = APIRouter()


class AuditSchemaPayload(BaseModel):
    metrics: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


def _serialize(specs) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "column": spec.column,
            "aggregation": spec.aggregation,
            "unit": spec.unit,
            "decimals": spec.decimals,
            "aliases": list(spec.aliases),
        }
        for spec in specs
    ]


@router.get("/api/sessions/{session_id}/audit-schema")
def get_audit_schema(session_id: str) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        specs = load_schema(workspace.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"metrics": _serialize(specs or []), "source": None if specs is None else "workspace"}


@router.put("/api/sessions/{session_id}/audit-schema")
def put_audit_schema(session_id: str, payload: AuditSchemaPayload) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        specs = save_schema(workspace.root, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"metrics": _serialize(specs), "source": "workspace"}
