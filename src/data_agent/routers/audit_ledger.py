"""经营简报的服务端确认门与哈希链账本路由。

确认不是前端的本地状态：服务端会按会话里保存的原始 CSV 重新复算，只有重算出的
``audit_id`` 与客户端提交的一致才允许发布，并把源文件、草稿、核验版简报的哈希写进
只追加账本。这样「改了草稿再点发布」或「改了 CSV」都会在服务端被拦下。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from data_agent.audit_ledger import (
    LedgerError,
    append_entry,
    recent_entries,
    sha256_text,
)

router = APIRouter()


class ConfirmRequest(BaseModel):
    draft: str = Field(min_length=1, max_length=6000)
    audit_id: str = Field(min_length=8, max_length=64, pattern=r"^aud-[0-9a-f]+$")
    confirm_full_table: bool = False
    draft_trace_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _workspace_and_source(session_id: str) -> tuple[Any, Any]:
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    source = workspace.source_path
    if (
        source is None
        or source.suffix.lower() != ".csv"
        or workspace.input_dir.resolve() not in source.resolve().parents
    ):
        raise HTTPException(status_code=422, detail="经营简报核验需要会话上传的原始 CSV。")
    return workspace, source


@router.post("/api/sessions/{session_id}/sales-audit/confirm")
def confirm_brief(session_id: str, payload: ConfirmRequest) -> dict[str, Any]:
    """服务端复算并比对 audit_id，通过后写入确认记录并返回权威简报。"""
    from data_agent import api
    from data_agent.sales_audit import audit_sales_brief

    workspace, source = _workspace_and_source(session_id)
    if not payload.draft.strip():
        raise HTTPException(status_code=422, detail="请输入待确认的简报草稿。")
    trace_digest = payload.draft_trace_sha256
    if trace_digest:
        from data_agent.audit_draft import TRACE_DIR_NAME, QuickDraftError, verify_trace

        try:
            verify_trace(workspace.root / TRACE_DIR_NAME, trace_digest, payload.draft)
        except QuickDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        from data_agent.audit_schema import load_schema

        metric_specs = load_schema(workspace.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = audit_sales_brief(
            source,
            payload.draft,
            confirm_full_table=payload.confirm_full_table,
            max_rows=api.bootstrap_settings.max_rows,
            draft_trace_sha256=trace_digest,
            metric_specs=metric_specs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail="无法读取原始 CSV，请重新上传数据。") from exc

    if result["audit_id"] != payload.audit_id:
        raise HTTPException(
            status_code=409,
            detail="草稿或数据在核验后发生变化，请重新核验再确认。",
        )
    brief = result["corrected_brief"]
    if brief is None:
        raise HTTPException(status_code=422, detail="没有可发布的核验结论，请先修正草稿。")
    try:
        entry = append_entry(
            workspace.root,
            kind="confirm",
            session_id=session_id,
            payload={
                "audit_id": result["audit_id"],
                "source_sha256": result["source"]["sha256"],
                "source_filename": result["source"]["filename"],
                "draft_sha256": sha256_text(payload.draft),
                "draft_trace_sha256": trace_digest,
                "schema_fingerprint": result.get("schema_fingerprint"),
                "brief_sha256": sha256_text(brief),
                "confirm_full_table": payload.confirm_full_table,
                "summary": result["summary"],
            },
        )
    except LedgerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "confirmed": True,
        "audit_id": result["audit_id"],
        "brief": brief,
        "source": result["source"],
        "summary": result["summary"],
        "receipt": {
            "seq": entry["seq"],
            "ts": entry["ts"],
            "hash": entry["hash"],
            "prev": entry["prev"],
            "brief_sha256": entry["brief_sha256"],
            "draft_trace_sha256": entry.get("draft_trace_sha256"),
        },
    }


@router.get("/api/sessions/{session_id}/sales-audit/ledger")
def get_ledger(session_id: str, limit: int = 50) -> dict[str, Any]:
    """返回最近的账本记录与整链校验结果（不含草稿正文）。"""
    from data_agent import api

    workspace = api.registry.get(session_id).workspace
    try:
        return recent_entries(workspace.root, limit=max(1, min(limit, 200)))
    except LedgerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
