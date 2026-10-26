"""evalkit：配对统计、接受门、台账与 CLI。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_agent.evalkit.cli import main
from data_agent.evalkit.harness import GatePolicy, evaluate, suite_fingerprint
from data_agent.evalkit.ledger import append_ledger, read_ledger, verify_ledger
from data_agent.evalkit.models import load_results
from data_agent.evalkit.stats import mcnemar_exact, wilson_interval


def _rows(passes: int, *, cost: float = 1.0, latency: float = 100.0) -> list[dict]:
    return [
        {
            "case_id": f"c{i:02d}",
            "passed": i < passes,
            "cost_usd": cost + i * 0.01,
            "latency_ms": latency + i,
        }
        for i in range(20)
    ]


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_mcnemar_exact_known_values() -> None:
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 6) == pytest.approx(0.03125)
    assert mcnemar_exact(6, 0) == pytest.approx(0.03125)
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(2, 2) == 1.0
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 0)


def test_wilson_interval_is_bounded() -> None:
    low, high = wilson_interval(14, 20)
    assert 0.0 <= low < 14 / 20 < high <= 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def test_load_results_jsonl_and_json_array(tmp_path: Path) -> None:
    jsonl = _write(tmp_path / "a.jsonl", _rows(3))
    loaded = load_results(jsonl)
    assert loaded.variant == "a"
    assert len(loaded.cases) == 20
    assert loaded.accuracy == pytest.approx(3 / 20)

    array = tmp_path / "b.json"
    array.write_text(json.dumps(_rows(4)), encoding="utf-8")
    assert load_results(array, variant="custom").variant == "custom"
    assert len(load_results(array).cases) == 20


def test_load_results_rejects_bad_schema(tmp_path: Path) -> None:
    dup = tmp_path / "dup.jsonl"
    rows = _rows(2)
    dup.write_text("\n".join(json.dumps(row) for row in [rows[0], rows[0]]), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_results(dup)

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"case_id": "x", "passed": "yes"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="布尔"):
        load_results(bad)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="为空"):
        load_results(empty)


def test_load_results_field_validation(tmp_path: Path) -> None:
    from data_agent.evalkit.models import SuiteResult

    with pytest.raises(ValueError, match="variant"):
        SuiteResult(variant=" ")
    assert SuiteResult(variant="x").accuracy == 0.0

    cases = {
        "not-object.json": ("[1]", "JSON 对象"),
        "no-id.json": ('{"passed": true}', "case_id"),
        "bad-cost.json": ('{"case_id": "x", "passed": true, "cost_usd": "1"}', "cost_usd"),
        "bad-latency.json": (
            '{"case_id": "x", "passed": true, "latency_ms": true}',
            "latency_ms",
        ),
        "broken.json": ("{not-json", "不是合法 JSON"),
        "broken-array.json": ('[{"case_id": "x"', "不是合法 JSON"),
    }
    for name, (content, message) in cases.items():
        path = tmp_path / name
        path.write_text(content + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_results(path)

    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n" + json.dumps(_rows(1)[0]) + "\n\n", encoding="utf-8")
    assert len(load_results(blank).cases) == 1


def _suite(passes: int, variant: str, **kwargs) -> object:
    from data_agent.evalkit.models import SuiteResult

    suite = SuiteResult(variant=variant)
    for row in _rows(passes, **kwargs):
        from data_agent.evalkit.models import CaseResult

        suite.cases[row["case_id"]] = CaseResult(
            case_id=row["case_id"],
            passed=row["passed"],
            cost_usd=row["cost_usd"],
            latency_ms=row["latency_ms"],
        )
    return suite


def test_evaluate_accepts_significant_improvement() -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand"))
    assert report.decision == "accept"
    assert report.metrics["discordant_baseline_only"] == 0
    assert report.metrics["discordant_candidate_only"] == 6


def test_evaluate_rejects_significant_regression() -> None:
    report = evaluate(_suite(20, "base"), _suite(14, "cand"))
    assert report.decision == "reject"
    assert report.metrics["discordant_baseline_only"] == 6


def test_evaluate_inconclusive_without_evidence() -> None:
    report = evaluate(_suite(18, "base"), _suite(20, "cand"))
    assert report.decision == "inconclusive"
    assert "不显著" in " ".join(report.reasons)


def test_evaluate_rejects_cost_guardrail() -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand", cost=1.8))
    assert report.decision == "reject"
    assert any("成本" in reason for reason in report.reasons)


def test_evaluate_enforces_min_cases() -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand"), GatePolicy(min_cases=30))
    assert report.decision == "inconclusive"
    assert any("少于下限" in reason for reason in report.reasons)


def test_evaluate_requires_paired_cases() -> None:
    base = _suite(14, "base")
    cand = _suite(20, "cand")
    cand.cases.pop("c00")
    with pytest.raises(ValueError, match="集合必须完全一致"):
        evaluate(base, cand)

    base = _suite(14, "base")
    cand = _suite(20, "cand")
    base.cases.pop("c00")
    with pytest.raises(ValueError, match="基线缺少"):
        evaluate(base, cand)


def test_evaluate_missing_cost_data_is_reported_not_fatal() -> None:
    import dataclasses

    base = _suite(14, "base")
    for case_id, case in list(base.cases.items()):
        base.cases[case_id] = dataclasses.replace(case, cost_usd=None)
    report = evaluate(base, _suite(20, "cand"))
    assert report.decision == "accept"
    assert any("成本数据缺失" in reason for reason in report.reasons)


def test_evaluate_zero_baseline_cost_is_guardrail_failure() -> None:
    import dataclasses

    base = _suite(14, "base")
    for case_id, case in list(base.cases.items()):
        base.cases[case_id] = dataclasses.replace(case, cost_usd=0.0)
    report = evaluate(base, _suite(20, "cand", cost=1.0))
    assert report.decision == "reject"
    assert any("基线均值为 0" in reason for reason in report.reasons)


def test_evaluate_equal_discordant_pairs() -> None:
    import dataclasses

    base = _suite(19, "base")
    cand = _suite(20, "cand")
    cand.cases["c00"] = dataclasses.replace(cand.cases["c00"], passed=False)
    report = evaluate(base, cand)
    assert report.decision == "inconclusive"
    assert any("持平" in reason for reason in report.reasons)


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        GatePolicy(alpha=0)
    with pytest.raises(ValueError):
        GatePolicy(max_cost_increase=-0.1)
    with pytest.raises(ValueError):
        GatePolicy(min_cases=0)


def test_suite_fingerprint_is_stable() -> None:
    base = _suite(14, "base")
    cand = _suite(20, "cand")
    policy = GatePolicy()
    first = suite_fingerprint(base, cand, policy)
    assert first == suite_fingerprint(base, cand, policy)
    assert len(first) == 64


def test_ledger_append_and_verify(tmp_path: Path) -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand"))
    path = tmp_path / "ledger.jsonl"
    entry = append_ledger(path, report, suite_fingerprint="f" * 64, git_sha="abc1234")
    assert entry["decision"] == "accept"
    assert read_ledger(path)[0]["git_sha"] == "abc1234"
    assert verify_ledger(path)["ok"] is True

    second = append_ledger(path, report, suite_fingerprint="f" * 64)
    assert second["prev"] == entry["hash"]
    assert verify_ledger(path)["entries"] == 2


def test_ledger_tamper_is_detected(tmp_path: Path) -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand"))
    path = tmp_path / "ledger.jsonl"
    append_ledger(path, report, suite_fingerprint="f" * 64)
    entry = read_ledger(path)[0]
    entry["decision"] = "reject"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    result = verify_ledger(path)
    assert result["ok"] is False
    assert result["errors"]


def test_ledger_edge_cases(tmp_path: Path) -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand"))
    assert read_ledger(tmp_path / "missing.jsonl") == []

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    first = append_ledger(empty, report, suite_fingerprint="f" * 64)
    assert first["prev"] == ""

    broken_tail = tmp_path / "broken-tail.jsonl"
    broken_tail.write_text("garbage\n", encoding="utf-8")
    assert append_ledger(broken_tail, report, suite_fingerprint="f" * 64)["prev"] == ""

    sparse = tmp_path / "sparse.jsonl"
    sparse.write_text("\n" + json.dumps(first) + "\n\n", encoding="utf-8")
    assert len(read_ledger(sparse)) == 1

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        read_ledger(invalid)

    prev_broken = tmp_path / "prev.jsonl"
    entry = dict(first)
    entry["prev"] = "wrong"
    prev_broken.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert any("prev" in error for error in verify_ledger(prev_broken)["errors"])


def test_cli_compare_exit_codes_and_ledger(tmp_path: Path, capsys) -> None:
    base = _write(tmp_path / "base.jsonl", _rows(14))
    cand = _write(tmp_path / "cand.jsonl", _rows(20))
    ledger = tmp_path / "ledger.jsonl"
    code = main(["compare", "--baseline", str(base), "--candidate", str(cand), "--ledger", str(ledger)])
    assert code == 0
    out = capsys.readouterr().out
    assert "ACCEPT" in out
    assert verify_ledger(ledger)["entries"] == 1

    regressed = _write(tmp_path / "regressed.jsonl", _rows(8))
    assert main(["compare", "--baseline", str(base), "--candidate", str(regressed)]) == 1

    ambiguous = _write(tmp_path / "ambiguous.jsonl", _rows(16))
    assert main(["compare", "--baseline", str(base), "--candidate", str(ambiguous)]) == 2


def test_cli_json_output_and_policy_errors(tmp_path: Path, capsys) -> None:
    base = _write(tmp_path / "base.jsonl", _rows(14))
    cand = _write(tmp_path / "cand.jsonl", _rows(20))
    ledger = tmp_path / "ledger.jsonl"
    assert (
        main(
            [
                "compare",
                "--baseline",
                str(base),
                "--candidate",
                str(cand),
                "--json",
                "--ledger",
                str(ledger),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "accept"
    assert payload["metrics"]["mcnemar_p"] < 0.05
    assert payload["ledger"]["hash"]

    policy = tmp_path / "policy.json"
    policy.write_text('{"alpha": 0.05, "unknown": 1}', encoding="utf-8")
    assert main(["compare", "--baseline", str(base), "--candidate", str(cand), "--policy", str(policy)]) == 2
    assert "未知字段" in capsys.readouterr().err

    not_object = tmp_path / "not-object.json"
    not_object.write_text("[1, 2]", encoding="utf-8")
    assert main(["compare", "--baseline", str(base), "--candidate", str(cand), "--policy", str(not_object)]) == 2
    assert "JSON 对象" in capsys.readouterr().err

    valid_policy = tmp_path / "valid-policy.json"
    valid_policy.write_text('{"alpha": 0.05, "min_cases": 8}', encoding="utf-8")
    assert (
        main(
            [
                "compare",
                "--baseline",
                str(base),
                "--candidate",
                str(cand),
                "--policy",
                str(valid_policy),
            ]
        )
        == 0
    )

    assert main(["compare", "--baseline", str(tmp_path / "missing.jsonl"), "--candidate", str(cand)]) == 2


def test_cli_verify_ledger(tmp_path: Path, capsys) -> None:
    report = evaluate(_suite(14, "base"), _suite(20, "cand"))
    path = tmp_path / "ledger.jsonl"
    append_ledger(path, report, suite_fingerprint="f" * 64)
    assert main(["verify-ledger", str(path)]) == 0
    assert "OK" in capsys.readouterr().out
    assert main(["verify-ledger", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 1

    entry = read_ledger(path)[0]
    entry["decision"] = "reject"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert main(["verify-ledger", str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "hash" in out
