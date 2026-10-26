"""评测结果的数据模型与加载器。

结果文件是 JSONL（也接受 JSON 数组）：每行一个用例，

    {"case_id": "csv-basic-01", "passed": true, "cost_usd": 0.012, "latency_ms": 4200}

``cost_usd`` / ``latency_ms`` 可省略；缺少时只跳过成本 / 延迟护栏，
不影响准确性判定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    cost_usd: float | None = None
    latency_ms: float | None = None


@dataclass
class SuiteResult:
    """一次运行的全部用例结果。``variant`` 仅用于报告展示。"""

    variant: str
    cases: dict[str, CaseResult] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        if not self.variant.strip():
            raise ValueError("variant 不能为空。")

    @property
    def accuracy(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for item in self.cases.values() if item.passed) / len(self.cases)


def _coerce_case(raw: Any, *, line: int, source: Path) -> CaseResult:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}:{line} 每行必须是一个 JSON 对象。")
    case_id = raw.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"{source}:{line} 缺少非空的 case_id。")
    passed = raw.get("passed")
    if not isinstance(passed, bool):
        raise ValueError(f"{source}:{line} 的 passed 必须是布尔值。")
    cost = raw.get("cost_usd")
    latency = raw.get("latency_ms")
    if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)):
        raise ValueError(f"{source}:{line} 的 cost_usd 必须是数字或 null。")
    if latency is not None and (
        not isinstance(latency, (int, float)) or isinstance(latency, bool)
    ):
        raise ValueError(f"{source}:{line} 的 latency_ms 必须是数字或 null。")
    return CaseResult(
        case_id=case_id,
        passed=passed,
        cost_usd=float(cost) if cost is not None else None,
        latency_ms=float(latency) if latency is not None else None,
    )


def _iter_records(path: Path) -> list[tuple[int, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            rows = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} 不是合法 JSON：{exc}") from exc
        return list(enumerate(rows, start=1))
    records: list[tuple[int, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append((line_no, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
    return records


def load_results(path: str | Path, variant: str | None = None) -> SuiteResult:
    """读取一份评测结果。

    ``variant`` 缺省时用文件名（不含扩展名），让同一份文件在报告里可辨认。
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"评测结果文件不存在：{source}")
    suite = SuiteResult(variant=variant or source.stem, source=str(source))
    for line_no, raw in _iter_records(source):
        case = _coerce_case(raw, line=line_no, source=source)
        if case.case_id in suite.cases:
            raise ValueError(f"{source}:{line_no} 重复的 case_id：{case.case_id}")
        suite.cases[case.case_id] = case
    if not suite.cases:
        raise ValueError(f"评测结果为空：{source}")
    return suite
