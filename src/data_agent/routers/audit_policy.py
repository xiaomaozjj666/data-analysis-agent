"""会话级访问策略：PII 脱敏列与行级权限。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from data_agent.audit_policy import (
    load_policy,
    policy_fingerprint,
    save_policy,
)

router = APIRouter()


class PolicyPayload(BaseModel):
    principal_id: str = Field(min_length=1, max_length=120)
    claims: dict[str, str] = Field(default_factory=dict)
    pii_columns: list[str] = Field(default_factory=list)
    rls: dict[str, str] | None = None


@router.get("/api/sessions/{session_id}/audit-policy")
def get_audit_policy(session_id: str) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        policy = load_policy(workspace.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if policy is None:
        return {"policy": None, "sha256": None}
    return {"policy": policy, "sha256": policy_fingerprint(policy)}


@router.put("/api/sessions/{session_id}/audit-policy")
def put_audit_policy(session_id: str, payload: PolicyPayload) -> dict[str, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        policy = save_policy(workspace.root, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"policy": policy, "sha256": policy_fingerprint(policy)}
