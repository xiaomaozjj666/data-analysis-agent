from __future__ import annotations

import json

from data_agent.facts import extract_facts
from data_agent.verification import render_verification_markdown, save_verification, verify_report


def test_verified_with_rounding_units_and_evidence_location():
    facts = extract_facts(
        [
            {
                "type": "tool_result",
                "name": "regression",
                "detail": {
                    "p_value": 0.03456,
                    "p_value_unit": "ratio",
                    "means": {"revenue": 12345},
                    "units": {"mean:revenue": "元"},
                },
            }
        ]
    )

    result = verify_report("p值为3.5%；revenue均值为1.2万元。", facts)

    assert result["summary"] == {"verified": 2, "conflict": 0, "unsupported": 0, "total": 2}
    assert {claim["verdict"] for claim in result["claims"]} == {"verified"}
    assert result["claims"][0]["evidence"][0]["json_path"] == "/p_value"
    assert result["claims"][1]["evidence"][0]["json_path"] == "/means/revenue"
    assert "verified=2" in render_verification_markdown(result)


def test_changed_value_is_conflict_with_fact_evidence():
    facts = extract_facts(
        [{"type": "tool_result", "name": "statistical_analysis", "detail": {"p_value": 0.03}}]
    )

    result = verify_report("结论是p值为0.05。", facts)

    assert result["summary"]["conflict"] == 1
    claim = result["claims"][0]
    assert claim["verdict"] == "conflict"
    assert claim["evidence"] == [
        {
            "source": "statistical_analysis",
            "field": "p_value",
            "value": "0.03",
            "trace_index": 0,
            "json_path": "/p_value",
        }
    ]


def test_conflicting_facts_win_even_when_one_value_matches():
    facts = extract_facts(
        [
            {"type": "tool_result", "name": "analysis_a", "detail": {"p_value": 0.03}},
            {"type": "tool_result", "name": "analysis_b", "detail": {"p_value": 0.05}},
        ]
    )

    result = verify_report("p值为0.03。", facts)

    assert result["claims"][0]["verdict"] == "conflict"
    assert {item["value"] for item in result["claims"][0]["evidence"]} == {"0.03", "0.05"}


def test_unsupported_when_no_fact_matches_and_unit_requires_fact_unit():
    unsupported = verify_report(
        "未知指标为999；另一个未知指标为10。",
        [{"source": "inspect", "field": "rows", "value": "10"}],
    )
    assert [claim["verdict"] for claim in unsupported["claims"]] == ["unsupported", "unsupported"]
    assert all(claim["evidence"] == [] for claim in unsupported["claims"])

    no_unit = extract_facts(
        [{"type": "tool_result", "name": "groupby", "detail": {"means": {"revenue": 12000}}}]
    )
    result = verify_report("revenue均值为1.2万元。", no_unit)
    assert result["claims"][0]["verdict"] == "conflict"


def test_numeric_evidence_is_scoped_to_its_clause():
    result = verify_report(
        "另一处p值为0.05；未知指标为10。",
        [{"source": "stats", "field": "p_value", "value": "0.035", "unit": "ratio"}],
    )
    assert [claim["verdict"] for claim in result["claims"]] == ["conflict", "unsupported"]
    assert result["claims"][1]["evidence"] == []


def test_missing_or_incompatible_fact_units_never_verify():
    missing = verify_report(
        "rows为10条。",
        [{"source": "inspect", "field": "rows", "value": "10"}],
    )
    incompatible = verify_report(
        "rows为10条。",
        [
            {"source": "a", "field": "rows", "value": "10", "unit": "条"},
            {"source": "b", "field": "rows", "value": "10", "unit": "元"},
        ],
    )
    assert missing["claims"][0]["verdict"] == "conflict"
    assert incompatible["claims"][0]["verdict"] == "conflict"


def test_unit_presence_mismatch_between_facts_is_conflict():
    result = verify_report(
        "rows为10条。",
        [
            {"source": "a", "field": "rows", "value": "10"},
            {"source": "b", "field": "rows", "value": "10", "unit": "条"},
        ],
    )
    assert result["claims"][0]["verdict"] == "conflict"
    assert {item["source"] for item in result["claims"][0]["evidence"]} == {"a", "b"}


def test_unknown_fact_unit_is_conflict_even_for_equal_values():
    result = verify_report(
        "rows为10格。",
        [
            {"source": "a", "field": "rows", "value": "10", "unit": "格"},
            {"source": "b", "field": "rows", "value": "10", "unit": "格"},
        ],
    )
    assert result["claims"][0]["verdict"] == "conflict"


def test_text_fact_verified_and_generated_appendix_not_reaudited(tmp_path):
    facts = extract_facts(
        [
            {
                "type": "tool_result",
                "name": "statistical_analysis",
                "detail": {"method": "独立样本t检验"},
            }
        ]
    )
    result = verify_report("采用独立样本t检验。", facts)
    assert result["claims"][0]["verdict"] == "verified"
    assert verify_report(render_verification_markdown(result), facts)["claims"] == []

    from data_agent.workspace import DataWorkspace

    workspace = DataWorkspace(tmp_path / "runs", session_id="proof")
    artifact = save_verification(workspace, result)
    assert artifact["kind"] == "verification"
    saved = json.loads(
        (workspace.artifacts_dir / "report-verification.json").read_text(encoding="utf-8")
    )
    assert saved["summary"]["verified"] == 1
    assert saved["claims"][0]["evidence"][0]["json_path"] == "/method"


def test_code_blocks_are_not_numeric_claims():
    result = verify_report("```text\n999\n```", [])
    assert result["claims"] == []
    assert render_verification_markdown(result) == ""


def test_leading_decimal_is_verified():
    facts = extract_facts(
        [{"type": "tool_result", "name": "statistical_analysis", "detail": {"p_value": ".5"}}]
    )
    assert verify_report("p值为.5。", facts)["claims"][0]["verdict"] == "verified"
