"""``data-agent-eval`` 命令行入口。

用法::

    data-agent-eval compare --baseline eval/baseline.jsonl \
        --candidate eval/candidate.jsonl --ledger eval/ledger.jsonl

退出码：0 = accept，1 = reject，2 = inconclusive（或参数/文件错误）。
CI 里应当只把 0 当作通过——把「样本不足」当成通过等于自欺。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from data_agent.evalkit.harness import GatePolicy, evaluate, suite_fingerprint
from data_agent.evalkit.ledger import append_ledger, verify_ledger
from data_agent.evalkit.models import load_results, write_results

_EXIT_BY_DECISION = {"accept": 0, "reject": 1, "inconclusive": 2}
_POLICY_FIELDS = {
    "alpha",
    "max_cost_increase",
    "max_latency_increase",
    "min_cases",
}


def _load_policy(path: str | None) -> GatePolicy:
    if not path:
        return GatePolicy()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("策略文件顶层必须是 JSON 对象。")
    unknown = set(payload) - _POLICY_FIELDS
    if unknown:
        raise ValueError(f"策略文件包含未知字段：{sorted(unknown)}")
    return GatePolicy(**payload)


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1%}"


def _print_report(report: Any, ledger_entry: dict[str, Any] | None) -> None:
    metrics = report.metrics
    print(f"裁决: {report.decision.upper()}")
    print(
        f"用例 {metrics['cases']} 条 | 基线 {metrics['baseline_passes']} 通过 "
        f"({metrics['baseline_accuracy']:.1%}) | 候选 {metrics['candidate_passes']} 通过 "
        f"({metrics['candidate_accuracy']:.1%}) | Δ {metrics['accuracy_delta']:+.1%}"
    )
    print(
        f"不一致对 b={metrics['discordant_baseline_only']} "
        f"c={metrics['discordant_candidate_only']} | McNemar p={metrics['mcnemar_p']:.4g}"
    )
    cost = metrics.get("cost")
    latency = metrics.get("latency")
    print(
        f"成本 {_format_percent(cost['relative_delta'] if cost else None)} | "
        f"延迟 {_format_percent(latency['relative_delta'] if latency else None)}"
    )
    for reason in report.reasons:
        print(f"  - {reason}")
    if ledger_entry is not None:
        print(f"台账已写入，hash={ledger_entry['hash'][:12]}…")


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline = load_results(args.baseline, variant=args.baseline_label)
    candidate = load_results(args.candidate, variant=args.candidate_label)
    policy = _load_policy(args.policy)
    report = evaluate(baseline, candidate, policy)
    fingerprint = suite_fingerprint(baseline, candidate, policy)
    ledger_entry = None
    if args.ledger:
        ledger_entry = append_ledger(
            args.ledger,
            report,
            suite_fingerprint=fingerprint,
            git_sha=args.git_sha,
            notes=args.notes,
        )
    if args.json:
        payload = report.to_dict()
        payload["suite_fingerprint"] = fingerprint
        if ledger_entry is not None:
            payload["ledger"] = ledger_entry
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_report(report, ledger_entry)
    return _EXIT_BY_DECISION[report.decision]


def _cmd_run_offline(args: argparse.Namespace) -> int:
    if not 0.0 <= args.min_accuracy <= 1.0:
        raise ValueError("min-accuracy 必须在 [0, 1] 内。")
    if args.min_cases < 1:
        raise ValueError("min-cases 必须为正整数。")
    # 延迟导入完整 Agent 栈，compare/verify-ledger 仍保持轻量。
    from data_agent.evalkit.offline import run_offline_suite

    suite = run_offline_suite(args.suite, work_dir=args.work_dir)
    if args.output:
        write_results(args.output, suite)
    passed = sum(case.passed for case in suite.cases.values())
    total = len(suite.cases)
    gate_passed = total >= args.min_cases and suite.accuracy >= args.min_accuracy
    summary = {
        "suite_id": suite.variant,
        "mode": "offline_fake",
        "cases": total,
        "passed": passed,
        "accuracy": round(suite.accuracy, 6),
        "min_cases": args.min_cases,
        "min_accuracy": args.min_accuracy,
        "gate_passed": gate_passed,
        "output": args.output or "",
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        state = "PASS" if gate_passed else "FAIL"
        print(
            f"离线评测: {state} | {suite.variant} | {passed}/{total} 通过 "
            f"({suite.accuracy:.1%}) | 阈值 case≥{args.min_cases}, accuracy≥{args.min_accuracy:.1%}"
        )
        for case in suite.cases.values():
            if not case.passed:
                print(f"  - {case.case_id}: {'; '.join(case.details.get('failures', []))}")
    return 0 if gate_passed else 1


def _cmd_verify_ledger(args: argparse.Namespace) -> int:
    result = verify_ledger(args.path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        state = "OK" if result["ok"] else "FAILED"
        print(f"台账校验: {state}（{result['entries']} 条）")
        for error in result["errors"]:
            print(f"  - {error}")
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-agent-eval",
        description="Agent 评测的配对统计护栏：只有显著且不超成本 / 延迟预算的改动才放行。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare = subparsers.add_parser("compare", help="比较基线与候选两次评测结果")
    compare.add_argument("--baseline", required=True, help="基线结果 JSONL / JSON")
    compare.add_argument("--candidate", required=True, help="候选结果 JSONL / JSON")
    compare.add_argument("--baseline-label", default=None, help="基线展示名（默认文件名）")
    compare.add_argument("--candidate-label", default=None, help="候选展示名（默认文件名）")
    compare.add_argument("--policy", default=None, help="接受门参数 JSON")
    compare.add_argument("--ledger", default=None, help="追加裁决台账的 JSONL 路径")
    compare.add_argument("--git-sha", default="", help="被评测代码的 git sha")
    compare.add_argument("--notes", default="", help="本次改动的说明")
    compare.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    compare.set_defaults(handler=_cmd_compare)

    offline = subparsers.add_parser("run-offline", help="用脚本化模型执行真实 Agent 工作流")
    offline.add_argument("--suite", required=True, help="离线用例套件 JSON")
    offline.add_argument("--output", default=None, help="写出真实运行结果 JSONL")
    offline.add_argument("--work-dir", default=None, help="可选工作目录（默认自动清理）")
    offline.add_argument("--min-accuracy", type=float, default=1.0, help="最低通过率")
    offline.add_argument("--min-cases", type=int, default=8, help="最少用例数")
    offline.add_argument("--json", action="store_true", help="输出机器可读摘要")
    offline.set_defaults(handler=_cmd_run_offline)

    verify = subparsers.add_parser("verify-ledger", help="校验台账哈希链")
    verify.add_argument("path")
    verify.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    verify.set_defaults(handler=_cmd_verify_ledger)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
