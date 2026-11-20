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


class SwitchRequest(BaseModel):
    principal_id: str = Field(min_length=1, max_length=120)


@router.post("/api/sessions/{session_id}/audit-policy/switch")
def switch_demo_principal(session_id: str, payload: SwitchRequest) -> dict[str, Any]:
    """切换演示主体：保存策略、重算预分析，并返回策略过滤后的预览。"""
    from data_agent import api
    from data_agent.audit_policy import rls_value
    from data_agent.demo_catalog import DEMOS
    from data_agent.demo_insights import insights_for

    workspace = api.registry.get(session_id).workspace
    demo = DEMOS["regional_policy"]
    selected = next(
        (item for item in demo.principals if item["id"] == payload.principal_id), None
    )
    if selected is None:
        raise HTTPException(status_code=404, detail="未知的演示主体。")
    policy = save_policy(workspace.root, selected["policy"])
    _, _ = rls_value(policy)
    source = workspace.source_path
    if source is None:
        raise HTTPException(status_code=422, detail="会话没有原始 CSV。")
    try:
        csv_text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from data_agent._registry_payloads import _session_payload

    record = api.registry.get(session_id)
    session_payload = _session_payload(session_id, record)
    return {
        "policy": policy,
        "insights": insights_for("regional_policy", csv_text, policy),
        "preview": session_payload["preview"],
        "profile": session_payload["profile"],
        "active": selected["id"],
    }
