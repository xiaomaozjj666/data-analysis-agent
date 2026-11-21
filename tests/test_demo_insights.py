"""演示预分析结论的回归：符号、措辞、主体切换后的 scope 与 KPI。"""

from __future__ import annotations

from data_agent.demo_catalog import (
    _REGIONAL_CSV,
    DEMOS,
    generate_finance,
    generate_grades,
)
from data_agent.demo_insights import insights_for

EAST_POLICY = DEMOS["regional_policy"].principals[0]["policy"]
ADMIN_POLICY = DEMOS["regional_policy"].principals[1]["policy"]


def test_finance_cash_change_uses_end_over_start():
    insight = insights_for("finance", generate_finance())
    assert insight is not None
    cash_finding = insight["findings"][0]
    # 现金流从约 492.6 万增长到约 1590.8 万，是 +223%，不是 -69%。
    assert "+223%" in cash_finding
    assert "-69%" not in cash_finding


def test_grades_findings_use_real_gap_and_real_spread():
    insight = insights_for("grades", generate_grades())
    assert insight is not None
    attendance_finding, spread_finding = insight["findings"]
    # 出勤高/低的学科均分差值约 0.007 分，不能输出自相矛盾的「低约 0.0 分」。
    assert "低约 0.0 分" not in attendance_finding
    assert "几乎无差异（差值 < 0.1 分）" in attendance_finding
    # 六科总体标准差：语文 11.13 > 数学 11.09 > ...，不能硬编码数学。
    assert spread_finding.startswith("语文的标准差最大")
    assert "11.13" in spread_finding
    assert "数学的标准差最大" not in spread_finding


def test_regional_insights_scope_carries_principal_and_pii():
    east = insights_for("regional_policy", _REGIONAL_CSV, EAST_POLICY)
    assert east is not None
    assert east["scope"] == {
        "rows": 20,
        "visible": 8,
        "rls_column": "region",
        "principal_id": "u_east",
        "pii_columns": ["phone"],
    }
    assert "只能看到自己的 8 行" in east["headline"]
    assert [item["label"] for item in east["kpis"]][1] == "华东可见"
    assert east["kpis"][1]["value"] == "8"
    assert east["kpis"][3]["value"] == "1"


def test_regional_insights_recompute_for_admin():
    admin = insights_for("regional_policy", _REGIONAL_CSV, ADMIN_POLICY)
    assert admin is not None
    assert admin["scope"] == {
        "rows": 20,
        "visible": 20,
        "rls_column": None,
        "principal_id": "admin",
        "pii_columns": ["phone"],
    }
    assert admin["headline"] == "管理员能看到全部 20 行"
    assert admin["kpis"][1] == {
        "label": "全部可见",
        "value": "20",
        "note": "无行级过滤",
        "unit": "行",
    }
    assert admin["kpis"][2]["label"] == "全部销售额"
    east = insights_for("regional_policy", _REGIONAL_CSV, EAST_POLICY)
    assert float(admin["kpis"][2]["value"].replace(",", "")) > float(
        east["kpis"][2]["value"].replace(",", "")
    )


def test_regional_insights_without_policy_default_to_full_scope():
    insight = insights_for("regional_policy", _REGIONAL_CSV)
    assert insight is not None
    assert insight["headline"] == "管理员能看到全部 20 行"
    assert insight["scope"]["visible"] == 20
    assert insight["scope"]["principal_id"] is None
    assert insight["scope"]["pii_columns"] == []
