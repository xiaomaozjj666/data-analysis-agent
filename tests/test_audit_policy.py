"""访问策略：RLS 过滤、PII 预览脱敏、主体与过滤指纹入账。"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent.audit_policy import parse_policy, rls_filter_hash

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "independent_replay.py"
NL = chr(10)
CSV = (
    "customer_id,region,phone,revenue"
    + NL
    + "C1,East,13800000000,100"
    + NL
    + "C2,West,13900000000,50"
    + NL
).encode()
SCHEMA = {
    "metrics": [
        {"name": "记录数", "column": "customer_id", "aggregation": "count", "unit": "条"},
        {"name": "收入合计", "column": "revenue", "aggregation": "sum", "unit": "元"},
    ]
}
POLICY = {
    "principal_id": "u_east",
    "claims": {"region": "East"},
    "pii_columns": ["phone"],
    "rls": {"column": "region", "claim": "region"},
}


def _session(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions", files={"file": ("customers.csv", CSV, "text/csv")}
    ).json()["id"]
    assert client.put(f"/api/sessions/{session_id}/audit-schema", json=SCHEMA).status_code == 200
    return client, session_id


def test_policy_validation():
    with pytest.raises(ValueError, match="principal_id"):
        parse_policy({"claims": {}})
    with pytest.raises(ValueError, match="claim"):
        parse_policy({"principal_id": "u", "claims": {}, "rls": {"column": "r", "claim": "r"}})
    with pytest.raises(ValueError, match="pii_columns"):
        parse_policy({"principal_id": "u", "claims": {}, "pii_columns": "phone"})


def test_rls_filters_rows_and_masks_pii(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    assert client.put(f"/api/sessions/{session_id}/audit-policy", json=POLICY).status_code == 200

    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "记录数：1 条" + NL + "收入合计：100.00 元"},
    ).json()
    assert audit["summary"] == {"verified": 2, "conflict": 0, "unsupported": 0}
    assert audit["source"]["rows_filtered"] == 1
    assert audit["source"]["preview"][0]["phone"] == "[已脱敏]"
    assert audit["policy"]["principal_id"] == "u_east"

    entry = json.loads(
        (api.registry.get(session_id).workspace.root / "audit_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert entry["principal_id"] == "u_east"
    assert entry["rls_filter_hash"] == rls_filter_hash("region", "East")
    assert entry["policy_sha256"]


def test_policy_works_with_builtin_sales_rules_without_schema(tmp_path, monkeypatch):
    """有策略、没声明式口径时回退内置销售口径，不能再一律 422。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    content = (
        "order_id,sales,quantity,region" + NL
        + "A,10.00,1,East" + NL
        + "B,5.00,1,West" + NL
        + "C,2.00,1,East" + NL
    ).encode()
    session_id = client.post(
        "/api/sessions", files={"file": ("sales.csv", content, "text/csv")}
    ).json()["id"]
    assert client.put(f"/api/sessions/{session_id}/audit-policy", json=POLICY).status_code == 200
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "订单数：2 笔" + NL + "销售额合计：12.00 元"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["summary"] == {"verified": 2, "conflict": 0, "unsupported": 0}
    assert result["source"]["rows_filtered"] == 1
    assert [row["order_id"] for row in result["source"]["preview"]] == ["A", "C"]


def test_policy_on_non_sales_csv_returns_clear_422(tmp_path, monkeypatch):
    """成绩表不是销售表：应给「缺少列」的 422，而不是 500。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    content = (
        "student_id,exam,语文" + NL + "S1,月考,90" + NL + "S2,月考,80" + NL
    ).encode()
    session_id = client.post(
        "/api/sessions", files={"file": ("grades.csv", content, "text/csv")}
    ).json()["id"]
    policy = {"principal_id": "u", "claims": {}, "pii_columns": ["student_id"], "rls": None}
    assert client.put(f"/api/sessions/{session_id}/audit-policy", json=policy).status_code == 200
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit", json={"draft": "参考人数：2 人"}
    )
    assert response.status_code == 422
    assert "order_id" in response.json()["detail"]


def test_bundle_replays_with_policy(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    client.put(f"/api/sessions/{session_id}/audit-policy", json=POLICY)
    draft = "记录数：1 条" + NL + "收入合计：100.00 元"
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit", json={"draft": draft}
    )
    assert audit.status_code == 200, audit.text
    # 证据包离线复算要求账本里有与 audit_id 匹配的确认记录（确认门）。
    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": audit.json()["audit_id"]},
    )
    assert confirm.status_code == 200, confirm.text
    response = client.post(f"/api/sessions/{session_id}/sales-audit/bundle", json={"draft": draft})
    assert response.status_code == 200
    bundle = tmp_path / "policy.zip"
    bundle.write_bytes(response.content)
    with zipfile.ZipFile(bundle) as archive:
        assert "policy.json" in archive.namelist()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(bundle)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    tampered = tmp_path / "policy-tampered.zip"
    source = zipfile.ZipFile(io.BytesIO(response.content))
    with zipfile.ZipFile(tampered, "w") as archive:
        for name in source.namelist():
            raw = source.read(name)
            if name == "policy.json":
                raw = raw.replace(b'"East"', b'"West"')
            archive.writestr(name, raw)
    failed = subprocess.run(
        [sys.executable, str(SCRIPT), str(tampered)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert failed.returncode == 1


def test_policy_get_and_invalid_put(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    assert client.get(f"/api/sessions/{session_id}/audit-policy").json()["policy"] is None
    assert client.put(f"/api/sessions/{session_id}/audit-policy", json=POLICY).status_code == 200
    fetched = client.get(f"/api/sessions/{session_id}/audit-policy").json()
    assert fetched["policy"]["principal_id"] == "u_east"
    assert fetched["sha256"]

    bad = client.put(
        f"/api/sessions/{session_id}/audit-policy",
        json={"principal_id": "u", "claims": {}, "rls": {"column": "r", "claim": "r"}},
    )
    assert bad.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"principal_id": "u", "claims": "x"},
        {"principal_id": "u", "claims": {}, "pii_columns": ["c"] * 21},
        {"principal_id": "u", "claims": {}, "rls": "x"},
        {"principal_id": "u", "claims": {}, "rls": {"column": "r"}},
        {"principal_id": "u", "claims": {f"c{i}": "v" for i in range(21)}},
    ],
)
def test_policy_parse_more_branches(payload):
    with pytest.raises(ValueError):
        parse_policy(payload)


def test_policy_load_errors_and_rls_value_guard(tmp_path):
    from data_agent.audit_policy import load_policy, rls_value

    (tmp_path / "audit_policy.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取访问策略"):
        load_policy(tmp_path)
    with pytest.raises(ValueError, match="缺少 RLS"):
        rls_value({"principal_id": "u", "claims": {}, "rls": {"column": "r", "claim": "r"}})


def test_policy_strings_rejects_non_list():
    """Pydantic 会在 HTTP 层拦类型；直接调解析函数锁定防御分支。"""
    import pytest

    from data_agent.audit_policy import _strings
    with pytest.raises(ValueError, match="字符串数组"):
        _strings("not-a-list", "pii_columns")
    assert _strings(None, "pii_columns") == ()
    assert _strings([" a ", ""], "pii_columns") == ("a",)


def test_switch_demo_principal_reports_metadata_errors(tmp_path, monkeypatch):
    """演示元数据损坏/格式无效/类型未知/缺少 CSV 都要给出明确状态码。"""
    client, session_id = _session(tmp_path, monkeypatch)
    workspace = api.registry.get(session_id).workspace
    url = f"/api/sessions/{session_id}/audit-policy/switch"

    (workspace.root / "demo.json").write_text("{not json", encoding="utf-8")
    assert client.post(url, json={"principal_id": "u_east"}).status_code == 422

    (workspace.root / "demo.json").write_text("[]", encoding="utf-8")
    assert client.post(url, json={"principal_id": "u_east"}).status_code == 422

    import data_agent.routers.audit_policy as policy_router

    monkeypatch.setattr(policy_router, "_demo_kind", lambda root: "unknown")
    response = client.post(url, json={"principal_id": "u_east"})
    assert response.status_code == 404
    assert "未知的演示类型" in response.json()["detail"]

    monkeypatch.setattr(policy_router, "_demo_kind", lambda root: "regional_policy")
    workspace.source_path = None
    response = client.post(url, json={"principal_id": "u_east"})
    assert response.status_code == 422
    assert "原始 CSV" in response.json()["detail"]
