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


def test_mask_cell_handles_empty_and_short_sensitive_values():
    """敏感列的空值与超短值分支：空值原样、两字符全遮、正常值首尾保留。"""
    from data_agent.privacy import mask_cell

    assert mask_cell("customer_phone", "") == ""
    assert mask_cell("customer_phone", "ab") == "**"
    assert mask_cell("customer_phone", "13800001111") == "1*********1"


def test_report_export_embeds_chart_images_and_tolerates_missing(tmp_path):
    """docx/pptx 的图表嵌页分支；缺失/非图片路径必须被跳过而不是报错。"""
    from PIL import Image

    chart = tmp_path / "trend.png"
    Image.new("RGB", (8, 6), "red").save(chart)
    markdown = "## 结论\n销售上行。\n\n## 注意事项\n样本量小。"
    docx_bytes = build_docx("报告", markdown, [chart])
    pptx_bytes = build_pptx("报告", markdown, [chart])
    assert docx_bytes[:2] == b"PK"
    assert pptx_bytes[:2] == b"PK"
    # 缺失的图片路径走 continue 分支，产物仍然合法
    assert build_docx("报告", markdown, [tmp_path / "missing.png"])[:2] == b"PK"
    assert build_pptx("报告", markdown, [tmp_path / "missing.png"])[:2] == b"PK"


def test_report_export_empty_markdown_falls_back():
    """空 Markdown：docx 走"暂无内容"兜底，pptx 不生成结论页。"""
    docx_bytes = build_docx("标题", "")
    pptx_bytes = build_pptx("标题", "")
    assert docx_bytes[:2] == b"PK"
    assert pptx_bytes[:2] == b"PK"


def test_connect_from_frame_normalises_exotic_dtypes():
    """datetime/boolean/Int64 列必须先归一化才能写入 SQLite（dataframe_source）。"""
    from data_agent.dataframe_source import ACTIVE_TABLE, connect_from_frame

    frame = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "flag": pd.array([True, False], dtype="boolean"),
            "nullable_int": pd.array([1, None], dtype="Int64"),
        }
    )
    connection = connect_from_frame(frame)
    try:
        rows = connection.execute(f"SELECT * FROM {ACTIVE_TABLE}").fetchall()
        assert len(rows) == 2
    finally:
        connection.close()


def test_recipe_replay_covers_ok_skip_and_error_paths(tmp_path):
    """配方重放的三条路径：正常执行、未知工具跳过、工具报错不中断。"""
    from data_agent.recipe import data_fingerprint, load_recipe, replay_report_markdown

    workspace = DataWorkspace(tmp_path / "runs", session_id="recipe_replay")
    source = workspace.save_upload("sales.csv", b"region,sales\nEast,100\n")
    workspace.load(source)
    recipe = build_recipe(
        task="配方重放",
        plan=[],
        completed_steps=[
            {
                "id": 1,
                "title": "正常步骤",
                "status": "completed",
                "tool_calls": [{"name": "inspect_data", "args": {}}],
            },
            {
                "id": 2,
                "title": "未知工具",
                "status": "completed",
                "tool_calls": [{"name": "no_such_tool", "args": {}}],
            },
            {
                "id": 3,
                "title": "报错工具",
                "status": "completed",
                "tool_calls": [{"name": "explode", "args": {}}],
            },
        ],
        fingerprint=data_fingerprint(workspace),
    )
    save_recipe(workspace, recipe)
    loaded = load_recipe(workspace)
    assert loaded is not None and loaded["task"] == "配方重放"

    class ExplodingTool:
        name = "explode"

        def invoke(self, args):
            raise ValueError("炸了")

    tools = [ExplodingTool()] + list(build_tools(workspace))
    replay = replay_recipe(workspace, loaded, tools)
    statuses = {call["name"]: call["status"] for call in replay["calls"]}
    assert statuses["inspect_data"] == "ok"
    assert statuses["no_such_tool"] == "skipped"
    assert statuses["explode"] == "error"
    error_call = next(call for call in replay["calls"] if call["name"] == "explode")
    assert "炸了" in error_call["detail"]
    assert replay["skipped"] == 1

    report = replay_report_markdown(replay)
    assert "已按上次分析配方重跑" in report
    assert "数据规模：" in report
    assert "不会重新规划" in report


def test_extract_facts_covers_payload_variants():
    """facts 抽取的字段/means/coefficients/告警与各类型归一化分支。"""
    import math

    trace = [
        {
            "type": "tool_result",
            "name": "statistical_analysis",
            "detail": {
                "p_value": 0.03,
                "sample_size": 120,
                "method": "独立样本 t 检验",
                "means": {"sales": 12.5, "cost": 3},
                "warning": "样本量偏小",
            },
        },
        {
            "type": "tool_result",
            "name": "regression",
            "detail": {"coefficients": {"ads": 0.8, "season": 0.2, "noise": 0.0}},
        },
        {
            "type": "tool_result",
            "name": "clean_data",
            "detail": json.dumps({"rows": 99, "truncated": False}),
        },
        {
            "type": "tool_result",
            "name": "numbers",
            "detail": {
                "statistic": "1,234",
                "mae": "12.5",
                "eta_squared": "inf",
                "mae": "12.5",
                "eta_squared": "inf",
                "mean_difference": float("nan"),
                "effect_size": None,
                "r2": True,
                "adjusted_r2": math.inf,
                "rmse": "not-a-number",
            },
        },
        {"type": "tool_result", "name": "empty_detail", "detail": ""},
        {"type": "tool_result", "name": "garbage", "detail": "不是 JSON 也不是字面量"},
        {"type": "not_tool_result", "name": "skipped_by_type", "detail": {"p_value": 1}},
    ]
    completed = [{"title": "清洗与检验", "status": "completed"}]
    facts = extract_facts(trace, completed)
    by_field: dict[str, list[str]] = {}
    for item in facts:
        by_field.setdefault(item["field"], []).append(item["value"])

    assert by_field["p_value"] == ["0.03"]
    assert by_field["sample_size"] == ["120"]
    assert by_field["method"] == ["独立样本 t 检验"]
    assert by_field["statistic"] == ["1234"]
    assert by_field["mean:sales"] == ["12.5"]
    assert by_field["coef:ads"] == ["0.8"]
    assert by_field["warning"] == ["样本量偏小"]
    assert by_field["status"] == ["completed"]
    assert by_field["rows"] == ["99"]
    # NaN/None 的 rendered 为空或 "nan" 被过滤；bool/inf/不可解析字符串按原样渲染
    assert "mean_difference" not in by_field
    assert "effect_size" not in by_field
    assert by_field["r2"] == ["True"]
    assert by_field["adjusted_r2"] == ["inf"]
    assert by_field["rmse"] == ["not-a-number"]
    assert by_field["mae"] == ["12.5"]
    assert by_field["eta_squared"] == ["inf"]
    assert by_field["mae"] == ["12.5"]
    assert by_field["eta_squared"] == ["inf"]
    # 空字符串与不可解析的 detail 不产生事实
    assert not any(item["source"] in {"empty_detail", "garbage"} for item in facts)
    # 同源同字段同值去重：重复的 p_value 只出现一次
    duplicate = extract_facts(
        [
            {"type": "tool_result", "name": "statistical_analysis", "detail": {"p_value": 0.03}},
            {"type": "tool_result", "name": "statistical_analysis", "detail": {"p_value": 0.03}},
        ],
        None,
    )
    assert len([item for item in duplicate if item["field"] == "p_value"]) == 1


def test_validate_select_semicolon_guard_and_quoted_semicolons():
    """多语句拒绝；字符串字面量内的分号（含反斜杠转义）不误伤。"""
    with pytest.raises(ValueError, match="单条"):
        validate_select("SELECT 1; SELECT 2")
    with pytest.raises(ValueError, match="单条"):
        validate_select("SELECT 'a\\'b'; DROP TABLE t")
    assert validate_select("SELECT ';' AS s") == "SELECT ';' AS s"
    assert validate_select('SELECT "a;b" AS x') == 'SELECT "a;b" AS x'
    assert validate_select("SELECT 'a\\'b' AS x") == "SELECT 'a\\'b' AS x"


def test_to_json_safe_renders_custom_driver_types():
    """datetime/Decimal 等驱动自定义类型统一转字符串，避免 json_text 抛错。"""
    from datetime import datetime
    from decimal import Decimal

    from data_agent.sql_guard import to_json_safe

    assert to_json_safe(datetime(2024, 1, 1, 12, 0)) == "2024-01-01 12:00:00"
    assert to_json_safe(Decimal("1.5")) == "1.5"
    assert to_json_safe(b"\x00\x01") == "<blob:2 bytes>"


def test_validate_select_semicolon_guard_and_quoted_semicolons():
    """多语句拒绝；字符串字面量内的分号（含反斜杠转义）不误伤。"""
    with pytest.raises(ValueError, match="单条"):
        validate_select("SELECT 1; SELECT 2")
    with pytest.raises(ValueError, match="单条"):
        validate_select("SELECT 'a\\\'b'; DROP TABLE t")
    assert validate_select("SELECT ';' AS s") == "SELECT ';' AS s"
    assert validate_select('SELECT "a;b" AS x') == 'SELECT "a;b" AS x'
    assert validate_select("SELECT 'a\\\'b' AS x") == "SELECT 'a\\\'b' AS x"


def test_to_json_safe_renders_custom_driver_types():
    """datetime/Decimal 等驱动自定义类型统一转字符串，避免 json_text 抛错。"""
    from datetime import datetime
    from decimal import Decimal

    from data_agent.sql_guard import to_json_safe

    assert to_json_safe(datetime(2024, 1, 1, 12, 0)) == "2024-01-01 12:00:00"
    assert to_json_safe(Decimal("1.5")) == "1.5"
    assert to_json_safe(b"\x00\x01") == "<blob:2 bytes>"
