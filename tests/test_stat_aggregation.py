"""大数据统计图（服务端分箱/五数概括/分层抽样）测试。

背景：直方图与箱线图原本把**全部原始数值**塞进交互 HTML 交给浏览器现算，
30 万行时实测 3.2MB / 5.2MB，产物卡缩略图甚至渲染不出来。改为服务端聚合
后载荷降到几十 KB，且视觉结果必须与全量一致（这是本文件的重点：不能为了
体积牺牲统计口径）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data_agent.tools import build_tools
from data_agent.tools.charts import (
    STAT_AGG_THRESHOLD,
    VIOLIN_SAMPLE_ROWS,
    _box_stats,
    _plotly_binned_histogram,
    _plotly_box_from_stats,
)
from data_agent.workspace import DataWorkspace

PLOTLY_BUNDLE_NAME = "plotly.min.js"


def _tool_map(workspace: DataWorkspace) -> dict:
    return {tool.name: tool for tool in build_tools(workspace)}


def _big_workspace(tmp_path, rows: int = 60_000, session: str = "stat_agg") -> DataWorkspace:
    rng = np.random.default_rng(4242)
    categories = np.array(["甲", "乙", "丙"])
    frame = pd.DataFrame({
        "category": categories[rng.integers(0, 3, rows)],
        "value": np.round(rng.gamma(2.0, 40.0, rows), 3),
        "group": np.where(rng.random(rows) < 0.5, "东", "西"),
    })
    source = tmp_path / f"stat_{rows}.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id=session)
    workspace.load(source, copy_into_workspace=True)
    return workspace


def _plotly_values(values):
    if isinstance(values, dict) and "bdata" in values:
        import base64

        return np.frombuffer(base64.b64decode(values["bdata"]), dtype=np.dtype(values["dtype"]))
    return values


# ---------------------------------------------------------------------------
# 直方图
# ---------------------------------------------------------------------------


def test_binned_histogram_matches_numpy_counts(tmp_path):
    """服务端分箱的纵轴必须是真实记录数（不是抽样估计）。"""
    rng = np.random.default_rng(7)
    values = rng.gamma(2.5, 30.0, 80_000)
    frame = pd.DataFrame({"value": values, "category": np.where(rng.random(80_000) < 0.4, "A", "B")})
    result = _plotly_binned_histogram(
        frame, x="value", bins=50, colors=["#111111", "#222222"], color="category"
    )
    assert result is not None
    figure, info = result
    assert info["total"] == 80_000
    assert len(figure.data) == 2
    for trace in figure["data"]:
        assert trace.type == "bar"
        counts = np.asarray(_plotly_values(trace.y), dtype=float)
        assert counts.sum() == pytest.approx(
            int((frame["category"] == trace.name).sum()), rel=0
        )
    # 分组计数之和 == 全部有限值
    assert sum(float(np.asarray(_plotly_values(t.y), dtype=float).sum()) for t in figure.data) == 80_000
    # bin 宽度一致、区间连续（hover 文案用得上）
    widths = {round(float(t.width), 9) for t in figure.data}
    assert len(widths) == 1


def test_binned_histogram_returns_none_for_unusable_input():
    frame = pd.DataFrame({"category": ["A", "B", "C"], "value": [1.0, np.nan, np.nan]})
    assert _plotly_binned_histogram(frame, x="value", bins=10, colors=["#111"]) is None
    assert _plotly_binned_histogram(
        pd.DataFrame({"value": [None, None]}), x="value", bins=10, colors=["#111"]
    ) is None


def test_huge_histogram_uses_binned_path_and_notes_it(tmp_path):
    workspace = _big_workspace(tmp_path)
    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "histogram", "x": "value", "color": "category", "bins": 40}
        )
    )
    assert result["stat_aggregation"]["kind"] == "histogram"
    assert result["stat_aggregation"]["rows_aggregated"] == 60_000
    assert result["stat_aggregation"]["bins"] == 40
    html = Path(result["html"])
    # 载荷必须远小于"原始数值"路径（曾经 3.2MB 级别）
    assert html.stat().st_size < 400_000
    text = html.read_text(encoding="utf-8")
    assert "服务端按全量数据分箱" in text
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    assert {trace["type"] for trace in figure["data"]} == {"bar"}


def test_small_histogram_keeps_raw_plotly_path(tmp_path):
    workspace = _big_workspace(tmp_path, rows=800, session="small_hist")
    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "histogram", "x": "value"}
        )
    )
    assert "stat_aggregation" not in result
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    assert "histogram" in {trace["type"] for trace in figure["data"]}


# ---------------------------------------------------------------------------
# 箱线图
# ---------------------------------------------------------------------------


def test_box_stats_match_plotly_percentile_convention():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
    stats = _box_stats(values)
    assert stats is not None
    assert stats["median"] == pytest.approx(3.5)
    assert stats["q1"] == pytest.approx(2.25)
    assert stats["q3"] == pytest.approx(4.75)
    # 100 超出 1.5×IQR 上界 → 须端点是 5，并计 1 个离群
    assert stats["upperfence"] == pytest.approx(5.0)
    assert stats["outliers"] == 1
    assert stats["n"] == 6
    assert _box_stats(np.array([np.nan])) is None


def test_box_from_stats_places_each_group_at_its_own_category(tmp_path):
    rng = np.random.default_rng(11)
    frame = pd.DataFrame({
        "category": np.repeat(["甲", "乙", "丙"], 2_000),
        "value": np.concatenate([
            rng.normal(10, 1, 2_000), rng.normal(20, 3, 2_000), rng.normal(30, 5, 2_000),
        ]),
    })
    result = _plotly_box_from_stats(frame, x="category", y="value", colors=["#111", "#222", "#333"])
    assert result is not None
    figure, info = result
    assert info["groups"] == 3
    assert info["total"] == 6_000
    positions = [trace.x[0] for trace in figure.data]
    assert positions == ["甲", "乙", "丙"]  # 不给 x 时四个箱体会叠在同一刻度
    medians = [trace.median[0] for trace in figure.data]
    assert medians == pytest.approx([10, 20, 30], abs=0.5)
    for trace in figure["data"]:
        assert trace.boxpoints is False
        assert trace.q1[0] < trace.median[0] < trace.q3[0]


def test_box_from_stats_returns_none_without_numeric_values():
    frame = pd.DataFrame({"category": ["A", "B"], "value": ["x", "y"]})
    assert _plotly_box_from_stats(frame, x="category", y="value", colors=["#111"]) is None


def test_huge_box_uses_precomputed_stats_and_reports_outliers(tmp_path):
    workspace = _big_workspace(tmp_path, session="big_box")
    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "box", "x": "category", "y": "value"}
        )
    )
    assert result["stat_aggregation"]["kind"] == "box"
    assert result["stat_aggregation"]["rows_aggregated"] == 60_000
    html = Path(result["html"])
    assert html.stat().st_size < 400_000
    text = html.read_text(encoding="utf-8")
    assert "五数概括" in text and "未逐点绘制" in text
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    assert {trace["type"] for trace in figure["data"]} == {"box"}
    # 不再携带原始数值数组（这正是 5.2MB 载荷的来源）
    for trace in figure["data"]:
        assert "y" not in trace or trace["y"] in (None, [])


def test_small_box_keeps_raw_plotly_path(tmp_path):
    workspace = _big_workspace(tmp_path, rows=600, session="small_box")
    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "box", "x": "category", "y": "value"}
        )
    )
    assert "stat_aggregation" not in result
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    assert {trace["type"] for trace in figure["data"]} == {"box"}
    assert any(trace.get("y") for trace in figure["data"])


def test_box_with_color_only_groups_by_color(tmp_path):
    rng = np.random.default_rng(13)
    frame = pd.DataFrame({
        "group": np.repeat(["东", "西"], 1_000),
        "value": np.concatenate([rng.normal(0, 1, 1_000), rng.normal(5, 1, 1_000)]),
    })
    result = _plotly_box_from_stats(frame, x=None, y="value", color="group", colors=["#111", "#222"])
    assert result is not None
    figure, _ = result
    assert [trace.x[0] for trace in figure.data] == ["东", "西"]


def test_stat_agg_threshold_is_the_contract():
    # 阈值是"是否走服务端聚合"的唯一开关，测试钉住它避免被误改。
    assert STAT_AGG_THRESHOLD == 50_000
    assert VIOLIN_SAMPLE_ROWS == 20_000
