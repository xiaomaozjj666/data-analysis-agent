"""声明式核验口径：非销售表也能按登记指标独立复算。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent.audit_schema import (
    load_schema,
    parse_schema,
    read_metrics,
    save_schema,
    schema_fingerprint,
)
from data_agent.sales_audit import audit_sales_brief

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import verify_audit_ledger  # noqa: E402

CSV = """customer_id,region,revenue
C1,East,100.50
C1,East,99.50
C2,West,50.00
"""

SPECS = {
    "metrics": [
        {"name": "收入合计", "column": "revenue", "aggregation": "sum", "unit": "元"},
        {"name": "客户数", "column": "customer_id", "aggregation": "count_distinct", "unit": "人"},
        {"name": "区域数", "column": "region", "aggregation": "count_distinct", "unit": "个"},
        {"name": "记录数", "column": "customer_id", "aggregation": "count", "unit": "条"},
    ]
}


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "customers.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def test_parse_and_roundtrip(tmp_path):
    specs = parse_schema(SPECS)
    assert [spec.name for spec in specs] == ["收入合计", "客户数", "区域数", "记录数"]
    assert specs[1].is_integer and not specs[0].is_integer
    saved = save_schema(tmp_path, SPECS)
    assert load_schema(tmp_path) == saved
    assert schema_fingerprint(saved) == schema_fingerprint(specs)


@pytest.mark.parametrize(
    "payload",
    [
        {"metrics": [{"name": "A", "column": "x", "aggregation": "median"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum"}, {"name": "A", "column": "y", "aggregation": "sum"}]},
        {"metrics": [{"name": "A", "aggregation": "sum"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "decimals": 9}]},
    ],
)
def test_invalid_schema_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_schema(payload)


def test_read_metrics_for_non_sales_table(source):
    read = read_metrics(source, parse_schema(SPECS))
    assert read.metrics == {"收入合计": "250.00", "客户数": "2", "区域数": "2", "记录数": "3"}
    assert read.evidence["rows"] == 3
    assert read.evidence["columns"] == ["customer_id", "region", "revenue"]


def test_audit_custom_metrics(source, tmp_path):
    specs = parse_schema(SPECS)
    result = audit_sales_brief(
        source,
        "收入合计：250.00 元\n客户数：2 人\n区域数：2 个\n记录数：3 条",
        metric_specs=specs,
    )
    assert result["summary"] == {"verified": 4, "conflict": 0, "unsupported": 0}
    assert result["audit_id"].startswith("aud-")

    conflict = audit_sales_brief(source, "收入合计：250.01 元", metric_specs=specs)
    assert conflict["summary"]["conflict"] == 1

    scoped = audit_sales_brief(source, "华东区收入合计：100.50 元", metric_specs=specs)
    assert scoped["claims"][0]["status"] == "unsupported"
    assert scoped["corrected_brief"] is None


def test_missing_column_is_rejected(source):
    specs = parse_schema({"metrics": [{"name": "利润", "column": "profit", "aggregation": "sum"}]})
    with pytest.raises(ValueError, match="缺少核验口径需要的列"):
        audit_sales_brief(source, "利润：1.00 元", metric_specs=specs)


def test_schema_fingerprint_changes_audit_id(source):
    first = parse_schema(SPECS)
    second = parse_schema({"metrics": [{"name": "收入合计", "column": "revenue", "aggregation": "sum", "unit": "元"}]})
    a = audit_sales_brief(source, "收入合计：250.00 元", metric_specs=first)
    b = audit_sales_brief(source, "收入合计：250.00 元", metric_specs=second)
    assert a["audit_id"] != b["audit_id"]


def test_duplicate_value_warning_attaches_caveat(tmp_path):
    path = tmp_path / "dup.csv"
    path.write_text("customer_id,revenue\nC1,1\nc1,2\n", encoding="utf-8")
    specs = parse_schema(
        {"metrics": [{"name": "客户数", "column": "customer_id", "aggregation": "count_distinct"}]}
    )
    result = audit_sales_brief(path, "客户数：2", metric_specs=specs)
    assert result["warnings"][0]["code"] == "near_duplicate_value"
    assert "caveat" in result["claims"][0]


def test_schema_api_roundtrip_and_audit(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions", files={"file": ("customers.csv", CSV.encode(), "text/csv")}
    )
    session_id = upload.json()["id"]

    saved = client.put(f"/api/sessions/{session_id}/audit-schema", json=SPECS)
    assert saved.status_code == 200
    assert [m["name"] for m in saved.json()["metrics"]] == ["收入合计", "客户数", "区域数", "记录数"]

    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "收入合计：250.00 元\n客户数：2 人"},
    )
    assert audit.status_code == 200
    assert audit.json()["summary"] == {"verified": 2, "conflict": 0, "unsupported": 0}

    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": "收入合计：250.00 元\n客户数：2 人", "audit_id": audit.json()["audit_id"]},
    )
    assert confirm.status_code == 200
    assert "收入合计：250.00 元" in confirm.json()["brief"]

@pytest.mark.parametrize(
    "payload",
    [
        "not-a-list",
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "aliases": "x"}]},
        {"metrics": ["x"]},
        {"metrics": [{"column": "x", "aggregation": "sum"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "unit": "123456789"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "decimals": "x"}]},
        {"metrics": [{"name": f"M{i}", "column": "x", "aggregation": "sum"} for i in range(13)]},
    ],
)
def test_schema_shape_errors(payload):
    with pytest.raises(ValueError):
        parse_schema(payload)


def test_load_schema_broken_file(tmp_path):
    (tmp_path / "audit_schema.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取核验口径"):
        load_schema(tmp_path)

NL = chr(10)
SIMPLE = parse_schema(
    {"metrics": [{"name": "收入合计", "column": "revenue", "aggregation": "sum", "unit": "元"}]}
)


def test_undecodable_and_empty_and_bad_cells(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_bytes(
        b"customer_id,revenue" + bytes([10]) + bytes([0xFF, 0xFE]) + b",1" + bytes([10])
    )
    with pytest.raises(ValueError, match="UTF-8 或 GB18030"):
        read_metrics(bad, SIMPLE)

    empty = tmp_path / "empty.csv"
    empty.write_text("customer_id,revenue" + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="至少需要一条记录"):
        read_metrics(empty, SIMPLE)

    cell = tmp_path / "cell.csv"
    cell.write_text("customer_id,revenue" + NL + "C1,abc" + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="不是有效数值"):
        read_metrics(cell, SIMPLE)


def test_row_limit_and_malformed_rows(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text("customer_id,revenue" + NL + "C1,1" + NL + "C2,2" + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="行上限"):
        read_metrics(path, SIMPLE, max_rows=1)

    path.write_text("customer_id,revenue" + NL + "C1,1,extra" + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="列数"):
        read_metrics(path, SIMPLE)

    path.write_text("customer_id,revenue" + NL + '"C1,1' + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="UTF-8 或 GB18030"):
        read_metrics(path, SIMPLE)


def test_min_max_mean_and_empty_distinct(tmp_path):
    path = tmp_path / "stats.csv"
    path.write_text(
        "k,v,label" + NL + "a,2,x" + NL + "b,4," + NL + "c,9,y" + NL,
        encoding="utf-8",
    )
    specs = parse_schema(
        {
            "metrics": [
                {"name": "最小", "column": "v", "aggregation": "min"},
                {"name": "最大", "column": "v", "aggregation": "max"},
                {"name": "均值", "column": "v", "aggregation": "mean", "decimals": 1},
                {"name": "标签数", "column": "label", "aggregation": "count_distinct"},
            ]
        }
    )
    read = read_metrics(path, specs)
    assert read.metrics == {"最小": "2.00", "最大": "9.00", "均值": "5.0", "标签数": "2"}

    path.write_text("k,v" + NL + "a," + NL + "b," + NL, encoding="utf-8")
    mean = parse_schema({"metrics": [{"name": "均值", "column": "v", "aggregation": "mean"}]})
    with pytest.raises(ValueError, match="没有可计算的非空数值"):
        read_metrics(path, mean)

def test_null_type_sign_and_rounding_policies(tmp_path):
    path = tmp_path / "policy.csv"
    path.write_text("k,v" + NL + "a," + NL + "b,N/A" + NL + "c,1.5" + NL + "d,-2" + NL, encoding="utf-8")

    def spec(**extra):
        base = {"name": "值", "column": "v", "aggregation": "sum", "unit": "元"}
        base.update(extra)
        return parse_schema({"metrics": [base]})

    assert read_metrics(path, spec(null_policy="exclude")).metrics["值"] == "-0.50"
    with pytest.raises(ValueError, match="null_policy=fail"):
        read_metrics(path, spec(null_policy="fail"))
    with pytest.raises(ValueError, match="不能为负数"):
        read_metrics(path, spec(sign_policy="non_negative", null_policy="exclude"))
    with pytest.raises(ValueError, match="必须是整数"):
        read_metrics(path, spec(data_type="integer", null_policy="exclude", sign_policy="allow"))

    path.write_text("k,v" + NL + "a,2.5" + NL, encoding="utf-8")
    assert read_metrics(path, spec(decimals=0, rounding="HALF_UP")).metrics["值"] == "3"
    assert read_metrics(path, spec(decimals=0, rounding="HALF_EVEN")).metrics["值"] == "2"


def test_filters_and_time_window(tmp_path):
    path = tmp_path / "orders.csv"
    path.write_text(
        "order_date,status,region,revenue" + NL
        + "2026-10-02,paid,East,100" + NL
        + "2026-11-15,cancelled,East,50" + NL
        + "2026-12-31,paid,West,30" + NL
        + "2027-01-01,paid,East,999" + NL,
        encoding="utf-8",
    )
    specs = parse_schema(
        {
            "metrics": [
                {
                    "name": "Q4 有效销售额",
                    "column": "revenue",
                    "aggregation": "sum",
                    "unit": "元",
                    "filters": [{"column": "status", "op": "ne", "value": "cancelled"}],
                    "time_window": {
                        "column": "order_date",
                        "start": "2026-10-01",
                        "end": "2026-12-31",
                    },
                },
                {
                    "name": "华东销售额",
                    "column": "revenue",
                    "aggregation": "sum",
                    "unit": "元",
                    "filters": [{"column": "region", "op": "eq", "value": "East"}],
                },
            ]
        }
    )
    read = read_metrics(path, specs)
    assert read.metrics == {"Q4 有效销售额": "130.00", "华东销售额": "1149.00"}
    assert read.evidence["rows"] == 4
    result = audit_sales_brief(path, "Q4 有效销售额：130.00 元", metric_specs=specs)
    assert result["summary"]["verified"] == 1
    blocked = audit_sales_brief(path, "Q3 有效销售额：130.00 元", metric_specs=specs)
    assert blocked["claims"][0]["status"] == "unsupported"


def test_schema_history_is_append_only(tmp_path):
    save_schema(tmp_path, {"metrics": [{"name": "A", "column": "x", "aggregation": "sum"}]})
    save_schema(tmp_path, {"metrics": [{"name": "B", "column": "y", "aggregation": "count"}]})
    lines = (tmp_path / "audit_schema_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["sha256"] != json.loads(lines[1])["sha256"]

def test_ratio_metric(tmp_path):
    path = tmp_path / "ratio.csv"
    path.write_text("customer_id,revenue" + NL + "C1,100" + NL + "C2,150" + NL, encoding="utf-8")
    specs = parse_schema(
        {
            "metrics": [
                {"name": "收入合计", "column": "revenue", "aggregation": "sum", "unit": "元"},
                {"name": "客户数", "column": "customer_id", "aggregation": "count_distinct", "unit": "人"},
                {
                    "name": "客单价",
                    "aggregation": "ratio",
                    "numerator": "收入合计",
                    "denominator": "客户数",
                    "unit": "元",
                },
            ]
        }
    )
    read = read_metrics(path, specs)
    assert read.metrics["客单价"] == "125.00"
    result = audit_sales_brief(path, "客单价：125.00 元", metric_specs=specs)
    assert result["summary"]["verified"] == 1

    zero = parse_schema(
        {
            "metrics": [
                {"name": "收入合计", "column": "revenue", "aggregation": "sum"},
                {"name": "客户数", "column": "missing", "aggregation": "count_distinct"},
                {"name": "客单价", "aggregation": "ratio", "numerator": "收入合计", "denominator": "客户数"},
            ]
        }
    )
    with pytest.raises(ValueError, match="缺少核验口径需要的列"):
        read_metrics(path, zero)

    no_ref = parse_schema(
        {"metrics": [{"name": "比率", "aggregation": "ratio", "numerator": "A", "denominator": "B"}]}
    )
    with pytest.raises(ValueError, match="未在口径中登记"):
        read_metrics(path, no_ref)

def test_schema_history_api_and_offline_fingerprint(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions", files={"file": ("customers.csv", CSV.encode(), "text/csv")}
    )
    session_id = upload.json()["id"]
    client.put(f"/api/sessions/{session_id}/audit-schema", json=SPECS)

    history = client.get(f"/api/sessions/{session_id}/audit-schema/history").json()
    assert history["consistent"] is True
    assert len(history["entries"]) == 1
    assert history["entries"][0]["fingerprint_valid"] is True

    draft = "收入合计：250.00 元" + NL + "客户数：2 人"
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit", json={"draft": draft}
    ).json()
    client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": audit["audit_id"]},
    )

    workspace = api.registry.get(session_id).workspace
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes((workspace.root / "audit_ledger.jsonl").read_bytes())
    schema_file = tmp_path / "audit_schema.json"
    schema_file.write_text(json.dumps(SPECS), encoding="utf-8")
    assert verify_audit_ledger.main([str(ledger), "--schema", str(schema_file)]) == 0

    tampered = json.loads(json.dumps(SPECS))
    tampered["metrics"][0]["unit"] = "美元"
    schema_file.write_text(json.dumps(tampered), encoding="utf-8")
    assert verify_audit_ledger.main([str(ledger), "--schema", str(schema_file)]) == 1

def test_schema_api_returns_advanced_policy_fields(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    csv = "revenue,status,order_date" + NL + "100,paid,2026-10-02" + NL
    upload = client.post(
        "/api/sessions", files={"file": ("orders.csv", csv.encode(), "text/csv")}
    )
    session_id = upload.json()["id"]
    payload = {
        "metrics": [
            {
                "name": "Q4 收入",
                "column": "revenue",
                "aggregation": "sum",
                "unit": "元",
                "data_type": "decimal",
                "null_policy": "fail",
                "rounding": "HALF_EVEN",
                "sign_policy": "non_negative",
                "filters": [{"column": "status", "op": "eq", "value": "paid"}],
                "time_window": {
                    "column": "order_date",
                    "start": "2026-10-01",
                    "end": "2026-12-31",
                },
            }
        ]
    }
    saved = client.put(f"/api/sessions/{session_id}/audit-schema", json=payload).json()
    metric = saved["metrics"][0]
    assert metric["data_type"] == "decimal"
    assert metric["null_policy"] == "fail"
    assert metric["rounding"] == "HALF_EVEN"
    assert metric["sign_policy"] == "non_negative"
    assert metric["filters"] == [{"column": "status", "op": "eq", "value": "paid"}]
    assert metric["time_window"]["start"] == "2026-10-01"
    fetched = client.get(f"/api/sessions/{session_id}/audit-schema").json()
    assert fetched["metrics"] == saved["metrics"]
