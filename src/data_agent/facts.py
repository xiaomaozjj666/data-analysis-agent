"""从工具结果抽出可核验事实，供最终报告引用而不是让模型现编数字。"""

from __future__ import annotations

import ast
import json
import math
import re
from typing import Any

_FACT_KEYS = (
    "p_value",
    "statistic",
    "sample_size",
    "mean_difference",
    "effect_size",
    "r2",
    "adjusted_r2",
    "rmse",
    "mae",
    "eta_squared",
    "rows",
    "row_count",
    "groups",
    "total_n",
    "min_expected",
    "truncated",
    "comparison",
)


def _parse_payload(detail: Any) -> dict[str, Any] | None:
    if isinstance(detail, dict):
        return detail
    text = str(detail or "").strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        try:
            loaded = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
    return loaded if isinstance(loaded, dict) else None


_NUMBER_WITH_UNIT = re.compile(
    r"(?P<number>[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    r"\s*(?P<unit>%|‰|bp|percent|pct|万亿元|亿元|万元|千元|元|万条|千条|万|亿|千|百|个|条|行|样本|records|rows)?",
    re.IGNORECASE,
)


def _as_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, str):
        match = _NUMBER_WITH_UNIT.fullmatch(value.strip())
        if not match:
            return None
        stripped = match.group("number").replace(",", "")
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
        try:
            number = float(stripped)
        except ValueError:
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    return None


def _field_unit(payload: dict[str, Any], field: str) -> str:
    units = payload.get("units")
    unit = units.get(field) if isinstance(units, dict) else None
    unit = unit or payload.get(f"{field}_unit")
    return str(unit or "").strip()


def extract_facts(trace: list[dict[str, Any]] | None, completed_steps: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """从 tool_result 与步骤摘要中收集事实及可重放的定位信息。"""
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(
        source: str,
        field: str,
        value: Any,
        *,
        unit: str = "",
        trace_index: int | None = None,
        step_index: int | None = None,
        json_path: str | None = None,
    ) -> None:
        number = _as_number(value)
        rendered = str(number) if number is not None else (str(value) if value is not None else "")
        if not rendered or rendered in {"None", "nan"}:
            return
        inferred = _NUMBER_WITH_UNIT.fullmatch(str(value).strip()) if number is not None else None
        unit = unit or (inferred.group("unit") if inferred else "")
        key = (source, field, rendered, unit)
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {"source": source, "field": field, "value": rendered}
        if unit:
            item["unit"] = unit
        if trace_index is not None:
            item["trace_index"] = trace_index
        if step_index is not None:
            item["step_index"] = step_index
        if json_path is not None:
            item["json_path"] = json_path
        facts.append(item)

    for trace_index, item in enumerate(trace or []):
        if item.get("type") != "tool_result":
            continue
        name = str(item.get("name") or "tool")
        payload = _parse_payload(item.get("detail"))
        if not payload:
            continue
        for field in _FACT_KEYS:
            if field in payload:
                _add(
                    name,
                    field,
                    payload[field],
                    unit=_field_unit(payload, field),
                    trace_index=trace_index,
                    json_path=f"/{field}",
                )
        means = payload.get("means")
        if isinstance(means, dict):
            for column, mean in means.items():
                field = f"mean:{column}"
                escaped = str(column).replace("~", "~0").replace("/", "~1")
                _add(
                    name,
                    field,
                    mean,
                    unit=_field_unit(payload, field),
                    trace_index=trace_index,
                    json_path=f"/means/{escaped}",
                )
        coefficients = payload.get("coefficients")
        if isinstance(coefficients, dict):
            for column, coef in list(coefficients.items())[:8]:
                field = f"coef:{column}"
                escaped = str(column).replace("~", "~0").replace("/", "~1")
                _add(
                    name,
                    field,
                    coef,
                    unit=_field_unit(payload, field),
                    trace_index=trace_index,
                    json_path=f"/coefficients/{escaped}",
                )
        if payload.get("method"):
            _add(name, "method", payload["method"], json_path="/method")
        warning = payload.get("warning")
        if warning:
            _add(name, "warning", warning, json_path="/warning")

    for step_index, step in enumerate(completed_steps or []):
        title = str(step.get("title") or step.get("id") or "step")
        status = step.get("status")
        if status:
            _add(title, "status", status, step_index=step_index)

    return facts[:80]


def render_facts_markdown(facts: list[dict[str, Any]]) -> str:
    if not facts:
        return ""
    lines = [
        "## 核验附录",
        "",
        "下表来自工具原始返回值，不是模型改写。报告正文中的关键数字应能在此表找到对应项。",
        "",
        "| 来源 | 字段 | 值 |",
        "| --- | --- | --- |",
    ]
    for item in facts:
        source = item["source"].replace("|", "\\|")
        field = item["field"].replace("|", "\\|")
        value = item["value"].replace("|", "\\|")
        lines.append(f"| {source} | {field} | {value} |")
    return "\n".join(lines)


def facts_prompt_block(facts: list[dict[str, Any]]) -> str:
    if not facts:
        return "（本次没有抽出结构化数字；不得编造精确统计量。）"
    lines = ["只能引用下列已核验数字，禁止使用未出现在此表中的精确统计量："]
    for item in facts:
        lines.append(f"- {item['source']} · {item['field']} = {item['value']}")
    return "\n".join(lines)
