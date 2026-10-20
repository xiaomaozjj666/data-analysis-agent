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
        stripped = value.strip().replace(",", "")
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


def extract_facts(trace: list[dict[str, Any]] | None, completed_steps: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """从 tool_result 与步骤摘要中收集扁平事实。"""
    facts: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(source: str, field: str, value: Any) -> None:
        number = _as_number(value)
        rendered = str(number) if number is not None else (str(value) if value is not None else "")
        if not rendered or rendered in {"None", "nan"}:
            return
        key = (source, field, rendered)
        if key in seen:
            return
        seen.add(key)
        facts.append({"source": source, "field": field, "value": rendered})

    for item in trace or []:
        if item.get("type") != "tool_result":
            continue
        name = str(item.get("name") or "tool")
        payload = _parse_payload(item.get("detail"))
        if not payload:
            continue
        for field in _FACT_KEYS:
            if field in payload:
                _add(name, field, payload[field])
        means = payload.get("means")
        if isinstance(means, dict):
            for column, mean in means.items():
                _add(name, f"mean:{column}", mean)
        coefficients = payload.get("coefficients")
        if isinstance(coefficients, dict):
            for column, coef in list(coefficients.items())[:8]:
                _add(name, f"coef:{column}", coef)
        if payload.get("method"):
            _add(name, "method", payload["method"])
        warning = payload.get("warning")
        if warning:
            _add(name, "warning", warning)

    for step in completed_steps or []:
        title = str(step.get("title") or step.get("id") or "step")
        status = step.get("status")
        if status:
            _add(title, "status", status)

    return facts[:80]


def render_facts_markdown(facts: list[dict[str, str]]) -> str:
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


def facts_prompt_block(facts: list[dict[str, str]]) -> str:
    if not facts:
        return "（本次没有抽出结构化数字；不得编造精确统计量。）"
    lines = ["只能引用下列已核验数字，禁止使用未出现在此表中的精确统计量："]
    for item in facts:
        lines.append(f"- {item['source']} · {item['field']} = {item['value']}")
    return "\n".join(lines)
