"""配对评测与接受门。

核心问题：改了一版 prompt / 工具后，怎么判断「真的更好」而不是噪声？

做法与量化研究的过拟合防护同源：

1. 只比较**同一批用例**上的配对结果（缺一不可）。
2. 不一致对做 **McNemar 精确检验**，p < alpha 才算有证据。
3. 准确性之外再加**成本 / 延迟护栏**——准确率涨 2 个点但成本翻倍不算改进。
4. 结论分三档：``accept`` / ``reject`` / ``inconclusive``，后者明确表示
   「样本不足以判断」，不允许被当成通过。
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from data_agent.evalkit.models import SuiteResult
from data_agent.evalkit.stats import mcnemar_exact, wilson_interval


@dataclass(frozen=True)
class GatePolicy:
    """接受门参数。

    Attributes:
        alpha: 显著性阈值；越小越保守。
        max_cost_increase: 成本相对涨幅上限（0.10 = 允许涨 10%）。
        max_latency_increase: 延迟相对涨幅上限。
        min_cases: 少于该用例数直接判 ``inconclusive``，避免 3 条用例定乾坤。
    """

    alpha: float = 0.05
    max_cost_increase: float = 0.10
    max_latency_increase: float = 0.10
    min_cases: int = 8

    def __post_init__(self) -> None:
        if not 0 < self.alpha < 1:
            raise ValueError("alpha 必须位于 (0, 1)。")
        if self.max_cost_increase < 0 or self.max_latency_increase < 0:
            raise ValueError("成本 / 延迟涨幅上限不能为负。")
        if self.min_cases < 1:
            raise ValueError("min_cases 必须为正整数。")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationReport:
    decision: str
    reasons: list[str]
    metrics: dict[str, Any]
    policy: GatePolicy
    baseline_variant: str
    candidate_variant: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "policy": self.policy.to_dict(),
            "baseline_variant": self.baseline_variant,
            "candidate_variant": self.candidate_variant,
            "extra": dict(self.extra),
        }


def _paired_ids(baseline: SuiteResult, candidate: SuiteResult) -> list[str]:
    baseline_ids = set(baseline.cases)
    candidate_ids = set(candidate.cases)
    if baseline_ids != candidate_ids:
        missing_in_candidate = sorted(baseline_ids - candidate_ids)
        missing_in_baseline = sorted(candidate_ids - baseline_ids)
        parts = []
        if missing_in_candidate:
            parts.append(f"候选缺少 {len(missing_in_candidate)} 条：{missing_in_candidate[:5]}")
        if missing_in_baseline:
            parts.append(f"基线缺少 {len(missing_in_baseline)} 条：{missing_in_baseline[:5]}")
        raise ValueError("两次运行的用例集合必须完全一致：" + "；".join(parts))
    return sorted(baseline_ids)


def _mean_delta(
    paired: list[str],
    baseline: SuiteResult,
    candidate: SuiteResult,
    attribute: str,
) -> dict[str, Any] | None:
    baseline_values: list[float] = []
    candidate_values: list[float] = []
    for case_id in paired:
        base_value = getattr(baseline.cases[case_id], attribute)
        cand_value = getattr(candidate.cases[case_id], attribute)
        if base_value is None or cand_value is None:
            continue
        baseline_values.append(float(base_value))
        candidate_values.append(float(cand_value))
    if not baseline_values:
        return None
    baseline_mean = statistics.fmean(baseline_values)
    candidate_mean = statistics.fmean(candidate_values)
    if baseline_mean == 0:
        relative = 0.0 if candidate_mean == 0 else None
    else:
        relative = (candidate_mean - baseline_mean) / baseline_mean
    return {
        "n": len(baseline_values),
        "baseline_mean": round(baseline_mean, 6),
        "candidate_mean": round(candidate_mean, 6),
        "relative_delta": None if relative is None else round(relative, 6),
    }


def _guardrail_reason(
    label: str,
    stats: dict[str, Any] | None,
    limit: float,
) -> tuple[bool, str] | None:
    """返回 ``(是否超限, 说明)``；未超限且数据完整时返回 None。"""
    if stats is None:
        return (False, f"{label}数据缺失，跳过{label}护栏。")
    relative = stats["relative_delta"]
    if relative is None:
        return (True, f"{label}基线均值为 0、候选为正，视为超限（上限 {limit:.0%}）。")
    if relative > limit:
        return (
            True,
            f"{label}上涨 {relative:.1%} 超过上限 {limit:.0%}"
            f"（基线均值 {stats['baseline_mean']:.4g}，候选均值 {stats['candidate_mean']:.4g}）。",
        )
    return None


def suite_fingerprint(baseline: SuiteResult, candidate: SuiteResult, policy: GatePolicy) -> str:
    """对「用例集合 + 策略」做指纹，写入 ledger 便于确认两次比较同源。"""
    payload = {
        "cases": sorted(baseline.cases),
        "policy": policy.to_dict(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate(
    baseline: SuiteResult,
    candidate: SuiteResult,
    policy: GatePolicy | None = None,
) -> EvaluationReport:
    """按配对统计与成本护栏给出裁决。"""
    gate = policy or GatePolicy()
    paired = _paired_ids(baseline, candidate)
    total = len(paired)

    baseline_passes = sum(1 for case_id in paired if baseline.cases[case_id].passed)
    candidate_passes = sum(1 for case_id in paired if candidate.cases[case_id].passed)
    b = sum(
        1
        for case_id in paired
        if baseline.cases[case_id].passed and not candidate.cases[case_id].passed
    )
    c = sum(
        1
        for case_id in paired
        if not baseline.cases[case_id].passed and candidate.cases[case_id].passed
    )
    p_value = mcnemar_exact(b, c)

    metrics: dict[str, Any] = {
        "cases": total,
        "baseline_passes": baseline_passes,
        "candidate_passes": candidate_passes,
        "baseline_accuracy": round(baseline_passes / total, 6),
        "candidate_accuracy": round(candidate_passes / total, 6),
        "accuracy_delta": round((candidate_passes - baseline_passes) / total, 6),
        "wilson_baseline": [round(x, 6) for x in wilson_interval(baseline_passes, total)],
        "wilson_candidate": [round(x, 6) for x in wilson_interval(candidate_passes, total)],
        "discordant_baseline_only": b,
        "discordant_candidate_only": c,
        "mcnemar_p": round(p_value, 8),
    }
    cost_stats = _mean_delta(paired, baseline, candidate, "cost_usd")
    latency_stats = _mean_delta(paired, baseline, candidate, "latency_ms")
    metrics["cost"] = cost_stats
    metrics["latency"] = latency_stats

    reasons: list[str] = []
    if total < gate.min_cases:
        reasons.append(f"用例数 {total} 少于下限 {gate.min_cases}，样本不足，不做结论。")
        return EvaluationReport(
            decision="inconclusive",
            reasons=reasons,
            metrics=metrics,
            policy=gate,
            baseline_variant=baseline.variant,
            candidate_variant=candidate.variant,
        )

    guardrail_failures: list[str] = []
    for label, stats, limit in (
        ("成本", cost_stats, gate.max_cost_increase),
        ("延迟", latency_stats, gate.max_latency_increase),
    ):
        outcome = _guardrail_reason(label, stats, limit)
        if outcome is None:
            continue
        failed, reason = outcome
        if failed:
            guardrail_failures.append(reason)
        else:
            reasons.append(reason)

    if guardrail_failures:
        reasons.extend(guardrail_failures)
        reasons.append("护栏未通过：无论准确性是否提升，都不接受本次改动。")
        return EvaluationReport(
            decision="reject",
            reasons=reasons,
            metrics=metrics,
            policy=gate,
            baseline_variant=baseline.variant,
            candidate_variant=candidate.variant,
        )

    if p_value < gate.alpha and c > b:
        reasons.append(
            f"候选显著更好：{c} 条由失败转通过、{b} 条由通过转失败，"
            f"McNemar 精确检验 p={p_value:.4g} < {gate.alpha}。"
        )
        decision = "accept"
    elif p_value < gate.alpha and b > c:
        reasons.append(
            f"候选显著更差：{b} 条由通过转失败、{c} 条由失败转通过，"
            f"McNemar 精确检验 p={p_value:.4g} < {gate.alpha}。"
        )
        decision = "reject"
    else:
        if b == c:
            reasons.append(f"不一致对持平（b={b}, c={c}），没有可判定的方向性差异。")
        else:
            reasons.append(
                f"方向性差异不显著：b={b}, c={c}, p={p_value:.4g} ≥ {gate.alpha}，"
                "样本不足以排除随机波动。"
            )
        decision = "inconclusive"

    return EvaluationReport(
        decision=decision,
        reasons=reasons,
        metrics=metrics,
        policy=gate,
        baseline_variant=baseline.variant,
        candidate_variant=candidate.variant,
    )
