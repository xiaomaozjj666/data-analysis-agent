"""Recalculate a sales brief directly from the original CSV.

金额允许负数（退款/冲销），订单号按原样去重（含尾随空格视为不同订单）。
行数上限由调用方传入（复用上传时的工作区 max_rows），避免绕过导入护栏。
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from data_agent.provenance import sha256_file

_COLUMNS = ("order_id", "sales", "quantity")
_RULES = {
    "销售额合计": ("元", "SUM(sales)，按分精确求和"),
    "销量合计": ("件", "SUM(quantity)"),
    "订单数": ("笔", "COUNT(DISTINCT order_id)，同一订单的多条明细只计一笔"),
}
_METRIC_LINE = re.compile(
    r" {0,3}(?:[-*] )?(?P<label>[^\s：:#>~`][^：:]*?)[：:]\s*(?P<value>.+?)\s*"
)
_AMOUNT = re.compile(r"[+-]?[0-9]{1,16}(?:\.[0-9]{1,2})?")
_INTEGER = re.compile(r"[0-9]{1,18}")
_DRAFT_AMOUNT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]{1,2})?")
_DRAFT_INTEGER = re.compile(r"[0-9]+")
_OTHER_NUMERIC_CLAIM = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\s*(?:%|％|元|件|笔|人)")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_EVIDENCE_ROWS = 6


def _read_sales(
    source: Path, *, max_rows: int | None = None
) -> tuple[dict[str, str], dict[str, Any]]:
    digest = sha256_file(source)
    orders: set[str] = set()
    sales_cents = units = row_count = 0
    preview = []
    try:
        with source.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            headers = reader.fieldnames or []
            if any(headers.count(column) != 1 for column in _COLUMNS):
                raise ValueError("CSV 必须各有一列 order_id、sales、quantity。")
            for row_number, row in enumerate(reader, start=1):
                if max_rows is not None and row_number > max_rows:
                    raise ValueError(f"CSV 超过 {max_rows} 行上限，请拆分或先用筛选视图收敛。")
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"第 {row_number} 条记录的列数和表头不一致。")
                order = row["order_id"]
                amount, quantity = row["sales"].strip(), row["quantity"].strip()
                if not order.strip():
                    raise ValueError(f"第 {row_number} 条记录的 order_id 为空。")
                if not _AMOUNT.fullmatch(amount):
                    raise ValueError(f"第 {row_number} 条记录的 sales 必须是元，最多两位小数。")
                if not _INTEGER.fullmatch(quantity):
                    raise ValueError(f"第 {row_number} 条记录的 quantity 必须是非负整数。")
                sales_cents += int(Decimal(amount) * 100)
                units += int(quantity)
                orders.add(order)
                row_count = row_number
                if row_count <= _EVIDENCE_ROWS:
                    preview.append(
                        {
                            "record": row_count,
                            "order_id": order,
                            "sales": amount,
                            "quantity": quantity,
                        }
                    )
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("核验使用 UTF-8 编码、逗号分隔的 CSV。请检查编码和引号。") from exc
    if not row_count:
        raise ValueError("CSV 至少需要一条订单明细。")
    if digest != sha256_file(source):
        raise ValueError("核验期间源文件发生变化，请重试。")

    metrics = {
        "销售额合计": f"{Decimal(sales_cents).scaleb(-2):.2f}",
        "销量合计": str(units),
        "订单数": str(len(orders)),
    }
    return metrics, {
        "filename": source.name,
        "sha256": digest,
        "rows": row_count,
        "preview": preview,
    }


def _audit_claims(
    draft: str,
    metrics: dict[str, str],
    *,
    confirm_full_table: bool,
) -> tuple[list[dict[str, Any]], bool]:
    claims = []
    fence = ""
    has_context = False
    for line, raw in enumerate(draft.splitlines(), start=1):
        if not raw.strip():
            continue
        delimiter = _FENCE.fullmatch(raw)
        match = _METRIC_LINE.fullmatch(raw) if not (fence or delimiter) else None
        if delimiter:
            marker, tail = delimiter.groups()
            if not fence:
                fence = marker
            elif marker.startswith(fence) and not tail.strip():
                fence = ""
        label = match["label"].strip().removeprefix("**").removesuffix("**") if match else ""
        value = match["value"].removeprefix("**").removesuffix("**").strip() if match else ""
        claim: dict[str, Any] = {
            "line": line,
            "text": raw,
            "metric": None,
            "status": "unsupported",
            "reason": "无法按已登记的指标口径核验这行草稿。",
        }
        if label in _RULES:
            unit, formula = _RULES[label]
            number = value.removesuffix(unit).strip()
            valid = _DRAFT_AMOUNT if unit == "元" else _DRAFT_INTEGER
            claim.update(metric=label, formula=formula)
            if value.endswith(unit) and valid.fullmatch(number):
                expected = metrics[label]
                agrees = Decimal(number) == Decimal(expected)
                claim.update(
                    status="verified" if agrees else "conflict",
                    submitted=value,
                    recalculated=f"{expected} {unit}",
                    reason="原值与明细复算一致。" if agrees else "原值与明细复算不一致。",
                )
            else:
                claim["reason"] = f"数值格式或单位不符合口径，此项使用「{unit}」。"
        elif not _OTHER_NUMERIC_CLAIM.fullmatch(value):
            has_context = True
            claim["reason"] = "含标题、说明或非独立指标行，需人工确认统计范围。"
        claims.append(claim)

    counts = Counter(claim["metric"] for claim in claims if claim["metric"])
    needs_scope_confirmation = False
    for claim in claims:
        if claim["metric"] is None:
            continue
        if counts[claim["metric"]] > 1:
            claim.update(status="unsupported", reason="同一指标出现多次，请确认要核验的原值。")
            claim.pop("recalculated", None)
        elif has_context and not confirm_full_table and claim["status"] != "unsupported":
            needs_scope_confirmation = True
            claim.update(status="unsupported", reason="草稿含上下文，请先确认这项指标的统计范围。")
            claim.pop("recalculated", None)
    return claims, needs_scope_confirmation


def audit_sales_brief(
    source: Path,
    draft: str,
    *,
    confirm_full_table: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    metrics, evidence = _read_sales(source, max_rows=max_rows)
    claims, needs_scope_confirmation = _audit_claims(
        draft,
        metrics,
        confirm_full_table=confirm_full_table,
    )
    summary = {
        key: sum(claim["status"] == key for claim in claims)
        for key in ("verified", "conflict", "unsupported")
    }
    publishable = [claim for claim in claims if claim["status"] in ("verified", "conflict")]
    brief = None
    if publishable:
        lines = [
            "# 经营简报（核验版）",
            "",
            f"全表统计，共 {evidence['rows']} 条订单明细。",
            "",
            *(f"- {claim['metric']}：{claim['recalculated']}" for claim in publishable),
            "",
            "## 复算依据",
            "",
            *(f"- {claim['metric']}：{claim['formula']}" for claim in publishable),
            f"- 原始 CSV 的 SHA-256：`{evidence['sha256']}`",
        ]
        if summary["unsupported"]:
            lines.append(f"- 暂缓发布 {summary['unsupported']} 行，草稿原文见核验明细。")
        brief = "\n".join(lines) + "\n"
    return {
        "schema_version": 1,
        "source": evidence,
        "summary": summary,
        "claims": claims,
        "needs_scope_confirmation": needs_scope_confirmation,
        "confirmed_full_table": confirm_full_table,
        "corrected_brief": brief,
    }
