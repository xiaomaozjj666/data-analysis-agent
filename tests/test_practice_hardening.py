"""落地审查项的回归：统计语义、SQL 护栏、指标计算、配方、导出、脱敏。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from data_agent.facts import extract_facts, render_facts_markdown
from data_agent.metrics import (
    MetricDefinition,
    compute_metrics_on_frame,
    evaluate_metric_expression,
    save_workspace_metrics,
    serialize_metric_definitions,
)
from data_agent.privacy import mask_sample_records
from data_agent.recipe import build_recipe, replay_recipe, save_recipe
from data_agent.report_export import build_docx, build_pptx
from data_agent.sql_guard import validate_select
from data_agent.sqlite_source import read_default_table
from data_agent.tools import build_tools
from data_agent.tools._stats import run_statistical_analysis
from data_agent.workspace import DataWorkspace


def _tools(workspace):
    return {item.name: item for item in build_tools(workspace)}


def test_validate_select_rejects_stacked_statements():
    with pytest.raises(ValueError, match="单条"):
        validate_select("SELECT 1; SELECT 2")
    assert validate_select("SELECT ';' AS mark") == "SELECT ';' AS mark"


def test_ttest_ind_compares_two_groups_not_two_columns():
    frame = pd.DataFrame(
        {
            "region": ["east", "east", "east", "west", "west", "west"],
            "sales": [10.0, 12.0, 11.0, 20.0, 22.0, 21.0],
        }
    )
    grouped = run_statistical_analysis(frame, "ttest_ind", group_by="region", target="sales")
    assert grouped["comparison"] == "two_groups"
    assert grouped["p_value"] < 0.05
    series = run_statistical_analysis(frame.assign(other=frame["sales"] + 1), "ttest_ind", columns=["sales", "other"])
    assert series["comparison"] == "two_series"
    assert "不是" in series["note"]


def test_chi_square_warns_when_expected_frequency_low():
    frame = pd.DataFrame({"a": ["x"] * 8 + ["y"] * 2, "b": ["p"] * 9 + ["q"]})
    result = run_statistical_analysis(frame, "chi_square", columns=["a", "b"])
    assert result["expected_frequency_ok"] is False
    assert "warning" in result


def test_correlation_includes_bonferroni():
    frame = pd.DataFrame({"a": [1, 2, 3, 4, 5], "b": [2, 4, 6, 8, 10], "c": [5, 4, 3, 2, 1]})
    result = run_statistical_analysis(frame, "correlation", columns=["a", "b", "c"])
    assert result["pair_count"] == 3
    assert result["bonferroni_alpha"] == pytest.approx(0.05 / 3)


def test_rolling_and_pct_change():
    frame = pd.DataFrame({"sales": [10.0, 20.0, 30.0, 40.0]})
    rolled = run_statistical_analysis(frame, "rolling", columns=["sales"], window=2, aggregation="mean")
    assert rolled["window"] == 2
    changed = run_statistical_analysis(frame, "pct_change", columns=["sales"], periods=1)
    assert changed["periods"] == 1


def test_registered_metrics_are_actually_computed():
    frame = pd.DataFrame({"sales": [10, 20], "returns": [1, 2]})
    series = evaluate_metric_expression(frame, "sales - returns")
    assert list(series) == [9, 18]
    with pytest.raises(ValueError, match="只允许"):
        evaluate_metric_expression(frame, "__import__('os')")
    computed = compute_metrics_on_frame(
        frame,
        [MetricDefinition(name="净额", description="扣退货", expression="sales - returns")],
    )
    assert computed[0]["status"] == "ok"
    assert computed[0]["sum"] == 27


def test_compute_registered_metrics_tool(tmp_path):
    workspace = DataWorkspace(tmp_path / "runs", session_id="metrics")
    workspace.dataframe = pd.DataFrame({"sales": [3, 4], "returns": [1, 1]})
    (workspace.root / "metrics.json").write_text(
        json.dumps(
            {"metrics": [{"name": "净额", "description": "扣退", "expression": "sales - returns"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = json.loads(_tools(workspace)["compute_registered_metrics"].invoke({"adopt": True}))
    assert payload["metrics"][0]["sum"] == 5
    assert "净额" in list(workspace.dataframe.columns)


def test_query_database_on_csv_session(workspace):
    payload = json.loads(_tools(workspace)["query_database"].invoke({"sql": "SELECT COUNT(*) AS n FROM data"}))
    assert payload["row_count"] == 1
    empty = json.loads(_tools(workspace)["query_database"].invoke({"sql": ""}))
    assert empty["tables"][0]["table"] == "data"


def test_profile_masks_phone_and_email(tmp_path):
    workspace = DataWorkspace(tmp_path / "runs", session_id="pii")
    workspace.dataframe = pd.DataFrame(
        {"phone": ["13812345678"], "email": ["a@example.com"], "sales": [1]}
    )
    profile = workspace.profile(sample_rows=1)
    sample = profile["sample"][0]
    assert "13812345678" not in str(sample["phone"])
    assert "a@example.com" not in str(sample["email"])
    assert workspace.dataframe.loc[0, "phone"] == "13812345678"


def test_mask_sample_records_keeps_row_count():
    frame = pd.DataFrame({"x": [1, 2, 3]})
    assert len(mask_sample_records(frame, 2)) == 2


def test_facts_appendix_from_tool_trace():
    facts = extract_facts(
        [{"type": "tool_result", "name": "statistical_analysis", "detail": json.dumps({"p_value": 0.01, "sample_size": 12})}]
    )
    text = render_facts_markdown(facts)
    assert "0.01" in text
    assert "核验附录" in text


def test_recipe_replay_invokes_inspect(tmp_path):
    workspace = DataWorkspace(tmp_path / "runs", session_id="recipe")
    source = tmp_path / "tiny.csv"
    pd.DataFrame({"sales": [1, 2, 3]}).to_csv(source, index=False)
    workspace.load(source, copy_into_workspace=True)
    recipe = build_recipe(
        task="检查",
        plan=[],
        completed_steps=[
            {"id": "inspect", "title": "检查", "status": "ok", "tool_calls": [{"name": "inspect_data", "args": {"sample_rows": 2}}]}
        ],
        fingerprint={"rows": 3, "columns": ["sales"]},
    )
    save_recipe(workspace, recipe)
    replayed = replay_recipe(workspace, recipe, build_tools(workspace))
    assert replayed["calls"][0]["status"] == "ok"


def test_office_export_bytes_are_zip_packages():
    markdown = "## 结论速览\n- 收入上升\n\n## 注意事项与建议\n- 核对口径"
    docx = build_docx("测试报告", markdown)
    pptx = build_pptx("测试报告", markdown)
    assert docx[:2] == b"PK"
    assert pptx[:2] == b"PK"


def test_read_default_table_caps_large_sqlite(tmp_path, monkeypatch):
    import sqlite3

    from data_agent import sqlite_source

    monkeypatch.setattr(sqlite_source, "DEFAULT_LOAD_MAX_ROWS", 3)
    path = tmp_path / "big.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
    connection.executemany("INSERT INTO t(n) VALUES (?)", [(i,) for i in range(10)])
    connection.commit()
    connection.close()
    frame, warnings = read_default_table(path)
    assert len(frame) == 3
    assert any("只载入前" in item for item in warnings)


def test_report_export_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api as api_module
    from data_agent.agent import AnalysisResult
    from data_agent.registry import SessionRecord, SessionRegistry

    workspace = DataWorkspace(tmp_path / "runs", session_id="exp")
    pd.DataFrame({"sales": [1]}).to_csv(tmp_path / "s.csv", index=False)
    workspace.load(tmp_path / "s.csv", copy_into_workspace=True)
    record = SessionRecord(workspace)
    record.last_result = AnalysisResult(
        response="## 结论速览\n- ok",
        trace=[],
        artifacts=[],
        dataset_profile=workspace.profile(),
        plan=[],
        completed_steps=[],
    )
    registry = SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    registry._items["exp"] = record
    monkeypatch.setattr(api_module, "registry", registry)
    client = TestClient(api_module.app)
    monkeypatch.setenv("APP_ACCESS_TOKEN", "")
    docx = client.get("/api/sessions/exp/report.docx")
    pptx = client.get("/api/sessions/exp/report.pptx")
    assert docx.status_code == 200
    assert pptx.status_code == 200
    assert docx.content[:2] == b"PK"
    assert pptx.content[:2] == b"PK"
    missing = client.post("/api/sessions/exp/replay")
    assert missing.status_code == 404


def test_session_metrics_roundtrip(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api as api_module
    from data_agent.registry import SessionRecord, SessionRegistry

    workspace = DataWorkspace(tmp_path / "runs", session_id="met")
    pd.DataFrame({"销售额": [10], "退货金额": [1]}).to_csv(tmp_path / "s.csv", index=False)
    workspace.load(tmp_path / "s.csv", copy_into_workspace=True)
    record = SessionRecord(workspace)
    registry = SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    registry._items["met"] = record
    monkeypatch.setattr(api_module, "registry", registry)
    monkeypatch.delenv("DATA_AGENT_METRICS_PATH", raising=False)
    monkeypatch.setenv("APP_ACCESS_TOKEN", "")
    client = TestClient(api_module.app)
    empty = client.get("/api/sessions/met/metrics")
    assert empty.status_code == 200
    assert empty.json()["metrics"] == []
    saved = client.put(
        "/api/sessions/met/metrics",
        json={
            "metrics": [
                {
                    "name": "净销售额",
                    "description": "销售额减去退货",
                    "expression": "销售额 - 退货金额",
                }
            ]
        },
    )
    assert saved.status_code == 200
    assert saved.json()["metrics"][0]["name"] == "净销售额"
    loaded = save_workspace_metrics(
        workspace.root,
        serialize_metric_definitions(
            [
                MetricDefinition(
                    name="净销售额",
                    description="销售额减去退货",
                    expression="销售额 - 退货金额",
                )
            ]
        ),
    )
    assert loaded[0].name == "净销售额"
    bad = client.put("/api/sessions/met/metrics", json={"metrics": [{"name": "x"}]})
    assert bad.status_code == 422


def test_session_metrics_env_path_is_read_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api as api_module
    from data_agent.registry import SessionRecord, SessionRegistry

    global_metrics = tmp_path / "global-metrics.json"
    global_metrics.write_text(
        '{"metrics": [{"name": "GMV", "description": "成交额"}]}',
        encoding="utf-8",
    )
    workspace = DataWorkspace(tmp_path / "runs", session_id="envm")
    pd.DataFrame({"销售额": [1]}).to_csv(tmp_path / "s.csv", index=False)
    workspace.load(tmp_path / "s.csv", copy_into_workspace=True)
    record = SessionRecord(workspace)
    registry = SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    registry._items["envm"] = record
    monkeypatch.setattr(api_module, "registry", registry)
    monkeypatch.setenv("DATA_AGENT_METRICS_PATH", str(global_metrics))
    monkeypatch.setenv("APP_ACCESS_TOKEN", "")
    client = TestClient(api_module.app)
    loaded = client.get("/api/sessions/envm/metrics")
    assert loaded.status_code == 200
    assert loaded.json()["env_overrides"] is True
    assert loaded.json()["metrics"][0]["name"] == "GMV"
    blocked = client.put("/api/sessions/envm/metrics", json={"metrics": []})
    assert blocked.status_code == 409
