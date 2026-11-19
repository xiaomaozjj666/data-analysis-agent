"""来源目录血缘与新鲜度：核验结果、账本与离线验签共用同一指纹。"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent.audit_lineage import freshness, lineage_fingerprint, parse_lineage

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import verify_audit_ledger  # noqa: E402

LINEAGE = {
    "catalog_urn": "urn:li:dataset:(warehouse,dwd.orders_daily,PROD)",
    "upstream_table": "dwd.orders_daily",
    "extracted_at": "2020-01-01T00:00:00+00:00",
    "owner": "data-platform",
    "freshness_sla_hours": 24,
}
SALES = b"order_id,sales,quantity\nA,10.00,1\n"


def test_parse_and_freshness():
    lineage = parse_lineage(LINEAGE)
    assert lineage["upstream_table"] == "dwd.orders_daily"
    assert lineage_fingerprint(lineage) == lineage_fingerprint(parse_lineage(LINEAGE))
    assert freshness(lineage)["stale"] is True

    now = datetime.now(UTC)
    fresh = parse_lineage({**LINEAGE, "extracted_at": now.isoformat(), "freshness_sla_hours": 24})
    state = freshness(fresh, now=now + timedelta(hours=1))
    assert state["stale"] is False
    assert state["age_hours"] == 1.0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"upstream_table": "t", "extracted_at": "2026-11-19T08:00:00"},
        {"upstream_table": "t", "extracted_at": "not-a-date"},
        {"upstream_table": "t", "extracted_at": "2026-11-19T08:00:00+08:00", "freshness_sla_hours": 0},
    ],
)
def test_invalid_lineage(payload):
    with pytest.raises(ValueError):
        parse_lineage(payload)


def test_audit_marks_stale_source_and_ledger_binds_lineage(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions", files={"file": ("sales.csv", SALES, "text/csv")}
    )
    session_id = upload.json()["id"]

    saved = client.put(f"/api/sessions/{session_id}/audit-lineage", json=LINEAGE)
    assert saved.status_code == 200
    assert saved.json()["lineage"]["stale"] is True

    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit", json={"draft": "订单数：1 笔"}
    ).json()
    assert audit["lineage"]["upstream_table"] == "dwd.orders_daily"
    assert audit["lineage"]["stale"] is True
    assert any(w["code"] == "source_stale" for w in audit["warnings"])

    client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": "订单数：1 笔", "audit_id": audit["audit_id"]},
    )
    workspace = api.registry.get(session_id).workspace
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes((workspace.root / "audit_ledger.jsonl").read_bytes())
    assert any(
        entry.get("lineage_sha256") for entry in [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    )

    lineage_file = tmp_path / "audit_lineage.json"
    lineage_file.write_text(json.dumps(LINEAGE), encoding="utf-8")
    assert verify_audit_ledger.main([str(ledger), "--lineage", str(lineage_file)]) == 0

    tampered = {**LINEAGE, "owner": "someone-else"}
    lineage_file.write_text(json.dumps(tampered), encoding="utf-8")
    assert verify_audit_ledger.main([str(ledger), "--lineage", str(lineage_file)]) == 1


def test_lineage_api_rejects_invalid_payload(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions", files={"file": ("sales.csv", SALES, "text/csv")}
    ).json()["id"]
    response = client.put(
        f"/api/sessions/{session_id}/audit-lineage",
        json={"upstream_table": "t", "extracted_at": "2026-11-19T08:00:00"},
    )
    assert response.status_code == 422
