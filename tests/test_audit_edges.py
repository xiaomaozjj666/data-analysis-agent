"""声明式口径、来源血缘、证据包与路由的边界分支。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent.audit_lineage import parse_lineage
from data_agent.audit_schema import (
    approve_schema,
    load_schema_document,
    parse_schema,
    read_metrics,
    save_schema,
)

NL = chr(10)


@pytest.mark.parametrize(
    "payload",
    [
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "filters": [{}] * 9}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "filters": ["x"]}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "filters": [{"op": "eq", "value": 1}]}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "filters": [{"column": "x", "op": "bad", "value": 1}]}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "filters": [{"column": "x", "op": "in", "value": 1}]}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "time_window": "x"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "time_window": {"start": "2026-01-01"}}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "time_window": {"column": "d", "start": "bad"}}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "time_window": {"column": "d"}}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "data_type": "blob"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "data_type": "string"}]},
        {"metrics": [{"name": "A", "aggregation": "ratio", "denominator": "B"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "ratio", "numerator": "B", "denominator": "C", "filters": [{"column": "x", "op": "eq", "value": 1}]}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "numerator": "B"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "null_policy": "ignore"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "rounding": "UP"}]},
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "sign_policy": "positive"}]},
    ],
)
def test_schema_validation_branches(payload):
    with pytest.raises(ValueError):
        parse_schema(payload)


def test_formula_integer_and_catalog_errors(tmp_path):
    integer = parse_schema(
        {"metrics": [{"name": "A", "column": "x", "aggregation": "sum", "data_type": "integer"}]}
    )[0]
    assert "INTEGER" in integer.formula()

    (tmp_path / "audit_schema.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="必须是对象"):
        load_schema_document(tmp_path)

    with pytest.raises(ValueError, match="批准人不能为空"):
        approve_schema(tmp_path, " ")
    (tmp_path / "audit_schema.json").unlink()
    with pytest.raises(ValueError, match="还没有登记"):
        approve_schema(tmp_path, "审计")


def test_history_write_failure_is_tolerated(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    original = _Path.open

    def failing(self, *args, **kwargs):
        if self.name == "audit_schema_history.jsonl":
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "open", failing)
    save_schema(tmp_path, {"metrics": [{"name": "A", "column": "x", "aggregation": "sum"}]})
    assert (tmp_path / "audit_schema.json").is_file()


def test_filter_operators_and_windows(tmp_path):
    path = tmp_path / "ops.csv"
    path.write_text(
        "k,v,label,d" + NL + "a,10,foo bar,2026-01-05" + NL + "b,20,baz,2026-02-05" + NL,
        encoding="utf-8",
    )

    def spec(op, value, column="v"):
        return parse_schema(
            {"metrics": [{"name": "计数", "column": "k", "aggregation": "count", "filters": [{"column": column, "op": op, "value": value}]}]}
        )

    assert read_metrics(path, spec("eq", "10")).metrics["计数"] == "1"
    assert read_metrics(path, spec("ne", "10")).metrics["计数"] == "1"
    assert read_metrics(path, spec("contains", "foo", "label")).metrics["计数"] == "1"
    assert read_metrics(path, spec("in", ["10", "20"])).metrics["计数"] == "2"
    assert read_metrics(path, spec("gt", "10")).metrics["计数"] == "1"
    assert read_metrics(path, spec("ge", "10")).metrics["计数"] == "2"
    assert read_metrics(path, spec("lt", "20")).metrics["计数"] == "1"
    assert read_metrics(path, spec("le", "20")).metrics["计数"] == "2"

    window = parse_schema(
        {"metrics": [{"name": "计数", "column": "k", "aggregation": "count", "time_window": {"column": "d", "start": "2026-01-01"}}]}
    )
    assert read_metrics(path, window).metrics["计数"] == "2"


def test_numeric_and_null_edge_cases(tmp_path):
    path = tmp_path / "edge.csv"
    path.write_text("v" + NL + '"1,234.50"' + NL + NL + NL, encoding="utf-8")
    spec = parse_schema({"metrics": [{"name": "合计", "column": "v", "aggregation": "sum", "unit": "元"}]})
    assert read_metrics(path, spec).metrics["合计"] == "1234.50"

    path.write_text("v" + NL + "1.1234567890" + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="小数位过多"):
        read_metrics(path, spec)

    path.write_text("v,z" + NL + "1,a" + NL + ",b" + NL, encoding="utf-8")
    zero = parse_schema({"metrics": [{"name": "合计", "column": "v", "aggregation": "sum", "null_policy": "zero"}]})
    assert read_metrics(path, zero).metrics["合计"] == "1.00"

    distinct = parse_schema({"metrics": [{"name": "值数", "column": "v", "aggregation": "count_distinct", "null_policy": "fail"}]})
    with pytest.raises(ValueError, match="null_policy=fail"):
        read_metrics(path, distinct)

    empty = tmp_path / "emptycol.csv"
    empty.write_text("v,z" + NL + ",a" + NL, encoding="utf-8")
    ratio = parse_schema(
        {
            "metrics": [
                {"name": "合计", "column": "v", "aggregation": "sum", "null_policy": "zero"},
                {"name": "值数", "column": "v", "aggregation": "count_distinct"},
                {"name": "均值", "aggregation": "ratio", "numerator": "合计", "denominator": "值数"},
            ]
        }
    )
    with pytest.raises(ValueError, match="分母为 0"):
        read_metrics(empty, ratio)


def test_non_ascii_near_duplicates(tmp_path):
    path = tmp_path / "dup.csv"
    path.write_text("id" + NL + "订单​A" + NL + "订单A" + NL, encoding="utf-8")
    spec = parse_schema({"metrics": [{"name": "订单数", "column": "id", "aggregation": "count_distinct"}]})
    result = read_metrics(path, spec)
    assert result.warnings[0]["code"] == "near_duplicate_value"


def test_router_error_paths(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions", files={"file": ("sales.csv", b"order_id,sales,quantity\nA,1,1\n", "text/csv")}
    ).json()["id"]
    workspace = api.registry.get(session_id).workspace

    (workspace.root / "audit_schema.json").write_text("[]", encoding="utf-8")
    assert client.get(f"/api/sessions/{session_id}/audit-schema").status_code == 422
    assert client.put(
        f"/api/sessions/{session_id}/audit-schema",
        json={"metrics": [{"name": "A", "column": "x", "aggregation": "bad"}]},
    ).status_code == 422
    assert client.post(
        f"/api/sessions/{session_id}/audit-schema/approve", json={"approved_by": "x"}
    ).status_code == 422
    (workspace.root / "audit_schema.json").unlink()
    assert client.post(
        f"/api/sessions/{session_id}/audit-schema/approve", json={"approved_by": "x"}
    ).status_code == 422

    (workspace.root / "audit_lineage.json").write_text("[]", encoding="utf-8")
    assert client.get(f"/api/sessions/{session_id}/audit-lineage").status_code == 422
    assert client.put(
        f"/api/sessions/{session_id}/audit-lineage",
        json={"upstream_table": "t", "extracted_at": "bad"},
    ).status_code == 422

    (workspace.root / "audit_schema_history.jsonl").write_text("{broken" + NL, encoding="utf-8")
    history = client.get(f"/api/sessions/{session_id}/audit-schema/history").json()
    assert history["consistent"] is False


def test_bundle_error_paths(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    # 非 CSV 会话：把会话源文件换成 json
    session_id = client.post(
        "/api/sessions",
        files={
            "file": (
                "sales.csv",
                b"order_id,sales,quantity" + NL.encode() + b"A,1,1" + NL.encode(),
                "text/csv",
            )
        },
    ).json()["id"]
    workspace = api.registry.get(session_id).workspace
    notes = workspace.input_dir / "notes.json"
    notes.write_text("{}", encoding="utf-8")
    workspace.source_path = notes
    assert client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle", json={"draft": "订单数：1 笔"}
    ).status_code == 422

    # CSV 缺列 → 核验失败
    bad = client.post(
        "/api/sessions", files={"file": ("bad.csv", b"x,y\n1,2\n", "text/csv")}
    ).json()["id"]
    assert client.post(
        f"/api/sessions/{bad}/sales-audit/bundle", json={"draft": "订单数：1 笔"}
    ).status_code == 422

    # 找不到轨迹
    ok = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"order_id,sales,quantity\nA,1,1\n", "text/csv")},
    ).json()["id"]
    assert client.post(
        f"/api/sessions/{ok}/sales-audit/bundle",
        json={"draft": "订单数：1 笔", "draft_trace_sha256": "a" * 64},
    ).status_code == 422


def test_lineage_parse_edge_branches():
    with pytest.raises(ValueError, match="至少填写一个"):
        parse_lineage({"extracted_at": "2026-11-19T08:00:00+08:00"})
    with pytest.raises(ValueError, match="带时区"):
        parse_lineage({"upstream_table": "t", "extracted_at": "2026-11-19T08:00:00"})
    with pytest.raises(ValueError, match="freshness_sla_hours"):
        parse_lineage({"upstream_table": "t", "extracted_at": "2026-11-19T08:00:00+08:00", "freshness_sla_hours": "x"})
    with pytest.raises(ValueError, match="不超过|最多"):
        parse_lineage({"upstream_table": "x" * 400, "extracted_at": "2026-11-19T08:00:00+08:00"})


def test_filter_operators_non_matching_and_iso_fallback(tmp_path):
    path = tmp_path / "ops2.csv"
    path.write_text(
        "k,v,label,d" + NL + "a,10,foo,2026-01-05" + NL + "b,20,baz,2026-02-05" + NL,
        encoding="utf-8",
    )

    def count(op, value, column="v"):
        return parse_schema(
            {"metrics": [{"name": "c", "column": "k", "aggregation": "count", "filters": [{"column": column, "op": op, "value": value}]}]}
        )

    assert read_metrics(path, count("contains", "zzz", "label")).metrics["c"] == "0"
    assert read_metrics(path, count("in", ["99"])).metrics["c"] == "0"
    assert read_metrics(path, count("gt", "99")).metrics["c"] == "0"
    assert read_metrics(path, count("ge", "99")).metrics["c"] == "0"
    assert read_metrics(path, count("lt", "1")).metrics["c"] == "0"
    assert read_metrics(path, count("le", "1")).metrics["c"] == "0"

    window = parse_schema(
        {"metrics": [{"name": "c", "column": "k", "aggregation": "count", "time_window": {"column": "d", "start": "2026-01-01", "end": "2026-02-01"}}]}
    )
    assert read_metrics(path, window).metrics["c"] == "1"


def test_lineage_missing_time_naive_now_and_describe(tmp_path):
    from datetime import datetime

    from data_agent.audit_lineage import describe, freshness, load_lineage

    with pytest.raises(ValueError, match="extracted_at"):
        parse_lineage({"upstream_table": "t"})
    (tmp_path / "audit_lineage.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取来源血缘"):
        load_lineage(tmp_path)

    lineage = parse_lineage(
        {"upstream_table": "dwd.orders", "extracted_at": "2026-11-19T08:00:00+08:00"}
    )
    state = freshness(lineage, now=datetime(2026, 11, 19, 12, 0, 0))
    assert state["stale"] is False
    assert describe(lineage) == "dwd.orders · 未登记负责人"

def test_lineage_get_without_record(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"order_id,sales,quantity" + NL.encode() + b"A,1,1" + NL.encode(), "text/csv")},
    ).json()["id"]
    payload = client.get(f"/api/sessions/{session_id}/audit-lineage").json()
    assert payload == {"lineage": None, "sha256": None}
