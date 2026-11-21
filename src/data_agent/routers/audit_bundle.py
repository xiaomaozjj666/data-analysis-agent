"""零信任证据包：导出一个自包含 ZIP，供独立脚本从头复算与验签。

包内包含原始 CSV、草稿、口径快照、来源血缘、账本、核验结果与 manifest。
独立脚本 ``scripts/independent_replay.py`` 不 import 本项目任何代码，只用
Python 标准库复算指标并重现 audit_id，把「相信服务端」变成「自己算一遍」。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

router = APIRouter()


class BundleRequest(BaseModel):
    draft: str = Field(min_length=1, max_length=6000)
    confirm_full_table: bool = False
    draft_trace_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


_BUILTIN_SCHEMA = {
    "metrics": [
        {
            "name": "销售额合计",
            "column": "sales",
            "aggregation": "sum",
            "unit": "元",
            "decimals": 2,
        },
        {
            "name": "销量合计",
            "column": "quantity",
            "aggregation": "sum",
            "unit": "件",
            "decimals": 0,
            "data_type": "integer",
        },
        {"name": "订单数", "column": "order_id", "aggregation": "count_distinct", "unit": "笔"},
        {
            "name": "客单价",
            "aggregation": "ratio",
            "numerator": "销售额合计",
            "denominator": "订单数",
            "unit": "元",
            "decimals": 2,
        },
    ]
}


def _read_optional(path: Any) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


@router.post("/api/sessions/{session_id}/sales-audit/bundle")
def export_bundle(session_id: str, payload: BundleRequest) -> Response:
    from data_agent import api
    from data_agent.audit_schema import load_schema
    from data_agent.sales_audit import audit_sales_brief

    workspace = api.registry.get(session_id).workspace
    source = workspace.source_path
    if (
        source is None
        or source.suffix.lower() != ".csv"
        or workspace.input_dir.resolve() not in source.resolve().parents
    ):
        raise HTTPException(status_code=422, detail="导出证据包需要会话上传的原始 CSV。")
    try:
        from data_agent.audit_policy import load_policy, policy_fingerprint, rls_value

        metric_specs = load_schema(workspace.root)
        policy = load_policy(workspace.root)
        rls_column, rls_value_matched = rls_value(policy) if policy else (None, None)
        audit = audit_sales_brief(
            source,
            payload.draft,
            confirm_full_table=payload.confirm_full_table,
            max_rows=api.bootstrap_settings.max_rows,
            draft_trace_sha256=payload.draft_trace_sha256,
            metric_specs=metric_specs,
            pii_columns=tuple(policy.get("pii_columns", ())) if policy else (),
            rls_column=rls_column,
            rls_value=rls_value_matched,
            policy_fingerprint=policy_fingerprint(policy) if policy else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail="无法读取原始 CSV，请重新上传数据。") from exc

    schema_bytes = _read_optional(workspace.root / "audit_schema.json")
    if schema_bytes is None:
        # 内置销售口径也要能以声明式形式复现；独立脚本据此从头复算。
        schema_bytes = json.dumps(_BUILTIN_SCHEMA, ensure_ascii=False).encode("utf-8")
    lineage_bytes = _read_optional(workspace.root / "audit_lineage.json")
    ledger_bytes = _read_optional(workspace.root / "audit_ledger.jsonl") or b""
    policy_bytes = _read_optional(workspace.root / "audit_policy.json")
    public_key_bytes = _read_optional(workspace.root / "audit_keys" / "ledger_ed25519.pub")
    trace_bytes = None
    if payload.draft_trace_sha256:
        trace_bytes = _read_optional(
            workspace.root / "draft_traces" / f"{payload.draft_trace_sha256}.json"
        )
        if trace_bytes is None:
            raise HTTPException(status_code=422, detail="找不到对应的起草轨迹，请重新起草。")

    report = {
        "audit_id": audit["audit_id"],
        "schema_version": audit["schema_version"],
        "schema_fingerprint": audit.get("schema_fingerprint"),
        "policy_fingerprint": audit.get("policy_fingerprint"),
        "metrics": audit["metrics"],
        "summary": audit["summary"],
        "claims": audit["claims"],
        "corrected_brief": audit["corrected_brief"],
        "confirmed_full_table": audit["confirmed_full_table"],
        "draft_trace_sha256": payload.draft_trace_sha256,
        "source": {"filename": source.name, "sha256": audit["source"]["sha256"]},
    }
    # source.csv 不读进内存：打包时用 ZipFile.write 直接流式复制文件，
    # 否则接近上传上限（200MB）的会话会让整份 CSV 常驻内存。
    files: dict[str, bytes] = {
        "draft.txt": payload.draft.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"),
        "schema.json": schema_bytes,
        "report.json": (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "ledger.jsonl": ledger_bytes,
    }
    if lineage_bytes is not None:
        files["lineage.json"] = lineage_bytes
    if policy_bytes is not None:
        files["policy.json"] = policy_bytes
    if public_key_bytes is not None:
        files["public_key.pem"] = public_key_bytes
    if trace_bytes is not None:
        files["draft_trace.json"] = trace_bytes
    manifest_files = {name: _sha256(raw) for name, raw in files.items()}
    manifest_files["source.csv"] = audit["source"]["sha256"]
    manifest = {
        "bundle_version": 1,
        "audit_id": audit["audit_id"],
        "files": manifest_files,
    }
    files["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )

    from data_agent._downloads import zip_file_response

    def write_archive(archive) -> None:  # type: ignore[no-untyped-def]
        archive.write(source, arcname="source.csv")
        for name, raw in files.items():
            archive.writestr(name, raw)

    # 证据包可能含大 CSV + 报告 + 账本：写临时文件后流式发送，避免整包驻留内存。
    return zip_file_response(f"evidence-{session_id}.zip", write_archive)
