"""离线 eval 必须执行真实工作流并以退出码实施门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_agent.evalkit.cli import main
from data_agent.evalkit.models import load_results, write_results
from data_agent.evalkit.offline import load_offline_cases, run_offline_suite

SUITE = Path(__file__).resolve().parents[1] / "eval" / "examples" / "suite.json"


def test_offline_suite_executes_real_workflow_without_llm_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = run_offline_suite(SUITE, work_dir=tmp_path)

    assert result.variant == "offline-workflow-v1"
    assert len(result.cases) == 8
    assert result.accuracy == 1.0
    for case in result.cases.values():
        assert case.cost_usd == 0.0
        assert case.latency_ms is not None and case.latency_ms > 0
        assert case.details["mode"] == "offline_fake"
        assert case.details["actual"]["tool_calls"] == ["inspect_data"]
        assert case.details["failures"] == []


def test_offline_cli_writes_real_results_and_enforces_min_cases(tmp_path, capsys) -> None:
    output = tmp_path / "results.jsonl"
    args = [
        "run-offline",
        "--suite",
        str(SUITE),
        "--output",
        str(output),
        "--min-accuracy",
        "1.0",
        "--min-cases",
        "8",
        "--json",
    ]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["gate_passed"] is True
    loaded = load_results(output)
    assert len(loaded.cases) == 8
    assert loaded.cases["sales-basic"].details["actual"]["dataset_rows"] == 4

    assert main(["run-offline", "--suite", str(SUITE), "--min-cases", "9"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_offline_gate_fails_when_actual_output_violates_assertion(tmp_path) -> None:
    payload = json.loads(SUITE.read_text(encoding="utf-8"))
    payload["cases"][0]["expect"]["tool_calls"] = ["not_a_real_tool"]
    payload["cases"][0]["expect"]["response_contains"] = ["missing-response-fragment"]
    broken = tmp_path / "broken-suite.json"
    broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "failed-results.jsonl"

    assert main(["run-offline", "--suite", str(broken), "--output", str(output)]) == 1
    result = load_results(output)
    assert result.cases["sales-basic"].passed is False
    assert "not_a_real_tool" in result.cases["sales-basic"].details["failures"][0]
    assert all(case.passed for case_id, case in result.cases.items() if case_id != "sales-basic")


def test_offline_suite_rejects_inconsistent_real_structure(tmp_path) -> None:
    payload = json.loads(SUITE.read_text(encoding="utf-8"))
    payload["cases"][0]["expect"]["rows"] = 999
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="行列数"):
        load_offline_cases(invalid)

    payload = json.loads(SUITE.read_text(encoding="utf-8"))
    payload["cases"][1]["case_id"] = payload["cases"][0]["case_id"]
    invalid.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_offline_cases(invalid)


def test_result_writer_preserves_evidence_and_rejects_bad_details(tmp_path) -> None:
    suite = run_offline_suite(SUITE)
    output = tmp_path / "roundtrip.jsonl"
    write_results(output, suite)
    loaded = load_results(output)
    assert loaded.cases["sales-basic"].details["mode"] == "offline_fake"

    bad = tmp_path / "bad-details.jsonl"
    bad.write_text('{"case_id":"x","passed":true,"details":[]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="details"):
        load_results(bad)


def test_offline_cli_validates_thresholds() -> None:
    assert main(["run-offline", "--suite", str(SUITE), "--min-accuracy", "1.1"]) == 2
    assert main(["run-offline", "--suite", str(SUITE), "--min-cases", "0"]) == 2


def test_offline_suite_rejects_every_malformed_schema_branch(tmp_path) -> None:
    base = json.loads(SUITE.read_text(encoding="utf-8"))

    def changed(path: tuple[str, ...], value: object) -> object:
        payload = json.loads(json.dumps(base))
        if path == ("case",):
            payload["cases"][0] = value
            return payload
        target = payload["cases"][0]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return payload

    invalid_payloads = [
        [],
        {"schema_version": 1, "suite_id": "", "cases": []},
        {"schema_version": 1, "suite_id": "empty", "cases": []},
        changed(("case",), 1),
        changed(("case_id",), "invalid id"),
        changed(("task",), ""),
        changed(("dataset",), None),
        changed(("dataset", "columns"), []),
        changed(("dataset", "rows"), []),
        changed(("dataset", "rows"), [[1]]),
        changed(("expect",), None),
        changed(("expect", "column_names"), ["wrong"]),
        changed(("expect", "tool_calls"), [1]),
    ]
    path = tmp_path / "schema-error.json"
    for payload in invalid_payloads:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            load_offline_cases(path)


def test_offline_runner_converts_unexpected_case_error_to_failed_result(
    tmp_path, monkeypatch
) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("offline boom")

    monkeypatch.setattr("data_agent.evalkit.offline._run_case", explode)
    result = run_offline_suite(SUITE, work_dir=tmp_path)
    assert result.accuracy == 0.0
    assert len(result.cases) == 8
    for case in result.cases.values():
        assert case.passed is False
        assert case.details["actual"] == {}
        assert case.details["failures"] == ["RuntimeError: offline boom"]
