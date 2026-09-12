"""指标语义层测试：定义解析、文件加载、选择与渲染。

重点在两处约定：定义文件缺失时特性完全静默（既有行为不变），以及定义文件
写坏时必须报错而不是跳过——静默忽略用户登记的指标口径会让分析用错定义却
看起来一切正常。
"""

from __future__ import annotations

import json

import pytest

from data_agent.metrics import (
    METRICS_FILENAME,
    METRICS_PATH_ENV,
    MetricDefinition,
    load_metric_definitions,
    load_metrics_for_workspace,
    parse_metric_definitions,
    render_metric_context,
    select_metrics,
)


def _definition(name: str = "净销售额", **overrides) -> MetricDefinition:
    payload = {
        "name": name,
        "description": "扣除退货后的销售额",
        "expression": "销售额 - 退货金额",
        "aliases": ("净收入",),
        "source_columns": ("sales",),
    }
    payload.update(overrides)
    return MetricDefinition(**payload)


# ---------------------------------------------------------------------------
# 定义解析
# ---------------------------------------------------------------------------


def test_parse_accepts_wrapper_object_and_bare_list():
    entry = {"name": "净销售额", "description": "扣除退货后的销售额"}
    from_wrapper = parse_metric_definitions({"metrics": [entry]})
    from_list = parse_metric_definitions([entry])
    assert from_wrapper == from_list
    assert from_wrapper[0].name == "净销售额"
    assert from_wrapper[0].aliases == ()


def test_parse_rejects_missing_metrics_field():
    with pytest.raises(ValueError, match="缺少顶层 metrics"):
        parse_metric_definitions({"items": []})


def test_parse_rejects_non_list_metrics():
    with pytest.raises(ValueError, match="metrics 必须是数组"):
        parse_metric_definitions({"metrics": {"name": "x"}})


def test_parse_rejects_non_object_entry_with_index():
    with pytest.raises(ValueError, match="第 1 项必须是对象"):
        parse_metric_definitions(["净销售额"])


def test_parse_rejects_entry_without_name():
    with pytest.raises(ValueError, match="缺少 name"):
        parse_metric_definitions([{"description": "没有名称"}])


def test_parse_rejects_entry_without_description():
    """没有口径说明的定义无法使用，必须显式拒绝。"""
    with pytest.raises(ValueError, match="缺少 description"):
        parse_metric_definitions([{"name": "净销售额"}])


def test_parse_rejects_non_list_aliases():
    with pytest.raises(ValueError, match="aliases 必须是字符串数组"):
        parse_metric_definitions([{"name": "n", "description": "d", "aliases": "净收入"}])


def test_parse_drops_blank_aliases_and_columns():
    parsed = parse_metric_definitions(
        [{"name": "n", "description": "d", "aliases": ["a", "  "], "source_columns": ["", "b"]}]
    )
    assert parsed[0].aliases == ("a",)
    assert parsed[0].source_columns == ("b",)


# ---------------------------------------------------------------------------
# 文件加载
# ---------------------------------------------------------------------------


def test_load_definitions_rejects_invalid_json(tmp_path):
    path = tmp_path / METRICS_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        load_metric_definitions(path)


def test_load_definitions_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_metric_definitions(tmp_path / "nope.json")


def test_load_for_workspace_returns_empty_when_absent(tmp_path):
    assert load_metrics_for_workspace(tmp_path) == []


def test_load_for_workspace_reads_workspace_file(tmp_path):
    (tmp_path / METRICS_FILENAME).write_text(
        json.dumps({"metrics": [{"name": "净销售额", "description": "口径"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    loaded = load_metrics_for_workspace(tmp_path)
    assert [item.name for item in loaded] == ["净销售额"]


def test_load_for_workspace_prefers_env_path(tmp_path, monkeypatch):
    """环境变量指定的全局定义优先于工作区内的文件。"""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / METRICS_FILENAME).write_text(
        json.dumps({"metrics": [{"name": "全局指标", "description": "来自全局配置"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace_dir = tmp_path / "session"
    workspace_dir.mkdir()
    (workspace_dir / METRICS_FILENAME).write_text(
        json.dumps({"metrics": [{"name": "会话指标", "description": "来自会话"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(METRICS_PATH_ENV, str(global_dir / METRICS_FILENAME))
    assert [item.name for item in load_metrics_for_workspace(workspace_dir)] == ["全局指标"]


def test_load_for_workspace_falls_back_when_env_path_missing(tmp_path, monkeypatch):
    """环境变量指向的文件不存在时，退回工作区文件而不是报错。"""
    (tmp_path / METRICS_FILENAME).write_text(
        json.dumps({"metrics": [{"name": "会话指标", "description": "来自会话"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(METRICS_PATH_ENV, str(tmp_path / "gone.json"))
    assert [item.name for item in load_metrics_for_workspace(tmp_path)] == ["会话指标"]


def test_load_for_workspace_reports_file_path_on_invalid_content(tmp_path):
    (tmp_path / METRICS_FILENAME).write_text('{"metrics": [{}]}', encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_metrics_for_workspace(tmp_path)
    assert str(tmp_path / METRICS_FILENAME) in str(exc.value)
    assert "缺少 name" in str(exc.value)


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------


def test_select_prefers_definitions_named_in_the_query():
    metrics = [_definition("净销售额"), _definition("毛利率"), _definition("客单价")]
    selected = select_metrics("看一下毛利率的变化", metrics, limit=2)
    assert selected[0].name == "毛利率"


def test_select_matches_aliases():
    metrics = [_definition("净销售额"), _definition("毛利率")]
    selected = select_metrics("净收入怎么波动", metrics, limit=1)
    assert selected[0].name == "净销售额"


def test_select_keeps_file_order_when_nothing_matches():
    """没有命中时不能返回空——登记的口径本身就是本次分析的上下文。"""
    metrics = [_definition("净销售额"), _definition("毛利率")]
    assert [item.name for item in select_metrics("分析这份数据", metrics, limit=5)] == [
        "净销售额",
        "毛利率",
    ]


def test_select_respects_limit_and_handles_edge_inputs():
    metrics = [_definition(f"指标{i}") for i in range(10)]
    assert len(select_metrics("分析", metrics, limit=3)) == 3
    assert select_metrics("分析", [], limit=3) == []
    assert select_metrics("分析", metrics, limit=0) == []


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_returns_empty_string_without_definitions():
    assert render_metric_context("分析销售数据", []) == ""


def test_render_includes_name_alias_expression_and_columns():
    text = render_metric_context("分析净销售额", [_definition()])
    assert "净销售额" in text
    assert "净收入" in text
    assert "扣除退货后的销售额" in text
    assert "销售额 - 退货金额" in text
    assert "sales" in text
    assert "不得自行重新定义" in text


def test_render_notes_the_truncated_remainder():
    metrics = [_definition(f"指标{i}") for i in range(4)]
    text = render_metric_context("分析", metrics, limit=2)
    assert "另有 2 条已登记指标未在此列出" in text


def test_render_truncates_overlong_fields():
    long_text = "口径" * 400
    text = render_metric_context("分析", [_definition(description=long_text)])
    assert "…" in text
    assert len(text) < len(long_text)
