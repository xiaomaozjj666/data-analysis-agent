"""内置演示目录：四份数据、预置口径/策略/血缘、20 万行生成。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api


def test_samples_catalog_and_unknown_kind(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    samples = client.get("/api/samples").json()["samples"]
    assert {item["kind"] for item in samples} == {
        "sales_audit",
        "regional_policy",
        "quality_injection",
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


def test_quality_and_bigdata_demos(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    quality = client.post("/api/sessions/sample?kind=quality_injection").json()
    assert quality["demo"]["kind"] == "quality_injection"
    assert quality["profile"]["rows"] == 37

    large = client.post("/api/sessions/sample?kind=bigdata").json()
    assert large["demo"]["kind"] == "bigdata"
    assert large["profile"]["rows"] == 200_000
    assert large["profile"]["columns"] == 4
