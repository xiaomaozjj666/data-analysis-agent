"""声明式核验口径：让经营简报核验不再只认四个销售指标。

企业数据很少有固定的 ``order_id / sales / quantity`` 三列。这里允许每个会话
登记一组**纯声明式**指标（列名 + 聚合 + 单位 + 类型 + 空值策略 + 过滤条件），
核验器按登记口径独立读取原始 CSV 复算。刻意不支持任意表达式：核验是最后一道
闸门，口径必须可审计、可解释，而不是再执行一段由模型写出来的代码。

文件约定：工作区根目录的 ``audit_schema.json``::

    {"metrics": [
      {"name": "Q4 有效销售额", "column": "revenue", "aggregation": "sum",
       "unit": "元", "data_type": "decimal", "null_policy": "exclude",
       "rounding": "HALF_UP", "sign_policy": "allow",
       "filters": [{"column": "status", "op": "ne", "value": "cancelled"}],
       "time_window": {"column": "order_date", "start": "2026-10-01",
                       "end": "2026-12-31"}}
    ]}

每次保存都会向 ``audit_schema_history.jsonl`` 追加一条带时间与指纹的记录，
口径变更因此可追溯；未登记时返回 None，调用方回退到内置销售口径。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from data_agent._workspace_bundles import _atomic_write_text

SCHEMA_FILENAME = "audit_schema.json"
HISTORY_FILENAME = "audit_schema_history.jsonl"
SCHEMA_VERSION = 2
ALLOWED_AGGREGATIONS = frozenset({"sum", "count", "count_distinct", "min", "max", "mean"})
_ALLOWED_TYPES = frozenset({"decimal", "integer", "date", "string"})
_ALLOWED_NULL_POLICIES = frozenset({"exclude", "fail", "zero"})
_ALLOWED_ROUNDING = frozenset({"HALF_UP", "HALF_EVEN"})
_ALLOWED_SIGN_POLICIES = frozenset({"allow", "non_negative"})
_ALLOWED_FILTER_OPS = frozenset({"eq", "ne", "gt", "ge", "lt", "le", "in", "contains"})
_INTEGER_AGGREGATIONS = frozenset({"count", "count_distinct"})
_MAX_METRICS = 12
_MAX_FILTERS = 8
_PREVIEW_ROWS = 6
_NUMBER = re.compile(r"[+-]?[0-9][0-9,]*(?:\.[0-9]+)?")
_CURRENCY = str.maketrans("", "", "¥￥$€£")
_BLANK_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})
_BLANK_GLYPHS = frozenset("ᅟᅠ⠀ㅤﾠ")
_NULL_TOKENS = frozenset({"", "n/a", "na", "null", "none", "nan", "-"})
_ENCODING_HINT = "核验使用 UTF-8 或 GB18030 编码、逗号分隔的 CSV。请检查编码和引号。"


@dataclass(frozen=True, slots=True)
class AuditMetric:
    name: str
    column: str
    aggregation: str
    unit: str = ""
    decimals: int = 2
    aliases: tuple[str, ...] = ()
    data_type: str = "decimal"
    null_policy: str = "exclude"
    rounding: str = "HALF_UP"
    sign_policy: str = "allow"
    filters: tuple[dict[str, Any], ...] = ()
    time_window: dict[str, Any] | None = None

    @property
    def is_integer(self) -> bool:
        # 输出是否为整数只取决于聚合方式；data_type=integer 约束的是输入单元格。
        return self.aggregation in _INTEGER_AGGREGATIONS

    def formula(self) -> str:
        parts = [f"{self.aggregation.upper()}({self.column})"]
        if self.data_type == "integer":
            parts.append("INTEGER")
        if self.filters:
            rendered = " AND ".join(
                f"{item['column']} {item['op']} {item['value']!r}" for item in self.filters
            )
            parts.append(f"WHERE {rendered}")
        window = self.time_window or {}
        if window:
            window_bits = []
            if window.get("start"):
                window_bits.append(f">= {window['start']}")
            if window.get("end"):
                window_bits.append(f"<= {window['end']}")
            parts.append(f"WINDOW {window.get('column')} " + " ".join(window_bits))
        return " ".join(parts)


@dataclass
class AuditMetricRead:
    metrics: dict[str, str]
    evidence: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _as_aliases(value: Any, index: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"metrics 第 {index + 1} 项的 aliases 必须是字符串数组。")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _parse_filters(raw: Any, name: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > _MAX_FILTERS:
        raise ValueError(f"指标「{name}」的 filters 必须是最多 {_MAX_FILTERS} 项的数组。")
    filters: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"指标「{name}」的 filter 必须是对象。")
        column = str(item.get("column") or "").strip()
        op = str(item.get("op") or "").strip().lower()
        if not column:
            raise ValueError(f"指标「{name}」的 filter 缺少 column。")
        if op not in _ALLOWED_FILTER_OPS:
            raise ValueError(f"指标「{name}」的 filter op 不支持：{op}。")
        value = item.get("value")
        if op == "in" and not isinstance(value, list):
            raise ValueError(f"指标「{name}」的 in 过滤需要使用数组。")
        filters.append({"column": column, "op": op, "value": value})
    return tuple(filters)


def _parse_window(raw: Any, name: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"指标「{name}」的 time_window 必须是对象。")
    column = str(raw.get("column") or "").strip()
    if not column:
        raise ValueError(f"指标「{name}」的 time_window 缺少 column。")
    window: dict[str, Any] = {"column": column}
    for key in ("start", "end"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"指标「{name}」的 time_window.{key} 必须是 YYYY-MM-DD。") from exc
        window[key] = str(value)
    if "start" not in window and "end" not in window:
        raise ValueError(f"指标「{name}」的 time_window 至少需要 start 或 end。")
    return window


def parse_schema(payload: Any) -> list[AuditMetric]:
    """解析并校验声明式口径；结构错误直接报错，不静默跳过。"""
    entries = payload.get("metrics") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("核验口径必须是数组，或含 metrics 数组的对象。")
    if len(entries) > _MAX_METRICS:
        raise ValueError(f"核验口径最多 {_MAX_METRICS} 项。")
    metrics: list[AuditMetric] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise ValueError(f"metrics 第 {index + 1} 项必须是对象。")
        name = str(raw.get("name") or "").strip()
        column = str(raw.get("column") or "").strip()
        aggregation = str(raw.get("aggregation") or "").strip().lower()
        if not name:
            raise ValueError(f"metrics 第 {index + 1} 项缺少 name。")
        if name in seen:
            raise ValueError(f"指标名称重复：{name}。")
        if not column:
            raise ValueError(f"指标「{name}」缺少 column。")
        if aggregation not in ALLOWED_AGGREGATIONS:
            allowed = "、".join(sorted(ALLOWED_AGGREGATIONS))
            raise ValueError(f"指标「{name}」的 aggregation 必须是 {allowed} 之一。")
        data_type = str(raw.get("data_type") or "decimal").strip().lower()
        if data_type not in _ALLOWED_TYPES:
            raise ValueError(f"指标「{name}」的 data_type 不支持：{data_type}。")
        if aggregation in {"sum", "mean", "min", "max"} and data_type not in {"decimal", "integer"}:
            raise ValueError(f"指标「{name}」的数值聚合需要 data_type=decimal 或 integer。")
        null_policy = str(raw.get("null_policy") or "exclude").strip().lower()
        if null_policy not in _ALLOWED_NULL_POLICIES:
            raise ValueError(f"指标「{name}」的 null_policy 不支持：{null_policy}。")
        rounding = str(raw.get("rounding") or "HALF_UP").strip().upper()
        if rounding not in _ALLOWED_ROUNDING:
            raise ValueError(f"指标「{name}」的 rounding 不支持：{rounding}。")
        sign_policy = str(raw.get("sign_policy") or "allow").strip().lower()
        if sign_policy not in _ALLOWED_SIGN_POLICIES:
            raise ValueError(f"指标「{name}」的 sign_policy 不支持：{sign_policy}。")
        unit = str(raw.get("unit") or "").strip()
        if len(unit) > 8:
            raise ValueError(f"指标「{name}」的 unit 最多 8 个字符。")
        try:
            decimals = int(raw.get("decimals", 2))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"指标「{name}」的 decimals 必须是整数。") from exc
        if decimals < 0 or decimals > 6:
            raise ValueError(f"指标「{name}」的 decimals 必须在 0–6 之间。")
        seen.add(name)
        metrics.append(
            AuditMetric(
                name=name,
                column=column,
                aggregation=aggregation,
                unit=unit,
                decimals=decimals,
                aliases=_as_aliases(raw.get("aliases"), index),
                data_type=data_type,
                null_policy=null_policy,
                rounding=rounding,
                sign_policy=sign_policy,
                filters=_parse_filters(raw.get("filters"), name),
                time_window=_parse_window(raw.get("time_window"), name),
            )
        )
    return metrics


def _serialize(metrics: list[AuditMetric]) -> list[dict[str, Any]]:
    return [
        {
            "name": metric.name,
            "column": metric.column,
            "aggregation": metric.aggregation,
            "unit": metric.unit,
            "decimals": metric.decimals,
            "aliases": list(metric.aliases),
            "data_type": metric.data_type,
            "null_policy": metric.null_policy,
            "rounding": metric.rounding,
            "sign_policy": metric.sign_policy,
            "filters": list(metric.filters),
            "time_window": metric.time_window,
        }
        for metric in metrics
    ]


def load_schema(root: str | Path) -> list[AuditMetric] | None:
    path = Path(root) / SCHEMA_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取核验口径 {SCHEMA_FILENAME}：{exc}") from exc
    return parse_schema(payload)


def save_schema(root: str | Path, payload: Any) -> list[AuditMetric]:
    metrics = parse_schema(payload)
    root_path = Path(root)
    _atomic_write_text(
        root_path / SCHEMA_FILENAME,
        json.dumps({"version": SCHEMA_VERSION, "metrics": _serialize(metrics)}, ensure_ascii=False, indent=2),
    )
    history = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": schema_fingerprint(metrics),
        "metrics": _serialize(metrics),
    }
    try:
        with (root_path / HISTORY_FILENAME).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(history, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # 历史写入失败不能回滚已生效的口径；下次保存会再补一条。
        pass
    return metrics


def schema_fingerprint(metrics: list[AuditMetric]) -> str:
    payload = json.dumps(_serialize(metrics), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_value(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value).casefold()
        if char not in _BLANK_GLYPHS
        and unicodedata.category(char) not in _BLANK_CATEGORIES
    )


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(_ENCODING_HINT)


def _decimal(cell: str, *, column: str, row_number: int) -> Decimal:
    cleaned = cell.translate(_CURRENCY).replace(",", "").strip()
    if not cleaned or not _NUMBER.fullmatch(cleaned):
        raise ValueError(f"第 {row_number} 条记录的 {column} 不是有效数值。")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - 正则已限制字符集
        raise ValueError(f"第 {row_number} 条记录的 {column} 不是有效数值。") from exc


def _is_null(cell: str) -> bool:
    return cell.strip().casefold() in _NULL_TOKENS


def _matches_filter(cell: str, op: str, expected: Any, *, column: str, row_number: int) -> bool:
    actual = cell.strip()
    if op in {"eq", "ne"}:
        desired = str(expected).strip()
        result = actual == desired
        if not result:
            try:
                result = Decimal(actual.replace(",", "")) == Decimal(desired.replace(",", ""))
            except (InvalidOperation, ValueError):
                result = False
        return result if op == "eq" else not result
    if op == "contains":
        return str(expected).casefold() in actual.casefold()
    if op == "in":
        return actual in {str(item).strip() for item in expected}
    left = _decimal(actual, column=column, row_number=row_number)
    right = _decimal(str(expected), column=column, row_number=row_number)
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    if op == "lt":
        return left < right
    return left <= right


def _row_passes(row: dict[str, str], metric: AuditMetric, row_number: int) -> bool:
    for item in metric.filters:
        column = item["column"]
        if column not in row:
            raise ValueError(f"CSV 缺少核验口径需要的列：{column}")
        if not _matches_filter(
            row[column], item["op"], item["value"], column=column, row_number=row_number
        ):
            return False
    window = metric.time_window
    if window:
        column = window["column"]
        if column not in row:
            raise ValueError(f"CSV 缺少核验口径需要的列：{column}")
        raw_value = row[column].strip()
        if _is_null(raw_value):
            return False
        try:
            current = date.fromisoformat(raw_value[:10].replace("/", "-"))
        except ValueError as exc:
            raise ValueError(f"第 {row_number} 条记录的 {column} 不是有效日期。") from exc
        if window.get("start") and current < date.fromisoformat(window["start"]):
            return False
        if window.get("end") and current > date.fromisoformat(window["end"]):
            return False
    return True


def read_metrics(
    source: Path, metrics: list[AuditMetric], *, max_rows: int | None = None
) -> AuditMetricRead:
    """单次读取字节快照，按声明式口径复算；金额/均值按登记精度输出。"""
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text, encoding = _decode(raw)

    sums: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    minimums: dict[str, Decimal] = {}
    maximums: dict[str, Decimal] = {}
    distinct: dict[str, set[str]] = defaultdict(set)
    normalized: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    preview: list[dict[str, Any]] = []
    rows = 0
    used = 0
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames or []
        required = {metric.column for metric in metrics}
        for metric in metrics:
            required.update(item["column"] for item in metric.filters)
            if metric.time_window:
                required.add(metric.time_window["column"])
        missing = sorted(column for column in required if column not in headers)
        if missing:
            raise ValueError("CSV 缺少核验口径需要的列：" + "、".join(missing))
        for row_number, row in enumerate(reader, start=1):
            if max_rows is not None and row_number > max_rows:
                raise ValueError(f"CSV 超过 {max_rows} 行上限，请拆分或先用筛选视图收敛。")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"第 {row_number} 条记录的列数和表头不一致。")
            rows = row_number
            matched_any = False
            for metric in metrics:
                if not _row_passes(row, metric, row_number):
                    continue
                matched_any = True
                cell = row[metric.column]
                if metric.aggregation == "count":
                    counts[metric.name] += 1
                    continue
                if metric.aggregation == "count_distinct":
                    value = str(cell).strip()
                    if _is_null(value):
                        if metric.null_policy == "fail":
                            raise ValueError(
                                f"第 {row_number} 条记录的 {metric.column} 为空（null_policy=fail）。"
                            )
                        continue
                    distinct[metric.name].add(value)
                    normalized[metric.name][_normalize_value(value)].add(value)
                    continue
                if _is_null(cell):
                    if metric.null_policy == "fail":
                        raise ValueError(
                            f"第 {row_number} 条记录的 {metric.column} 为空（null_policy=fail）。"
                        )
                    if metric.null_policy == "zero":
                        counts[metric.name] += 1
                    continue
                value = _decimal(cell, column=metric.column, row_number=row_number)
                if metric.data_type == "integer" and value != value.to_integral_value():
                    raise ValueError(f"第 {row_number} 条记录的 {metric.column} 必须是整数。")
                if metric.sign_policy == "non_negative" and value < 0:
                    raise ValueError(f"第 {row_number} 条记录的 {metric.column} 不能为负数。")
                sums[metric.name] += value
                counts[metric.name] += 1
                minimums[metric.name] = (
                    value if metric.name not in minimums else min(minimums[metric.name], value)
                )
                maximums[metric.name] = (
                    value if metric.name not in maximums else max(maximums[metric.name], value)
                )
            if rows <= _PREVIEW_ROWS:
                preview.append({"record": rows, **{column: row[column] for column in headers}})
            if matched_any:
                used += 1
    except (csv.Error, UnicodeError) as exc:
        raise ValueError(_ENCODING_HINT) from exc
    if not rows:
        raise ValueError("CSV 至少需要一条记录。")

    values: dict[str, str] = {}
    for metric in metrics:
        if metric.aggregation == "count_distinct":
            values[metric.name] = str(len(distinct[metric.name]))
            continue
        if metric.aggregation == "count":
            values[metric.name] = str(counts[metric.name])
            continue
        if metric.aggregation == "sum":
            number = sums[metric.name]
        elif metric.aggregation == "min":
            number = minimums[metric.name]
        elif metric.aggregation == "max":
            number = maximums[metric.name]
        else:
            if not counts[metric.name]:
                raise ValueError(f"指标「{metric.name}」没有可计算的非空数值。")
            number = sums[metric.name] / counts[metric.name]
        mode = ROUND_HALF_EVEN if metric.rounding == "HALF_EVEN" else ROUND_HALF_UP
        quant = Decimal(1).scaleb(-metric.decimals)
        values[metric.name] = f"{number.quantize(quant, rounding=mode):.{metric.decimals}f}"

    warnings: list[dict[str, Any]] = []
    for metric in metrics:
        if metric.aggregation != "count_distinct":
            continue
        groups = [
            {"normalized": key, "values": sorted(items)[:5], "count": len(items)}
            for key, items in normalized[metric.name].items()
            if len(items) > 1
        ]
        if groups:
            warnings.append(
                {
                    "code": "near_duplicate_value",
                    "metric": metric.name,
                    "message": f"「{metric.name}」存在近似重复值，去重计数按原样处理。",
                    "samples": groups[:20],
                    "total_groups": len(groups),
                }
            )

    return AuditMetricRead(
        metrics=values,
        evidence={
            "filename": source.name,
            "sha256": digest,
            "rows": rows,
            "rows_used": used,
            "encoding": encoding.lower(),
            "columns": list(headers),
            "preview": preview,
        },
        warnings=warnings,
    )
