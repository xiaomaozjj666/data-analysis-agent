"""密度视图聚合层（data_agent/density.py）单元测试。

这一层决定"几十万点怎么变成可读的格子"：分箱、分档、热点、聚集判定、
文案。它不依赖 Plotly/ECharts，因此可以精确断言数值语义——渲染层的
颜色/布局错误（例如色标被静默丢弃）不属于本文件范围。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from data_agent.density import (
    DENSITY_MIN_POINTS,
    MAX_BINS_PER_AXIS,
    DensityGrid,
    band_indexes,
    band_label,
    band_legend,
    bin_shape,
    build_density_view,
    clip_segment,
    colorscale_from_bands,
    density_bands,
    density_note,
    extreme_points,
    format_number,
    format_range,
    format_share,
    hotspot_sentence,
    panels_sentence,
    should_use_density,
)


def _grid(counts: list[list[int]], total: int | None = None) -> DensityGrid:
    matrix = np.asarray(counts, dtype=np.int64)
    ny, nx = matrix.shape
    return DensityGrid(
        x_edges=np.arange(nx + 1, dtype=float),
        y_edges=np.arange(ny + 1, dtype=float),
        counts=matrix,
        total=int(matrix.sum()) if total is None else total,
    )


# ---------------------------------------------------------------------------
# 阈值与网格形状
# ---------------------------------------------------------------------------


def test_should_use_density_threshold():
    assert should_use_density(DENSITY_MIN_POINTS) is True
    assert should_use_density(DENSITY_MIN_POINTS - 1) is False


def test_bin_shape_scales_with_panel_and_clamps():
    # 面板越大格子越多（保持屏幕上接近正方形），但有上下限保护体积。
    wide = bin_shape(1080.0, 560.0)
    narrow = bin_shape(540.0, 560.0)
    assert wide[0] > narrow[0]
    assert wide[1] == narrow[1]
    tiny = bin_shape(10.0, 10.0)
    assert tiny == (16, 12)
    huge = bin_shape(100_000.0, 100_000.0)
    assert huge == (MAX_BINS_PER_AXIS, MAX_BINS_PER_AXIS)


# ---------------------------------------------------------------------------
# 分档（1-2-5 系列）
# ---------------------------------------------------------------------------


def test_density_bands_uses_round_numbers_and_stays_bounded():
    bands = density_bands(35)
    assert bands == [(1, 1), (2, 4), (5, 9), (10, 24), (25, None)]
    assert len(density_bands(4_000_000)) <= 7
    assert density_bands(1) == [(1, None)]
    assert density_bands(0) == [(1, None)]


def test_density_bands_top_bucket_is_open_ended():
    bands = density_bands(9)
    low, high = bands[-1]
    assert high is None
    # 开区间必须罩得住最大值，否则最大值会落不进任何档（渲染成全透明）。
    values = np.array([[1, 9], [0, 3]])
    indexes = band_indexes(values, bands)
    assert not np.isnan(indexes[0, 1])
    assert indexes[0, 1] == len(bands) - 1


def test_band_labels_and_legend():
    labels = [band_label(band) for band in [(1, 1), (2, 4), (25, None)]]
    assert labels == ["1", "2–4", "≥25"]
    legend = band_legend([(1, 1), (2, None)], ["#111111", "#222222"])
    assert legend[1]["label"] == "≥2"
    assert legend[0]["color"] == "#111111"
    # 颜色不够时重复最后一档，不能越界。
    assert band_legend([(1, 1)] * 3, ["#111111"])[2]["color"] == "#111111"


def test_band_indexes_keeps_zero_cells_transparent():
    values = np.array([[0, 1, 5], [12, 0, 99]])
    indexes = band_indexes(values, density_bands(99))
    assert math.isnan(indexes[0, 0])
    assert math.isnan(indexes[1, 1])
    assert indexes[0, 1] == 0
    assert indexes[1, 2] == len(density_bands(99)) - 1


def test_colorscale_from_bands_spans_zero_to_one():
    """plotly.js 会静默丢弃首尾不是正好 0/1 的色标（实测回退成内置彩虹色）。"""
    scale = colorscale_from_bands(["#aaa111", "#bbb222", "#ccc333"])
    assert scale[0] == [0.0, "#aaa111"]
    assert scale[-1] == [1.0, "#ccc333"]
    positions = [item[0] for item in scale]
    assert positions == sorted(positions)
    # 每档是硬边界：相邻两段共享同一个位置（不同颜色）。
    assert scale[1][0] == scale[2][0]
    assert scale[1][1] != scale[2][1]
    assert colorscale_from_bands([]) == []


# ---------------------------------------------------------------------------
# 网格统计
# ---------------------------------------------------------------------------


def test_grid_basic_stats_and_marginals():
    grid = _grid([[1, 2, 0], [3, 0, 6]])
    assert grid.total == 12
    assert grid.max_count == 6
    assert grid.occupied_cells == 4
    assert grid.empty_share == pytest.approx(1 - 4 / 6)
    assert grid.mean_occupied == pytest.approx(3.0)
    assert list(grid.marginal_x()) == [4, 2, 6]
    assert list(grid.marginal_y()) == [3, 9]
    assert list(grid.x_centers) == [0.5, 1.5, 2.5]
    assert list(grid.y_centers) == [0.5, 1.5]
    assert grid.peak_share == pytest.approx(0.5)


def test_grid_half_mass_share_reflects_spread():
    tight = _grid([[50, 0, 0], [0, 0, 0], [0, 0, 0]])
    spread = _grid([[10, 10, 10], [10, 10, 10], [0, 0, 0]])
    assert tight.half_mass_share < spread.half_mass_share
    # 空网格不炸（total=0 / counts 为空）。
    assert _grid([[0, 0], [0, 0]]).half_mass_share == 0.0
    empty = DensityGrid(x_edges=np.array([0.0]), y_edges=np.array([0.0]),
                        counts=np.zeros((0, 0), dtype=np.int64), total=0)
    assert empty.half_mass_share == 0.0
    assert empty.empty_share == 0.0
    assert empty.peak_share == 0.0
    assert empty.max_count == 0


def test_grid_cluster_detection_separates_noise_from_real_clusters():
    # 均匀噪声：峰值只比均值高一点 → 不应报"存在聚集"。
    rng = np.random.default_rng(3)
    noise = _grid(rng.poisson(4.0, size=(40, 40)).tolist())
    assert noise.concentration_ratio < noise.cluster_threshold
    assert noise.has_clusters is False
    # 真聚集：一个格子吃掉大部分记录。
    clustered = _grid([[0] * 40 for _ in range(40)])
    clustered.counts[5, 5] = 3_000
    clustered.counts[5, 6] = 10
    clustered.total = int(clustered.counts.sum())
    assert clustered.has_clusters is True
    assert clustered.noise_peak_ratio >= 1.0


def test_grid_hotspots_min_separation_and_peak():
    counts = [[0] * 6 for _ in range(6)]
    counts[1][1] = 100
    counts[1][2] = 90   # 紧挨着第一个 → 应被最小间隔过滤掉
    counts[5][5] = 50
    grid = _grid(counts)
    spots = grid.hotspots(top=3, min_separation=1)
    assert [spot["count"] for spot in spots] == [100, 50]
    assert spots[0]["share"] == pytest.approx(100 / 240)
    assert grid.peak()["count"] == 100
    assert _grid([[0, 0], [0, 0]]).hotspots() == []
    assert _grid([[0, 0], [0, 0]]).peak() is None


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------


def test_build_density_view_single_panel_and_range_clipping():
    x = np.concatenate([np.linspace(0, 10, 500), [1_000.0]])
    y = np.concatenate([np.linspace(0, 10, 500), [1_000.0]])
    view = build_density_view(x, y, x_range=(0.0, 10.0), y_range=(0.0, 10.0))
    assert view is not None
    assert view.is_faceted is False
    assert view.panels[0].name == ""
    assert view.rows_used == 500
    assert view.dropped_rows == 1  # 极端点落在视口外，不静默计入
    assert view.bin_label == f"{view.bin_shape[0]}×{view.bin_shape[1]}"
    assert view.panels[0].grid.total == 500


def test_build_density_view_drops_nan_rows_and_validates_length():
    x = np.array([1.0, 2.0, np.nan, 4.0])
    y = np.array([1.0, np.nan, 3.0, 4.0])
    view = build_density_view(x, y)
    assert view is not None
    assert view.rows_used == 2
    with pytest.raises(ValueError):
        build_density_view([1.0, 2.0], [1.0])


def test_build_density_view_returns_none_for_degenerate_input():
    assert build_density_view([1.0], [1.0]) is None
    assert build_density_view([np.nan, np.nan], [np.nan, np.nan]) is None
    # 视口把数据全排除掉
    assert build_density_view([5.0, 6.0], [5.0, 6.0], x_range=(0.0, 1.0), y_range=(0.0, 1.0)) is None


def test_build_density_view_handles_constant_columns():
    view = build_density_view([3.0] * 50, [7.0] * 50)
    assert view is not None
    assert view.x_range[1] > view.x_range[0]
    assert view.y_range[1] > view.y_range[0]


def test_build_density_view_facets_by_group_with_share_and_levels():
    x = np.concatenate([np.random.default_rng(1).normal(0, 1, 300),
                        np.random.default_rng(2).normal(50, 1, 100)])
    y = np.concatenate([np.random.default_rng(3).normal(0, 1, 300),
                        np.random.default_rng(4).normal(50, 1, 100)])
    groups = ["A"] * 300 + ["B"] * 100
    view = build_density_view(x, y, groups=groups)
    assert view is not None
    assert [panel.name for panel in view.panels] == ["A", "B"]
    assert view.panels[0].share == pytest.approx(0.75)
    assert view.panels[0].levels == ("A",)
    assert view.is_faceted is True
    # B 只有 100 个点但挤在一个小簇里，按"最密单格占比"它才是最集中的组。
    assert view.busiest_panel().name == "B"
    assert view.spread_panel().name == "A"
    # 合并网格 = 各面板之和（用于全局热点描述）
    assert view.overall.total == view.panels[0].grid.total + view.panels[1].grid.total


def test_build_density_view_single_level_group_is_not_faceted():
    view = build_density_view([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], groups=["A"] * 3)
    assert view is not None
    assert view.is_faceted is False
    assert view.panels[0].levels == ("A",)


def test_build_density_view_merges_excess_panels_into_other():
    rng = np.random.default_rng(11)
    groups = np.repeat([f"G{index}" for index in range(9)], 100)
    x = rng.normal(0, 1, 900)
    y = rng.normal(0, 1, 900)
    view = build_density_view(x, y, groups=groups.tolist(), max_panels=4)
    assert view is not None
    names = [panel.name for panel in view.panels]
    assert len(names) == 4
    assert names[-1].startswith("其他 ")
    # "其他"面板合并了剩余 6 个分组
    assert len(view.panels[-1].levels) == 6


def test_build_density_view_skips_nan_groups():
    view = build_density_view(
        [1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0],
        groups=["A", None, "B", float("nan")],
    )
    assert view is not None
    assert sorted(panel.name for panel in view.panels) == ["A", "B"]


def test_build_density_view_empty_after_group_filter_returns_none():
    # 全部分组值为空 → 没有任何面板可分
    assert build_density_view([1.0, 2.0], [1.0, 2.0], groups=[None, None]) is None


def test_view_band_colors_follow_scheme_and_count():
    rng = np.random.default_rng(5)
    view = build_density_view(rng.normal(0, 1, 5_000), rng.normal(0, 1, 5_000))
    assert view is not None
    light = view.band_colors()
    dark = view.band_colors(dark=True)
    assert len(light) == len(view.bands) == len(dark)
    assert light != dark


def test_view_band_palette_repeats_for_extreme_tails():
    """色板长度固定 7，档数由 density_bands 限制在 7 以内；颜色不足时重复最深色。"""
    from data_agent.density import _fit_palette

    assert _fit_palette(["#1", "#2"], 0) == []
    assert _fit_palette(["#1", "#2"], 1) == ["#1"]
    assert _fit_palette(["#1", "#2"], 4) == ["#1", "#2", "#2", "#2"]
    counts = [[0] * 20 for _ in range(20)]
    counts[3][3] = 5_000_000
    grid = _grid(counts)
    from data_agent.density import DensityPanel, DensityView

    view = DensityView(
        panels=[DensityPanel(name="", grid=grid, share=1.0)],
        total=5_000_000, rows_used=5_000_000,
        x_range=(0.0, 20.0), y_range=(0.0, 20.0), bin_shape=(20, 20),
    )
    assert len(view.bands) == len(view.band_colors()) == 7


# ---------------------------------------------------------------------------
# 极端点召回与线段裁剪
# ---------------------------------------------------------------------------


def test_extreme_points_picks_outermost_records():
    rng = np.random.default_rng(8)
    x = rng.normal(0, 1, 1_000)
    y = rng.normal(0, 1, 1_000)
    x[7], y[7] = 40.0, 40.0
    xs, ys = extreme_points(x, y, x_range=(-50, 50), y_range=(-50, 50), top=5)
    assert len(xs) == 5
    assert 40.0 in xs
    # 视口外的记录不参与（它们本来就不在图上）
    xs2, _ = extreme_points(x, y, x_range=(-3, 3), y_range=(-3, 3), top=5)
    assert 40.0 not in xs2
    # 空输入 / 形状不匹配 → 空结果而不是异常
    assert extreme_points(np.array([]), np.array([]), x_range=(0, 1), y_range=(0, 1))[0].size == 0
    assert extreme_points(np.array([1.0]), np.array([1.0, 2.0]),
                          x_range=(0, 1), y_range=(0, 1))[0].size == 0
    # 常数列（MAD=0）走 std 兜底分支
    xs3, _ = extreme_points(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0]),
                            x_range=(0, 10), y_range=(0, 10), top=2)
    assert xs3.size == 2


def test_clip_segment_handles_inside_outside_and_axis_aligned():
    inside = clip_segment([1.0, 1.0, 2.0, 2.0], x_range=(0, 3), y_range=(0, 3))
    assert inside == [1.0, 1.0, 2.0, 2.0]
    clipped = clip_segment([0.0, 0.0, 10.0, 10.0], x_range=(0, 5), y_range=(0, 5))
    assert clipped == [0.0, 0.0, 5.0, 5.0]
    assert clip_segment([9.0, 9.0, 10.0, 10.0], x_range=(0, 5), y_range=(0, 5)) is None
    # 水平线（dy=0）：p==0 且 q>=0 必须保留，而不是除零
    horizontal = clip_segment([-5.0, 2.0, 20.0, 2.0], x_range=(0, 5), y_range=(0, 5))
    assert horizontal == [0.0, 2.0, 5.0, 2.0]
    # 水平线整体在视口外（q<0 分支）
    assert clip_segment([-5.0, 99.0, 20.0, 99.0], x_range=(0, 5), y_range=(0, 5)) is None
    # 只与左边界相交（t0 更新后仍被 t1 限制）
    assert clip_segment([-1.0, 2.0, 2.0, 2.0], x_range=(0, 5), y_range=(0, 5)) == [0.0, 2.0, 2.0, 2.0]


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


def test_number_and_share_formatting():
    assert format_number(1_234.5) == "1,234" or format_number(1_234.5) == "1,235"
    assert format_number(12.34) == "12.3"
    assert format_number(0.5) == "0.5"
    assert format_number(0.0001) == "1.00e-04"
    assert format_number(float("nan")) == "—"
    assert format_range(1_200.0, 1_400.0) == "1,200 ~ 1,400"
    assert format_share(0.25) == "25%"
    assert format_share(0.0125) == "1.2%"
    assert format_share(0.0002) == "0.02%"


def test_density_note_mentions_bins_counts_and_outside_rows():
    x = np.concatenate([np.linspace(0, 10, 200), [100.0]])
    y = np.concatenate([np.linspace(0, 10, 200), [100.0]])
    single = build_density_view(x, y, x_range=(0.0, 10.0), y_range=(0.0, 10.0))
    assert single is not None
    note = density_note(single)
    assert "密度图" in note and single.bin_label in note
    assert "200 条记录参与绘制" in note
    assert "1 条落在坐标轴范围外未计入" in note

    groups = ["A"] * 100 + ["B"] * 101
    faceted = build_density_view(x, y, groups=groups, x_range=(0.0, 10.0), y_range=(0.0, 10.0))
    assert faceted is not None
    assert "按品类拆成 2 个密度面板" in density_note(faceted, color_label="品类")


def test_hotspot_sentence_reports_clusters_and_calls_out_uniform_noise():
    rng = np.random.default_rng(13)
    clustered = build_density_view(
        np.concatenate([rng.normal(10, 0.4, 3_000), rng.uniform(0, 30, 300)]),
        np.concatenate([rng.normal(10, 0.4, 3_000), rng.uniform(0, 30, 300)]),
    )
    assert clustered is not None
    sentence = hotspot_sentence(clustered, "销售额", "利润")
    assert "最密集的区域在" in sentence
    assert "销售额" in sentence and "利润" in sentence

    uniform = build_density_view(rng.uniform(0, 100, 20_000), rng.uniform(0, 100, 20_000))
    assert uniform is not None
    assert "比较均匀" in hotspot_sentence(uniform, "x", "y")
    assert hotspot_sentence(uniform, "x", "y", panel="不存在的面板") != ""
    assert hotspot_sentence(uniform, "x", "y") != ""


def test_panels_sentence_requires_facets_and_reports_contrast():
    rng = np.random.default_rng(17)
    tight_x = rng.normal(0, 0.3, 2_000)
    tight_y = rng.normal(0, 0.3, 2_000)
    wide_x = rng.uniform(-60, 60, 2_000)
    wide_y = rng.uniform(-60, 60, 2_000)
    view = build_density_view(
        np.concatenate([tight_x, wide_x]), np.concatenate([tight_y, wide_y]),
        groups=["紧"] * 2_000 + ["散"] * 2_000,
    )
    assert view is not None
    sentence = panels_sentence(view, "品类")
    assert "「紧」最集中" in sentence
    assert "「散」最分散" in sentence
    single = build_density_view(tight_x, tight_y)
    assert single is not None
    assert panels_sentence(single, "品类") == ""


def test_panels_sentence_states_similarity_when_no_dominant_group():
    rng = np.random.default_rng(19)
    scattered = build_density_view(
        rng.uniform(0, 100, 4_000), rng.uniform(0, 100, 4_000),
        groups=["A"] * 2_000 + ["B"] * 2_000,
    )
    assert scattered is not None
    assert "都比较分散" in panels_sentence(scattered, "品类")

    # 两组都是同样紧的簇 → 峰值占比接近，给"形态接近"的结论
    tight = np.concatenate([rng.normal(0, 0.5, 1_000), rng.normal(50, 0.5, 1_000)])
    same_shape = build_density_view(
        tight, tight + rng.normal(0, 0.01, 2_000),
        groups=["A"] * 1_000 + ["B"] * 1_000,
    )
    assert same_shape is not None
    assert "分布形态接近" in panels_sentence(same_shape, "品类")


def test_panels_sentence_handles_missing_panels():
    from data_agent.density import DensityView

    view = DensityView(panels=[], total=0, rows_used=0, x_range=(0.0, 1.0),
                       y_range=(0.0, 1.0), bin_shape=(4, 4))
    assert view.overall is None
    assert view.busiest_panel() is None
    assert view.spread_panel() is None
    assert view.has_real_clusters is False
    assert panels_sentence(view, "品类") == ""
    assert hotspot_sentence(view, "x", "y") == ""
