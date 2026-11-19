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
    ledger_path,
    read_entries,
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
        from data_agent.audit_schema import load_schema, load_schema_document

        metric_specs = load_schema(workspace.root)
        schema_document = load_schema_document(workspace.root)
        from data_agent.audit_policy import (
            load_policy,
            policy_fingerprint,
            rls_filter_hash,
            rls_value,
        )

        policy = load_policy(workspace.root)
        rls_column, rls_value_matched = rls_value(policy) if policy else (None, None)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        result = audit_sales_brief(
            source,
            payload.draft,
            confirm_full_table=payload.confirm_full_table,
            max_rows=api.bootstrap_settings.max_rows,
            draft_trace_sha256=trace_digest,
            metric_specs=metric_specs,
            pii_columns=tuple(policy.get("pii_columns", ())) if policy else (),
            rls_column=rls_column,
            rls_value=rls_value_matched,
            policy_fingerprint=policy_fingerprint(policy) if policy else None,
        )
        result["schema_status"] = (schema_document or {}).get("status")
        policy_sha = policy_fingerprint(policy) if policy else None
        rls_sha = (
            rls_filter_hash(rls_column, rls_value_matched or "")
            if rls_column
            else None
        )
        if policy:
            result["policy"] = {
                "principal_id": policy["principal_id"],
                "pii_columns": list(policy.get("pii_columns", ())),
                "rls_column": rls_column,
                "sha256": policy_sha,
            }
        from data_agent.audit_lineage import freshness, lineage_fingerprint, load_lineage

        lineage = load_lineage(workspace.root)
        lineage_sha = None
        if lineage:
            lineage_sha = lineage_fingerprint(lineage)
            state = freshness(lineage)
            result["lineage"] = {**lineage, **state, "sha256": lineage_sha}
            if state["stale"]:
                result.setdefault("warnings", []).append(
                    {
                        "code": "source_stale",
                        "message": (
                            "上游数据已超过新鲜度 SLA："
                            f"{state['age_hours']} 小时 > {state['freshness_sla_hours']} 小时。"
                        ),
                        "age_hours": state["age_hours"],
                        "freshness_sla_hours": state["freshness_sla_hours"],
                    }
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
                "lineage_sha256": lineage_sha,
                "principal_id": policy["principal_id"] if policy else None,
                "policy_sha256": policy_sha,
                "rls_filter_hash": rls_sha,
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


@router.get("/api/sessions/{session_id}/sales-audit/signature-check")
def check_signatures(session_id: str) -> dict[str, Any]:
    """按会话公钥复核每条账本记录的 Ed25519 签名。"""
    from data_agent import api
    from data_agent.audit_signing import public_key_bytes, verify_entries_signatures

    workspace = api.registry.get(session_id).workspace
    try:
        entries = read_entries(ledger_path(workspace.root))
        if not entries:
            raise HTTPException(status_code=404, detail="账本还没有记录。")
        public_pem = public_key_bytes(workspace.root)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = verify_entries_signatures(entries, public_pem)
    return {
        "valid": result["valid"],
        "signed": result["signed"],
        "entries": len(entries),
        "errors": result["errors"],
    }


@router.get("/api/sessions/{session_id}/sales-audit/signature-tamper-demo")
def tamper_signature_demo(session_id: str) -> dict[str, Any]:
    """只改内存中的签名字节并验签，不修改磁盘账本。"""
    import base64

    from data_agent import api
    from data_agent.audit_signing import public_key_bytes, verify_entries_signatures

    workspace = api.registry.get(session_id).workspace
    try:
        entries = read_entries(ledger_path(workspace.root))
        if not entries:
            raise HTTPException(status_code=404, detail="账本还没有记录。")
        public_pem = public_key_bytes(workspace.root)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tampered = [dict(entry) for entry in entries]
    signature = str(tampered[0].get("signature") or "")
    try:
        raw = bytearray(base64.b64decode(signature))
        raw[0] ^= 1
        tampered[0]["signature"] = base64.b64encode(raw).decode("ascii")
    except (ValueError, TypeError, IndexError) as exc:
        raise HTTPException(status_code=422, detail="账本没有可篡改的签名。") from exc
    result = verify_entries_signatures(tampered, public_pem)
    return {
        "valid": result["valid"],
        "errors": result["errors"][:3],
        "note": "仅改动内存副本，磁盘账本未变。",
    }
