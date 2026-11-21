"""会话级访问策略：PII 脱敏列与行级权限。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from data_agent.audit_policy import (
    load_policy,
    policy_fingerprint,
    save_policy,
)

router = APIRouter()


def _demo_kind(root: Path) -> str | None:
    """读取会话的演示类型；非演示会话返回 None，损坏文件返回 422。"""
    path = root / "demo.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"无法读取演示元数据：{exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="演示元数据格式无效。")
    kind = str(payload.get("kind") or "").strip()
    return kind or None


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


@router.delete("/api/sessions/{session_id}/audit-policy", status_code=204)
def delete_audit_policy(session_id: str) -> Response:
    """清除本会话的访问策略（只删本模块管理的 audit_policy.json）。"""
    from data_agent import api
    from data_agent.audit_policy import POLICY_FILENAME

    workspace = api.registry.get(session_id).workspace
    (workspace.root / POLICY_FILENAME).unlink(missing_ok=True)
    return Response(status_code=204)


@router.post("/api/sessions/{session_id}/audit-policy/switch")
def switch_demo_principal(session_id: str, payload: SwitchRequest) -> dict[str, Any]:
    """切换演示主体：保存策略、重算预分析，并返回策略过滤后的预览。"""
    from data_agent import api
    from data_agent.audit_policy import rls_value
    from data_agent.demo_catalog import DEMOS
    from data_agent.demo_insights import insights_for

    workspace = api.registry.get(session_id).workspace
    # 主体与演示类型必须来自本会话的 demo.json，不能把区域策略硬编码后
    # 写进财报/成绩等其它演示会话。
    kind = _demo_kind(workspace.root)
    if kind is None:
        raise HTTPException(status_code=404, detail="该会话不是演示数据，无法切换主体。")
    demo = DEMOS.get(kind)
    if demo is None:
        raise HTTPException(status_code=404, detail="未知的演示类型。")
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
        "insights": insights_for(kind, csv_text, policy),
        "preview": session_payload["preview"],
        "profile": session_payload["profile"],
        "active": selected["id"],
    }
