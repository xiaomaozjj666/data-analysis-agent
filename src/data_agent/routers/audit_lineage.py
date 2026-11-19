"""来源目录血缘：登记与读取上传 CSV 的上游表、抽取时间与新鲜度 SLA。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from data_agent.audit_lineage import (
    freshness,
    lineage_fingerprint,
    load_lineage,
    save_lineage,
)

router = APIRouter()


class LineagePayload(BaseModel):
    catalog_urn: str = Field(default="", max_length=300)
    upstream_table: str = Field(default="", max_length=300)
    extracted_at: str = Field(min_length=1, max_length=64)
    owner: str = Field(default="", max_length=300)
    freshness_sla_hours: int = Field(default=24, ge=1, le=8760)


@router.get("/api/sessions/{session_id}/audit-lineage")
def get_audit_lineage(session_id: str) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        lineage = load_lineage(workspace.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if lineage is None:
        return {"lineage": None, "sha256": None}
    return {
        "lineage": {**lineage, **freshness(lineage)},
        "sha256": lineage_fingerprint(lineage),
    }


@router.put("/api/sessions/{session_id}/audit-lineage")
def put_audit_lineage(session_id: str, payload: LineagePayload) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        lineage = save_lineage(workspace.root, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "lineage": {**lineage, **freshness(lineage)},
        "sha256": lineage_fingerprint(lineage),
    }
