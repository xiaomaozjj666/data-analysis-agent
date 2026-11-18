"""用原始 CSV 独立复算经营简报草稿，逐行给出 verified / conflict / unsupported。

核验原则：宁可 unsupported 也不误判 verified；任何归一化都写明判定过程；不替用户猜口径
（只给候选，由 label_choices 显式采纳）；模型不参与算数，数字全部由本模块按行复算。

读取与口径：
- 源文件只读一次，SHA-256 与解码、CSV 解析都基于同一份字节快照。
- 编码支持 UTF-8（含 BOM）与 GB18030（中文 Excel 常见导出编码）。
- 表头允许 订单号/金额/数量 等中文别名，仅当每个标准列恰好唯一匹配才映射。
- 金额允许负数（退款/冲销）；订单号按原样去重（含尾随空格视为不同订单），
  近似重复（NFKC、大小写、零宽字符、前导 0）只写入 warnings，不改口径。
- 行数上限由调用方传入（复用上传时的工作区 max_rows），避免绕过导入护栏。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

_COLUMNS = ("order_id", "sales", "quantity")
_COLUMN_ALIASES = {
    "order_id": ("order_id", "orderid", "订单号", "订单编号", "订单id", "订单"),
    "sales": ("sales", "amount", "金额", "销售额", "订单金额", "销售金额"),
    "quantity": ("quantity", "qty", "数量", "销量", "件数"),
}
_RULES: dict[str, tuple[str, str]] = {
    "销售额合计": ("元", "SUM(sales)，按分精确求和"),
    "销量合计": ("件", "SUM(quantity)"),
    "订单数": ("笔", "COUNT(DISTINCT order_id)，同一订单的多条明细只计一笔"),
    "客单价": ("元", "销售额合计 ÷ 订单数，按分四舍五入（HALF_UP）"),
}
# 依赖订单数的指标：订单号近似重复会让它们的更正值不可信。
_ORDER_BASED = frozenset({"订单数", "客单价"})
_CENT_METRICS = frozenset({"客单价"})

_METRIC_LINE = re.compile(
    r" {0,3}(?:[-*] )?(?P<label>[^\s：:#>~`][^：:]*?)[：:]\s*(?P<value>.+?)\s*"
)
_AMOUNT = re.compile(r"[+-]?[0-9]{1,16}(?:\.[0-9]{1,2})?")
_INTEGER = re.compile(r"[0-9]{1,18}")
_SALES_CELL = re.compile(
    r"(?P<s1>[+-]?)(?P<currency>[¥￥]?)(?P<s2>[+-]?)"
    r"(?P<whole>[1-9][0-9]{0,2}(?:,[0-9]{3})+|[0-9]+)(?P<frac>\.[0-9]+)?"
)
_DRAFT_AMOUNT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]{1,2})?")
_DRAFT_INTEGER = re.compile(r"[0-9]+")
# 非登记标签行里，只有 % 与「人」和统计范围无关，可以豁免范围门；元/件/笔 必须过范围门。
_OTHER_NUMERIC_CLAIM = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\s*(?:%|％|人)")
_FENCE = re.compile(r" {0,3}(?P<marker>`{3,}|~{3,})(?P<info>[^`~]*)")
_INTENT = re.compile(
    r"最高|最低|最好|最差|最大|最小|最多|最少|平均|均值|中位|人均|日均|月均|同比|环比|占比"
    r"|排名|增长|增速|增幅|下降|降幅|涨幅|按|前\s*[0-9一二三四五六七八九十]+|(?<![a-z])top(?![a-z])",
    re.IGNORECASE,
)
_SUMMARY_IDS = frozenset({"合计", "总计", "小计", "total", "subtotal", "grandtotal"})
_CURRENCY_PREFIX = str.maketrans("", "", "¥￥")
_BLANK_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})
_BLANK_GLYPHS = frozenset("ᅟᅠ⠀ㅤﾠ")
_ENCODING_HINT = "核验使用 UTF-8 或 GB18030 编码、逗号分隔的 CSV。请检查编码和引号。"
_COLUMN_HINT = "CSV 必须各有一列 order_id、sales、quantity（也接受 订单号/金额/数量 等中文表头）。"
_DEFAULT_REASON = "无法按已登记的指标口径核验这行草稿。"
_CONTEXT_REASON = "含标题、说明或非独立指标行，需人工确认统计范围。"
_FENCE_REASON = "代码块内的示例不作为指标核验。"
_SCOPED_REASON = "指标带有范围或分组限定，不能按原始 CSV 全表核验。"
_DUPLICATE_REASON = "同一指标出现多次，请确认要核验的原值。"
_SUGGEST_THRESHOLD = 0.5
_SUGGEST_LIMIT = 3
_PREVIEW_ROWS = 6
_EVIDENCE_CAP = 200
_SAMPLE_CAP = 5


@dataclass
class _ReadResult:
    metrics: dict[str, str]
    evidence: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _normalize_blank(value: str) -> str:
    """去掉零宽字符与不可见空白，用于近似重复判定（不改原始订单号）。"""
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value)
        if char not in _BLANK_GLYPHS
        and unicodedata.category(char) not in _BLANK_CATEGORIES
    )


def _normalize_header(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().strip("﻿").lower()


def _resolve_columns(headers: list[str]) -> dict[str, str]:
    """把标准列映射到实际表头；只有唯一匹配才接受，歧义直接报错。"""
    normalized = [_normalize_header(header) for header in headers]
    resolved: dict[str, str] = {}
    for column in _COLUMNS:
        matches = [
            headers[index]
            for index, name in enumerate(normalized)
            if name in _COLUMN_ALIASES[column]
        ]
        if len(matches) != 1:
            raise ValueError(_COLUMN_HINT)
        resolved[column] = matches[0]
    return resolved


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(_ENCODING_HINT)


def _parse_amount(cell: str, row_number: int) -> int:
    """把 sales 单元格解析为整数分；接受货币符号、千分位和正负号。"""
    cleaned = _normalize_blank(cell).translate(_CURRENCY_PREFIX).strip()
    match = _SALES_CELL.fullmatch(cleaned)
    if not match:
        raise ValueError(f"第 {row_number} 条记录的 sales 必须是元，最多两位小数。")
    digits = match.group("whole").replace(",", "")
    fraction = (match.group("frac") or ".")[1:]
    if len(fraction) > 2:
        raise ValueError(f"第 {row_number} 条记录的 sales 必须是元，最多两位小数。")
    try:
        value = Decimal(f"{digits}.{fraction or '0'}").scaleb(2).to_integral_value()
    except InvalidOperation as exc:  # pragma: no cover - 正则已限制字符集
        raise ValueError(f"第 {row_number} 条记录的 sales 必须是元，最多两位小数。") from exc
    cents = int(value)
    if match.group("s1") == "-" or match.group("s2") == "-":
        cents = -abs(cents)
    else:
        cents = abs(cents)
    return cents


def _read_sales(source: Path, *, max_rows: int | None = None) -> _ReadResult:
    """单次读取字节快照：哈希、解码和解析都基于同一份内容。"""
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text, encoding = _decode(raw)

    orders: set[str] = set()
    normalized_orders: dict[str, set[str]] = defaultdict(set)
    sales_cents = units = row_count = 0
    preview: list[dict[str, Any]] = []
    summary_rows = 0
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames or []
        columns = _resolve_columns(headers)
        for row_number, row in enumerate(reader, start=1):
            if max_rows is not None and row_number > max_rows:
                raise ValueError(f"CSV 超过 {max_rows} 行上限，请拆分或先用筛选视图收敛。")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"第 {row_number} 条记录的列数和表头不一致。")
            order = row[columns["order_id"]]
            if not order.strip():
                raise ValueError(f"第 {row_number} 条记录的 order_id 为空。")
            sales_cents += _parse_amount(row[columns["sales"]], row_number)
            quantity = _normalize_blank(row[columns["quantity"]]).strip()
            if not _INTEGER.fullmatch(quantity):
                raise ValueError(f"第 {row_number} 条记录的 quantity 必须是非负整数。")
            units += int(quantity)
            orders.add(order)
            key = _normalize_blank(order).casefold()
            if key.isdigit():
                # 001 与 1 视为近似重复（只告警，不改订单数口径）。
                key = key.lstrip("0") or "0"
            normalized_orders[key].add(order)
            row_count = row_number
            if _normalize_blank(order).strip().lower() in _SUMMARY_IDS:
                summary_rows += 1
            if row_count <= _PREVIEW_ROWS:
                preview.append(
                    {
                        "record": row_count,
                        "order_id": order,
                        "sales": row[columns["sales"]].strip(),
                        "quantity": quantity,
                    }
                )
    except (csv.Error, UnicodeError) as exc:
        raise ValueError(_ENCODING_HINT) from exc

    if not row_count:
        raise ValueError("CSV 至少需要一条订单明细。")

    order_count = len(orders)
    metrics = {
        "销售额合计": f"{Decimal(sales_cents).scaleb(-2):.2f}",
        "销量合计": str(units),
        "订单数": str(order_count),
    }
    if order_count:
        # sales_cents 是分；先按分四舍五入，再换算成元，避免浮点误差。
        average_cents = (Decimal(sales_cents) / order_count).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        metrics["客单价"] = f"{average_cents.scaleb(-2):.2f}"

    warnings: list[dict[str, Any]] = []
    near_duplicates = [
        {"normalized": key, "order_ids": sorted(values)[:_SAMPLE_CAP], "count": len(values)}
        for key, values in normalized_orders.items()
        if len(values) > 1
    ]
    if near_duplicates:
        warnings.append(
            {
                "code": "near_duplicate_order_id",
                "message": "订单号存在近似重复（见核验明细），订单数仍按订单号原样去重。",
                "samples": near_duplicates[:_EVIDENCE_CAP],
                "total_groups": len(near_duplicates),
            }
        )
    if summary_rows:
        warnings.append(
            {
                "code": "summary_row",
                "message": "表中含合计/总计/小计行，复算已按明细行原样计入。",
                "count": summary_rows,
            }
        )

    return _ReadResult(
        metrics=metrics,
        evidence={
            "filename": source.name,
            "sha256": digest,
            "rows": row_count,
            "encoding": encoding.lower(),
            "preview": preview,
            "order_ids": order_count,
        },
        warnings=warnings,
    )


def _clean_label(label: str) -> str:
    value = label.strip()
    for marker in ("**", "__", "`"):
        if value.startswith(marker):
            value = value[len(marker) :]
        if value.endswith(marker):
            value = value[: -len(marker)]
    return value.strip()


def _clean_value(value: str) -> str:
    cleaned = value.strip()
    for marker in ("**", "__", "`"):
        if cleaned.startswith(marker):
            cleaned = cleaned[len(marker) :]
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)]
    return cleaned.strip()


def _scoped_metric(label: str) -> str | None:
    """label 里夹带了登记名（如「华东区订单数」）时返回被夹带的指标名。"""
    for metric in _RULES:
        if metric in label and label != metric:
            return metric
    return None


def _candidates(value: str) -> list[dict[str, str]]:
    """按单位与名称相似度给出口径候选，仅作提示，不自动采纳。"""
    unit = next(
        (unit for unit, _ in _RULES.values() if value.endswith(unit)),
        None,
    )
    suggestions: list[dict[str, str]] = []
    if unit:
        suggestions.extend(
            {"metric": metric, "formula": formula}
            for metric, (metric_unit, formula) in _RULES.items()
            if metric_unit == unit
        )
    return suggestions[:_SUGGEST_LIMIT]


def _claims_for_line(
    raw: str, line_number: int, *, in_fence: bool
) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "line": line_number,
        "text": raw,
        "metric": None,
        "status": "unsupported",
        "reason": _DEFAULT_REASON,
        "context": False,
    }
    if in_fence:
        claim["reason"] = _FENCE_REASON
        return claim
    match = _METRIC_LINE.fullmatch(raw)
    if not match:
        claim["context"] = True
        claim["reason"] = _CONTEXT_REASON
        return claim
    label = _clean_label(match["label"])
    value = _clean_value(match["value"])
    claim["label"] = label
    claim["submitted"] = value

    scoped = _scoped_metric(label)
    if scoped:
        claim["scoped_metric"] = scoped
        claim["reason"] = _SCOPED_REASON
        return claim
    if _INTENT.search(label):
        claim["context"] = True
        claim["reason"] = _CONTEXT_REASON
        return claim
    if label not in _RULES:
        claim["candidates"] = _candidates(value)
        if not _OTHER_NUMERIC_CLAIM.fullmatch(value):
            claim["context"] = True
            claim["reason"] = _CONTEXT_REASON
        return claim

    unit, formula = _RULES[label]
    number = value.removesuffix(unit).strip()
    valid = _DRAFT_AMOUNT if unit == "元" else _DRAFT_INTEGER
    claim.update(metric=label, formula=formula)
    if not (value.endswith(unit) and valid.fullmatch(number)):
        claim["reason"] = f"数值格式或单位不符合口径，此项使用「{unit}」。"
        return claim
    claim["submitted"] = value
    claim["_number"] = number
    claim["_unit"] = unit
    return claim


def _audit_claims(
    draft: str,
    metrics: dict[str, str],
    *,
    confirm_full_table: bool,
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    claims: list[dict[str, Any]] = []
    fence = ""
    has_context = False
    for line_number, raw in enumerate(draft.splitlines(), start=1):
        if not raw.strip():
            continue
        delimiter = _FENCE.fullmatch(raw)
        if delimiter:
            marker, info = delimiter.group("marker"), delimiter.group("info")
            if not fence:
                fence = marker
                claim = _claims_for_line(raw, line_number, in_fence=True)
            else:
                closes = (
                    marker[0] == fence[0]
                    and len(marker) >= len(fence)
                    and not info.strip()
                )
                claim = _claims_for_line(raw, line_number, in_fence=True)
                if closes:
                    fence = ""
            claims.append(claim)
            continue
        claim = _claims_for_line(raw, line_number, in_fence=bool(fence))
        has_context = has_context or bool(claim.pop("context", False))
        claims.append(claim)

    counts = Counter(claim["metric"] for claim in claims if claim["metric"])
    needs_scope_confirmation = False
    order_warning = any(
        warning.get("code") == "near_duplicate_order_id" for warning in warnings
    )
    for claim in claims:
        number = claim.pop("_number", None)
        unit = claim.pop("_unit", None)
        metric = claim.get("metric")
        if metric is None:
            continue
        if counts[metric] > 1:
            claim.update(status="unsupported", reason=_DUPLICATE_REASON)
            claim.pop("recalculated", None)
            continue
        if number is None or unit is None:
            continue
        expected = metrics[metric]
        if unit == "元" and metric == "客单价":
            agrees = Decimal(number) == Decimal(expected)
        elif unit == "元":
            agrees = Decimal(number) == Decimal(expected)
        else:
            agrees = int(number) == int(expected)
        claim.update(
            status="verified" if agrees else "conflict",
            recalculated=f"{expected} {unit}",
            reason="原值与明细复算一致。" if agrees else "原值与明细复算不一致。",
        )
        if metric in _ORDER_BASED and order_warning:
            claim["caveat"] = "订单号存在近似重复，订单数口径按原样去重，请人工复核。"

    for claim in claims:
        if claim.get("metric") and counts[claim["metric"]] == 1:
            if has_context and not confirm_full_table and claim["status"] != "unsupported":
                needs_scope_confirmation = True
                claim.update(status="unsupported", reason="草稿含上下文，请先确认这项指标的统计范围。")
                claim.pop("recalculated", None)
        claim.pop("_number", None)
        claim.pop("_unit", None)
    # 收尾统一字段：内部标记不进入响应；unsupported 不携带复算值，避免前端误显示。
    for claim in claims:
        claim.pop("context", None)
        claim.pop("_number", None)
        claim.pop("_unit", None)
        if claim.get("status") == "unsupported":
            claim.pop("recalculated", None)
    return claims, needs_scope_confirmation


def _audit_id(
    digest: str,
    draft: str,
    confirm_full_table: bool,
    metrics: dict[str, str],
    draft_trace_sha256: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "source_sha256": digest,
            "draft": draft.replace("\r\n", "\n").replace("\r", "\n"),
            "confirm_full_table": confirm_full_table,
            "metrics": metrics,
            # 有起草轨迹时把它绑进 audit_id：换了轨迹（例如重新起草）就是另一个审计对象。
            "draft_trace_sha256": draft_trace_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "aud-" + hashlib.sha256(payload).hexdigest()[:32]


def audit_sales_brief(
    source: Path,
    draft: str,
    *,
    confirm_full_table: bool = False,
    max_rows: int | None = None,
    draft_trace_sha256: str | None = None,
) -> dict[str, Any]:
    read = _read_sales(source, max_rows=max_rows)
    metrics = read.metrics
    claims, needs_scope_confirmation = _audit_claims(
        draft,
        metrics,
        confirm_full_table=confirm_full_table,
        warnings=read.warnings,
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
            f"全表统计，共 {read.evidence['rows']} 条订单明细。",
            "",
            *(f"- {claim['metric']}：{claim['recalculated']}" for claim in publishable),
            "",
            "## 复算依据",
            "",
            *(f"- {claim['metric']}：{claim['formula']}" for claim in publishable),
            f"- 原始 CSV 的 SHA-256：`{read.evidence['sha256']}`",
        ]
        if summary["unsupported"]:
            lines.append(f"- 暂缓发布 {summary['unsupported']} 行，草稿原文见核验明细。")
        brief = "\n".join(lines) + "\n"
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": _audit_id(
            read.evidence["sha256"],
            draft,
            confirm_full_table,
            metrics,
            draft_trace_sha256,
        ),
        "draft_trace_sha256": draft_trace_sha256,
        "source": read.evidence,
        "metrics": metrics,
        "summary": summary,
        "claims": claims,
        "warnings": read.warnings,
        "needs_scope_confirmation": needs_scope_confirmation,
        "confirmed_full_table": confirm_full_table,
        "corrected_brief": brief,
    }
