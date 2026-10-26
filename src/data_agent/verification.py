"""报告数字与事实的确定性核验，不调用模型。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from data_agent.serialization import json_text
from data_agent.workspace import _atomic_write_text

VERIFICATION_FILENAME = "report-verification.json"
VERIFICATION_VERSION = 1
_NUMBER = re.compile(
    r"(?<![\d.])(?P<number>[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    r"\s*(?P<unit>%|‰|bp|percent|pct|万亿元|亿元|万元|千元|元|万条|千条|万|亿|千|百|个|条|行|样本|records|rows)?",
    re.IGNORECASE,
)
_APPENDIX = re.compile(r"(?m)^##\s+(?:核验附录|结论核验)\b")
_UNIT_SCALES = {
    "%": ("ratio", Decimal("0.01")),
    "percent": ("ratio", Decimal("0.01")),
    "pct": ("ratio", Decimal("0.01")),
    "‰": ("ratio", Decimal("0.001")),
    "bp": ("ratio", Decimal("0.0001")),
    "ratio": ("ratio", Decimal(1)),
    "比例": ("ratio", Decimal(1)),
    "元": ("currency", Decimal(1)),
    "千元": ("currency", Decimal(1000)),
    "万元": ("currency", Decimal(10000)),
    "亿元": ("currency", Decimal(100000000)),
    "万亿元": ("currency", Decimal(1000000000000)),
    "个": ("count", Decimal(1)),
    "条": ("count", Decimal(1)),
    "行": ("count", Decimal(1)),
    "样本": ("count", Decimal(1)),
    "records": ("count", Decimal(1)),
    "rows": ("count", Decimal(1)),
    "百": ("count", Decimal(100)),
    "千": ("count", Decimal(1000)),
    "千条": ("count", Decimal(1000)),
    "万": ("count", Decimal(10000)),
    "万条": ("count", Decimal(10000)),
    "亿": ("count", Decimal(100000000)),
}
_FIELD_ALIASES = {
    "p_value": ("p_value", "p-value", "p值", "p 值"),
    "sample_size": ("sample_size", "样本量", "样本数"),
    "rows": ("rows", "row_count", "行数", "记录数"),
    "row_count": ("rows", "row_count", "行数", "记录数"),
    "total_n": ("total_n", "总样本量", "总样本数"),
    "groups": ("groups", "组数", "分组数"),
    "mean_difference": ("mean_difference", "均值差", "平均差"),
    "effect_size": ("effect_size", "效应量"),
    "r2": ("r2", "r²", "决定系数"),
    "adjusted_r2": ("adjusted_r2", "调整r2", "调整r²", "调整决定系数"),
    "rmse": ("rmse", "均方根误差"),
    "mae": ("mae", "平均绝对误差"),
    "eta_squared": ("eta_squared", "η²", "eta方"),
    "statistic": ("statistic", "统计量"),
    "min_expected": ("min_expected", "最小期望", "最小期望频数"),
}


def _unit_scale(unit: str) -> tuple[str, Decimal] | None:
    return _UNIT_SCALES.get(unit.strip().lower())


def _aliases(field: str) -> tuple[str, ...]:
    aliases = list(_FIELD_ALIASES.get(field, (field, field.replace("_", " "))))
    if ":" in field:
        kind, column = field.split(":", 1)
        word = {"mean": "均值", "coef": "系数"}.get(kind, kind)
        aliases.extend((column, f"{column}{word}", f"{column} {word}", f"{kind} {column}"))
    return tuple(alias.lower() for alias in aliases)


def _claim_value(match: re.Match[str]) -> tuple[Decimal, int] | None:
    try:
        value = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    return value, max(0, -value.as_tuple().exponent)


def _compatible(claim: Decimal, places: int, unit: str, fact: Decimal, fact_unit: str) -> bool:
    scale = Decimal(1)
    if bool(unit) != bool(fact_unit):
        return False
    if unit:
        claim_scale = _unit_scale(unit)
        evidence_scale = _unit_scale(fact_unit)
        if not claim_scale or not evidence_scale or claim_scale[0] != evidence_scale[0]:
            return False
        scale = claim_scale[1]
        difference = abs(claim * scale - fact * evidence_scale[1])
    else:
        difference = abs(claim - fact)
    tolerance = Decimal("0.5") * (Decimal(10) ** -places) * scale
    return difference <= tolerance


def _conflicting_fields(candidates: list[tuple[dict[str, Any], Decimal]]) -> set[str]:
    fields = set()
    for index, (fact, value) in enumerate(candidates):
        for other, other_value in candidates[index + 1 :]:
            if fact["field"] != other["field"]:
                continue
            unit = str(fact.get("unit", ""))
            other_unit = str(other.get("unit", ""))
            if bool(unit) != bool(other_unit):
                fields.add(str(fact["field"]))
                continue
            scale = _unit_scale(unit) if unit else None
            other_scale = _unit_scale(other_unit) if other_unit else None
            if unit and (not scale or not other_scale or scale[0] != other_scale[0]):
                fields.add(str(fact["field"]))
                continue
            left = value * scale[1] if scale and other_scale else value
            right = other_value * other_scale[1] if scale and other_scale else other_value
            if left != right:
                fields.add(str(fact["field"]))
    return fields


def _evidence(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in fact.items()
            if key in {"source", "field", "value", "unit", "trace_index", "step_index", "json_path"}
        }
        for fact in facts
    ]


def _clause_context(line: str, start: int, end: int) -> str:
    delimiters = "；;。！？!?，,|"
    left = max(line.rfind(delimiter, 0, start) for delimiter in delimiters)
    right_positions = [
        position for delimiter in delimiters if (position := line.find(delimiter, end)) >= 0
    ]
    right = min(right_positions, default=len(line))
    return line[left + 1 : right].strip() or line.strip()


def _numeric_claims(text: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numeric_facts = []
    for fact in facts:
        try:
            value = Decimal(str(fact["value"]).replace(",", ""))
        except (InvalidOperation, KeyError):
            continue
        numeric_facts.append((fact, value))
    claims = []
    in_code = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for match in _NUMBER.finditer(line):
            parsed = _claim_value(match)
            if parsed is None:
                continue
            claim, places = parsed
            unit = match.group("unit") or ""
            context = _clause_context(line, match.start(), match.end())
            candidates = [
                (fact, value)
                for fact, value in numeric_facts
                if any(alias in context.lower() for alias in _aliases(str(fact["field"])))
            ]
            matches = [
                (fact, value)
                for fact, value in candidates
                if _compatible(claim, places, unit, value, str(fact.get("unit", "")))
            ]
            implicated = matches or candidates
            relevant = {str(fact["field"]) for fact, _ in implicated}
            conflicting = _conflicting_fields(candidates) & relevant
            verdict = (
                "conflict"
                if conflicting
                else ("verified" if matches else ("conflict" if candidates else "unsupported"))
            )
            evidence = (
                [fact for fact, _ in candidates if fact["field"] in conflicting]
                if conflicting
                else [fact for fact, _ in implicated]
            )
            claims.append(
                {
                    "claim": context[:240],
                    "line": line_number,
                    "value": match.group("number"),
                    "unit": unit,
                    "verdict": verdict,
                    "evidence": _evidence(evidence) if verdict != "unsupported" else [],
                }
            )
    return claims


def verify_report(response: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """把正文数字/已知文本事实判为 verified、conflict 或 unsupported。"""
    narrative = _APPENDIX.split(response, maxsplit=1)[0]
    claims = _numeric_claims(narrative, facts)
    for fact in facts:
        try:
            Decimal(str(fact["value"]).replace(",", ""))
            continue
        except (InvalidOperation, KeyError):
            pass
        value = str(fact.get("value", "")).strip()
        if value and value in narrative:
            claims.append(
                {
                    "claim": value,
                    "line": next(
                        (
                            index
                            for index, line in enumerate(narrative.splitlines(), 1)
                            if value in line
                        ),
                        0,
                    ),
                    "value": value,
                    "unit": "",
                    "verdict": "verified",
                    "evidence": _evidence([fact]),
                }
            )
    for index, claim in enumerate(claims, 1):
        claim["id"] = f"claim-{index:03d}"
    summary = {
        status: sum(claim["verdict"] == status for claim in claims)
        for status in ("verified", "conflict", "unsupported")
    }
    summary["total"] = len(claims)
    return {"schema_version": VERIFICATION_VERSION, "summary": summary, "claims": claims}


def render_verification_markdown(report: dict[str, Any]) -> str:
    claims = report.get("claims") or []
    if not claims:
        return ""
    lines = [
        "## 结论核验",
        "",
        f"共 {len(claims)} 项：verified={report['summary']['verified']}，conflict={report['summary']['conflict']}，unsupported={report['summary']['unsupported']}。",
        "",
        "| 结论（原文行） | 判定 | 证据定位 |",
        "| --- | --- | --- |",
    ]
    for claim in claims:
        quote = claim["claim"].replace("|", "\\|").replace("\n", " ")
        evidence = (
            "; ".join(
                f"{item['source']}:{item['field']}"
                + (f" trace[{item['trace_index']}]" if "trace_index" in item else "")
                + (f" {item['json_path']}" if "json_path" in item else "")
                for item in claim["evidence"]
            )
            or "无"
        )
        lines.append(f"| {quote}（L{claim['line']}） | {claim['verdict']} | {evidence} |")
    return "\n".join(lines)


def save_verification(workspace: Any, report: dict[str, Any]) -> dict[str, str]:
    """原子写入并注册可下载的核验 JSON 产物。"""
    path = Path(workspace.artifacts_dir) / VERIFICATION_FILENAME
    _atomic_write_text(path, json_text(report))
    workspace.register_artifact(path, "verification", "逐结论确定性核验报告")
    return next(item for item in workspace.artifacts if item["path"] == str(path.resolve()))
