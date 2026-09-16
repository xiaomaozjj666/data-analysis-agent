"""补充分支用例：密度视图/统计聚合的边界路径（保持新增代码 100% 覆盖）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from data_agent.density import (
    DensityGrid,
    DensityPanel,
    DensityView,
    build_density_view,
    clip_segment,
    extreme_points,
)
from data_agent.tools.charts import _plotly_box_from_stats
from data_agent.tools.density_plotly import (
    _add_anchors,
    _add_structure,
    density_figure,
    panel_structure,
)


def test_noise_peak_ratio_with_single_cell():
    """只有一格（或平均为 0）时峰均比退化为 1，不能除零。"""
    one = DensityGrid(x_edges=np.array([0.0, 1.0]), y_edges=np.array([0.0, 1.0]),
                      counts=np.array([[5]]), total=5)
    assert one.noise_peak_ratio == 1.0
    # 只有一格时"最密格 / 均值"恒为 1，聚集判定按阈值给 False（无可比性）
    assert one.has_clusters is False
    empty = DensityGrid(x_edges=np.array([0.0, 1.0]), y_edges=np.array([0.0, 1.0]),
                        counts=np.array([[0]]), total=0)
    assert empty.noise_peak_ratio == 1.0


def test_build_density_view_returns_none_without_finite_values():
    nan = np.full(10, np.nan)
    assert build_density_view(nan, nan) is None
    # 给了视口但数据全是 NaN：边界算不出来，同样返回 None
    assert build_density_view(nan, nan, x_range=(0.0, 1.0), y_range=(0.0, 1.0)) is None


def test_extreme_points_returns_empty_when_nothing_inside():
    xs, ys = extreme_points(
        np.array([100.0, 200.0]), np.array([100.0, 200.0]),
        x_range=(0.0, 1.0), y_range=(0.0, 1.0), top=10,
    )
    assert xs.size == 0 and ys.size == 0


def test_clip_segment_rejects_segments_leaving_the_box():
    # 反向线段（从右往左）：右侧边界的 p<0 分支决定取舍
    assert clip_segment([20.0, 2.0, -20.0, 2.0], x_range=(0, 5), y_range=(0, 5)) is not None
    # 完全在右下角之外
    assert clip_segment([6.0, 6.0, 9.0, 9.0], x_range=(0, 5), y_range=(0, 5)) is None
    # 竖直线（dx=0，p==0 分支）且 x 在范围内 → 按 y 裁剪
    assert clip_segment([2.0, -5.0, 2.0, 9.0], x_range=(0, 5), y_range=(0, 5)) == [2.0, 0.0, 2.0, 5.0]
    # 竖直线但 x 在范围外（q<0 分支）
    assert clip_segment([9.0, 0.0, 9.0, 5.0], x_range=(0, 5), y_range=(0, 5)) is None


def test_panel_structure_handles_missing_columns_and_tiny_groups():
    frame = pd.DataFrame({"sales": [1.0, 2.0, 3.0]})
    panel = DensityPanel(name="", grid=DensityGrid(
        x_edges=np.array([0.0, 1.0]), y_edges=np.array([0.0, 1.0]),
        counts=np.array([[3]]), total=3), share=1.0)
    # 列不存在 → 没有结构注记（而不是抛错）
    assert panel_structure(frame, x="sales", y="profit", color=None, panel=panel) is None
    # 有效点少于 3 个 → 同样返回 None
    tiny = pd.DataFrame({"x": [1.0, np.nan, np.nan], "y": [1.0, np.nan, np.nan]})
    assert panel_structure(tiny, x="x", y="y", color=None, panel=panel) is None


def test_density_single_panel_with_single_level_color_uses_that_panel_subset():
    """只有一个分组值时仍是单面板，但最外围记录只取该组（levels 过滤分支）。"""
    rng = np.random.default_rng(5)
    rows = 25_000
    frame = pd.DataFrame({
        "x": rng.normal(0, 1, rows),
        "y": rng.normal(0, 1, rows),
        "tag": ["唯一组"] * rows,
    })
    view = build_density_view(frame["x"], frame["y"], groups=frame["tag"].tolist())
    assert view is not None and view.is_faceted is False
    view.panels[0].levels = ("唯一组",)
    fig = density_figure(frame, x="x", y="y", color="tag", view=view,
                         x_label="x", y_label="y", title="单组密度")
    names = [trace.name for trace in fig.data]
    assert "最外围记录" in names

    # 有效点不足 10 条时不再叠加原始点（避免几个点撑满画面）
    sparse = pd.DataFrame({
        "x": list(rng.normal(0, 1, 25_000)),
        "y": [np.nan] * 25_000,
        "tag": ["唯一组"] * 25_000,
    })
    for index in range(8):
        sparse.loc[index, "y"] = float(index)
    sparse_view = build_density_view(sparse["x"], sparse["y"], groups=sparse["tag"].tolist())
    assert sparse_view is not None
    sparse_fig = density_figure(sparse, x="x", y="y", color="tag", view=sparse_view,
                                x_label="x", y_label="y", title="稀疏密度")
    assert "最外围记录" not in [trace.name for trace in sparse_fig.data]


def test_add_structure_returns_early_without_structure():
    import plotly.graph_objects as go

    fig = go.Figure()
    _add_structure(fig, None, row=2, col=1)
    assert not fig.data and not fig.layout.shapes


def test_add_anchors_skips_when_pair_too_small():
    import plotly.graph_objects as go

    frame = pd.DataFrame({"x": [1.0, 2.0], "y": [1.0, 2.0]})
    panel = DensityPanel(name="", grid=DensityGrid(
        x_edges=np.array([0.0, 1.0, 2.0]), y_edges=np.array([0.0, 1.0, 2.0]),
        counts=np.array([[1, 0], [0, 1]]), total=2), share=1.0)
    view = DensityView(panels=[panel], total=2, rows_used=2, x_range=(0.0, 2.0),
                       y_range=(0.0, 2.0), bin_shape=(2, 2))
    fig = go.Figure()
    _add_anchors(fig, frame, panel=panel, structure=None, view=view, x="x", y="y",
                 color=None, row=1, col=1, faceted=False, x_format=",.0f", y_format=",.0f")
    assert "最外围记录" not in [trace.name for trace in fig.data]


def test_box_from_stats_without_grouping_column():
    """x 与 color 都为空时整体出一口箱子（plotly 的单组语义）。"""
    rng = np.random.default_rng(7)
    frame = pd.DataFrame({"value": rng.normal(10, 2, 500)})
    result = _plotly_box_from_stats(frame, x=None, y="value", colors=["#111111"])
    assert result is not None
    figure, info = result
    assert info["groups"] == 1
    assert len(figure.data) == 1
    assert figure.data[0].name == "value"


def test_box_from_stats_with_two_grouping_columns_combines_labels():
    rng = np.random.default_rng(11)
    frame = pd.DataFrame({
        "region": np.repeat(["东", "西"], 300),
        "channel": np.tile(np.repeat(["线上", "线下"], 150), 2),
        "value": rng.normal(5, 1, 600),
    })
    result = _plotly_box_from_stats(frame, x="region", y="value", color="channel",
                                    colors=["#111111", "#222222"])
    assert result is not None
    figure, info = result
    assert info["groups"] == 4
    assert " / " in figure.data[0].name


def test_stratified_sample_keeps_every_group_represented():
    from data_agent.tools.builder import VIOLIN_SAMPLE_ROWS, _stratified_sample

    rng = np.random.default_rng(13)
    rows = VIOLIN_SAMPLE_ROWS * 4
    frame = pd.DataFrame({
        "group": np.where(rng.random(rows) < 0.98, "大组", "小组"),
        "value": rng.normal(0, 1, rows),
    })
    sampled = _stratified_sample(frame, "group")
    assert len(sampled) <= rows
    counts = sampled["group"].value_counts()
    # 小组占比不到 2%，也必须拿到最低配额（否则小提琴会被压成一条线）
    assert counts["小组"] >= 200
    # 无分组 / 无该列 / 行数不足时按原样或整体抽样返回
    assert len(_stratified_sample(frame.head(10), "group")) == 10
    assert len(_stratified_sample(frame, None)) == VIOLIN_SAMPLE_ROWS
    assert len(_stratified_sample(frame, "不存在")) == VIOLIN_SAMPLE_ROWS


@pytest.mark.parametrize("rows", [0])
def test_extreme_points_empty_input(rows):
    xs, ys = extreme_points(np.array([]), np.array([]), x_range=(0, 1), y_range=(0, 1))
    assert xs.size == 0 and ys.size == 0
