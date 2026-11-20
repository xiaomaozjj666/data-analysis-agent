"""内置演示目录：四份数据、预置口径/策略/血缘、20 万行生成。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent._registry_payloads import _AUDIT_SAMPLE_SALES_CSV
from data_agent.demo_insights import _histogram, _is_date, _unit, insights_for


def test_samples_catalog_and_unknown_kind(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    samples = client.get("/api/samples").json()["samples"]
    assert {item["kind"] for item in samples} == {
        "sales_audit",
        "regional_policy",
        "quality_injection",
        "growth",
        "grades",
        "finance",
        "bigdata",
    }
    assert client.post("/api/sessions/sample?kind=nope").status_code == 422


def test_regional_demo_ships_policy_and_schema(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    created = client.post("/api/sessions/sample?kind=regional_policy")
    assert created.status_code == 201
    assert created.json()["demo"]["kind"] == "regional_policy"
    session_id = created.json()["id"]

    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "销售额合计：13611.30 元\n订单数：8 笔\n客单价：1701.41 元"},
    ).json()
    assert audit["summary"] == {"verified": 3, "conflict": 0, "unsupported": 0}
    assert audit["source"]["rows_filtered"] == 12
    assert audit["source"]["preview"][0]["phone"] == "[已脱敏]"
    assert audit["policy"]["principal_id"] == "u_east"
    assert audit["schema_status"] == "approved"
    assert audit["lineage"]["upstream_table"] == "dwd.sales_orders"

    insights = created.json()["demo"]["insights"]
    kinds = [chart["type"] for chart in insights["charts"]]
    assert kinds[:3] == ["line", "bar", "histogram"]
    assert [kpi["label"] for kpi in insights["kpis"]][:2] == ["全部订单", "华东可见"]
    assert insights["scope"] == {"rows": 20, "visible": 8, "rls_column": "region"}
    assert insights["charts"][1]["title"] == "渠道对比"

    payload = created.json()
    assert payload["preview"][0]["phone"] == "[已脱敏]"
    assert {row["region"] for row in payload["preview"]} == {"华东"}
    assert payload["policy"]["principal_id"] == "u_east"


def test_quality_and_bigdata_demos(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    quality = client.post("/api/sessions/sample?kind=quality_injection").json()
    assert quality["demo"]["kind"] == "quality_injection"
    assert quality["profile"]["rows"] == 37
    charts = quality["demo"]["insights"]["charts"]
    assert {"line", "bar", "scatter", "histogram"} <= {chart["type"] for chart in charts}
    assert "忽略" in " ".join(quality["demo"]["insights"]["findings"])

    large = client.post("/api/sessions/sample?kind=bigdata").json()
    assert large["demo"]["kind"] == "bigdata"
    assert large["profile"]["rows"] == 200_000
    assert large["profile"]["columns"] == 4
    kinds = [chart["type"] for chart in large["demo"]["insights"]["charts"]]
    assert kinds[0] == "heatmap" and "bar" in kinds


def test_insight_helpers_and_sales_insights():
    sales = insights_for("sales_audit", _AUDIT_SAMPLE_SALES_CSV)
    assert sales is not None
    assert sales["charts"][0]["type"] == "line"
    assert sales["charts"][0]["title"] == "销售额趋势"

    assert _histogram([]) == []
    assert _histogram([5, 5])[0]["value"] == 2
    assert _is_date("2026-13-40") is False
    assert _unit("quantity") == "件"
    assert insights_for("unknown", "a" + chr(10) + "1" + chr(10)) is None
    assert insights_for("bigdata", "x,y" + chr(10)) is None
    assert (
        insights_for("quality_injection", "store,revenue,note" + chr(10) + ",," + chr(10)) is None
    )


def test_insights_validation_and_non_finite(tmp_path):
    rows = "order_id,sales,quantity" + chr(10)
    rows += chr(10).join(f"{index},{index},{index}" for index in range(1, 6)) + chr(10)
    result = insights_for("sales_audit", rows)
    assert result is not None
    assert result["kpis"][0]["label"] == "订单数"

    assert insights_for("sales_audit", "order_id,sales" + chr(10) + "A,1" + chr(10)) is None
    bad = "store,revenue,note" + chr(10)
    for index in range(6):
        bad += f"s{index},NaN," + chr(10)
    quality = insights_for("quality_injection", bad)
    assert quality is not None
    assert quality["kpis"][1]["value"] == "6"


def test_constant_axes_and_empty_policy_are_safe(tmp_path, monkeypatch):
    constant = "x,y,group" + chr(10)
    constant += chr(10).join("1,1,A" for _ in range(6)) + chr(10)
    insight = insights_for("bigdata", constant)
    assert insight is not None
    scatter = [chart for chart in insight["charts"] if chart["type"] == "scatter"][0]
    assert len(scatter["samples"]) == 6

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session = client.post("/api/sessions/sample?kind=sales_audit").json()
    workspace = api.registry.get(session["id"]).workspace
    (workspace.root / "audit_policy.json").write_text("{broken", encoding="utf-8")
    assert client.get(f"/api/sessions/{session['id']}").status_code == 200


def test_principal_switch_rewires_charts_and_preview(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    created = client.post("/api/sessions/sample?kind=regional_policy").json()
    session_id = created["id"]
    assert len(created["demo"]["principals"]) == 2

    full = client.post(
        f"/api/sessions/{session_id}/audit-policy/switch",
        json={"principal_id": "admin"},
    )
    assert full.status_code == 200
    payload = full.json()
    assert payload["insights"]["scope"]["visible"] == 20
    assert {row["region"] for row in payload["preview"]} == {"华东", "华南", "华北"}
    assert payload["preview"][0]["phone"] == "[已脱敏]"
    assert "区域对比" in [chart["title"] for chart in payload["insights"]["charts"]]

    scoped = client.post(
        f"/api/sessions/{session_id}/audit-policy/switch",
        json={"principal_id": "u_east"},
    ).json()
    assert scoped["insights"]["scope"]["visible"] == 8
    assert {row["region"] for row in scoped["preview"]} == {"华东"}

    assert (
        client.post(
            f"/api/sessions/{session_id}/audit-policy/switch",
            json={"principal_id": "nobody"},
        ).status_code
        == 404
    )


def test_policy_get_reports_corrupt_document(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session = client.post("/api/sessions/sample?kind=regional_policy").json()
    workspace = api.registry.get(session["id"]).workspace
    (workspace.root / "audit_policy.json").write_text("{broken", encoding="utf-8")
    assert client.get(f"/api/sessions/{session['id']}/audit-policy").status_code == 422


def test_auto_charts_merges_donut_tail_into_other():
    """>5 个分类时 donut 合并为 Top4 + 其他，避免 8 个扇区不可读。"""
    from data_agent.demo_insights import _auto_charts

    rows = [
        {"region": f"R{index % 6}", "sales": str(100 + index * 10), "qty": str(index + 1)}
        for index in range(18)
    ]
    charts = _auto_charts(rows)
    donut = [chart for chart in charts if chart["type"] == "donut"][0]
    assert len(donut["items"]) == 5
    assert donut["items"][-1]["label"] == "其他"
    assert "其余合并为其他" in donut["subtitle"]
    assert donut["dimension"] == "region"


def test_principal_switch_reports_unreadable_csv(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session = client.post("/api/sessions/sample?kind=regional_policy").json()
    workspace = api.registry.get(session["id"]).workspace
    workspace.source_path.unlink()
    response = client.post(
        f"/api/sessions/{session['id']}/audit-policy/switch",
        json={"principal_id": "admin"},
    )
    assert response.status_code == 422


def test_growth_demo_covers_ten_chart_types(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    samples = client.get("/api/samples").json()["samples"]
    assert "growth" in {item["kind"] for item in samples}

    created = client.post("/api/sessions/sample?kind=growth")
    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"]["rows"] == 144
    chart_types = {chart["type"] for chart in payload["demo"]["insights"]["charts"]}
    assert {
        "multiline",
        "stacked",
        "area",
        "treemap",
        "funnel",
        "radar",
        "gauge",
        "box",
        "bubble",
        "waterfall",
    } == chart_types
    assert payload["demo"]["insights"]["kpis"][0]["label"] == "收入"
    assert len(payload["artifacts"]) == 10
    assert all(item["kind"] == "visualization" for item in payload["artifacts"])


def test_grades_demo_has_scale_and_masks_pii(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    created = client.post("/api/sessions/sample?kind=grades")
    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"]["rows"] == 9000
    assert payload["demo"]["insights"]["kpis"][0]["value"] == "3000"
    assert {chart["type"] for chart in payload["demo"]["insights"]["charts"]} == {
        "histogram",
        "bar",
        "multiline",
        "radar",
        "scatter",
        "box",
        "heatmap",
        "gauge",
    }
    assert len(payload["artifacts"]) == 8
    assert payload["preview"][0]["name"] == "[已脱敏]"
    assert payload["preview"][0]["student_id"] == "[已脱敏]"
    reloaded = client.get(f"/api/sessions/{payload['id']}").json()
    assert reloaded["demo"]["insights"]["kpis"][0]["value"] == "3000"


def test_finance_demo_products(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    created = client.post("/api/sessions/sample?kind=finance")
    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"]["rows"] == 12
    assert {chart["type"] for chart in payload["demo"]["insights"]["charts"]} == {
        "multiline",
        "stacked",
        "waterfall",
        "bar",
        "gauge",
        "box",
        "donut",
    }
    assert len(payload["artifacts"]) == 7
    assert payload["demo"]["insights"]["kpis"][0]["label"] == "收入"
