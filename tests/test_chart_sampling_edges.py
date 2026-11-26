"""chart_sampling 的退化输入分支回归。

图表的降采样发生在用户数据上：时间字符串、缺失标记、点对/一维混用、
多层嵌套数组都可能出现。这些分支一旦抛异常会直接把出图变成 500，因此
把纯函数的退化路径集中测一遍（不做真实渲染，速度快）。
"""

from __future__ import annotations

from data_agent.chart_sampling import (
    _category_envelope,
    _echarts_curve,
    _resample_trace_arrays,
    _series_y_values,
    sample_echarts_option_for_embed,
)


def test_echarts_curve_rejects_unusable_inputs() -> None:
    assert _echarts_curve([], 0) is None
    # 非数值（时间字符串 / 缺失标记）
    assert _echarts_curve(["2026-01-01", "-"], 2) is None
    # 点对数量与 n 不符
    assert _echarts_curve([[0, 1]], 2) is None
    # 不规则嵌套无法转成数值矩阵
    assert _echarts_curve([[0, 1], [1]], 2) is None
    # 一维长度与 n 不符
    assert _echarts_curve([1, 2, 3], 2) is None
    # 三维数组
    assert _echarts_curve([[[1, 2]]], 1) is None
    # x 含 NaN
    assert _echarts_curve([[0, 1], [float("nan"), 2]], 2) is None
    # x 不递增（乱序会让"相邻桶"假设失效）
    assert _echarts_curve([[1, 1], [0, 2]], 2) is None


def test_echarts_curve_accepts_points_and_plain_values() -> None:
    xs, ys = _echarts_curve([1, 2], 2)
    assert list(xs) == [0.0, 1.0]
    assert list(ys) == [1.0, 2.0]

    xs, ys = _echarts_curve([[0, 3], [1, 4]], 2)
    assert list(xs) == [0.0, 1.0]
    assert list(ys) == [3.0, 4.0]


def test_series_y_values_rejects_invalid_shapes() -> None:
    assert _series_y_values({"data": None}, 1) is None
    assert _series_y_values({"data": [1]}, 0) is None
    assert _series_y_values({"data": [1, 2]}, 3) is None
    assert _series_y_values({"data": ["a"]}, 1) is None
    assert _series_y_values({"data": [[0]]}, 1) is None
    assert _series_y_values({"data": [[[0, 1]]]}, 1) is None


def test_series_y_values_reads_second_column_of_pairs() -> None:
    assert _series_y_values({"data": [[0, 1], [1, 2]]}, 2).tolist() == [1.0, 2.0]
    assert _series_y_values({"data": [3, 4]}, 2).tolist() == [3.0, 4.0]


def test_category_envelope_returns_none_for_empty_series() -> None:
    assert _category_envelope([], 2) is None
    envelope = _category_envelope([{"data": [1, -1]}, {"data": [0, 2]}], 2)
    assert envelope.tolist() == [1.0, 2.0]


def test_embed_sampling_skips_non_list_series() -> None:
    sampled, before, after = sample_echarts_option_for_embed({"series": "bad"}, max_points=10)
    assert sampled == {"series": "bad"}
    assert before == 0 and after == 0


def test_embed_sampling_skips_malformed_series_and_axes() -> None:
    option = {
        "series": [
            "not-a-dict",
            {"type": "line"},
            {"type": "scatter", "data": "not-a-list"},
            {"type": "line", "data": [1, 2], "xAxisIndex": "bad"},
        ]
    }
    sampled, before, after = sample_echarts_option_for_embed(option, max_points=100)
    assert before == 2 and after == 2

    out_of_range = {
        "xAxis": [{"type": "value"}],
        "series": [{"type": "line", "data": [1, 2], "xAxisIndex": 5}],
    }
    sample_echarts_option_for_embed(out_of_range, max_points=100)


def test_embed_sampling_keeps_small_value_axis_series() -> None:
    option = {
        "xAxis": [{"type": "value"}],
        "series": [
            {"type": "line", "data": [1]},
            {"type": "line", "data": [2, 3]},
        ],
    }
    sampled, before, after = sample_echarts_option_for_embed(option, max_points=2)
    assert before == 3 and after == 3
    assert sampled["series"][1]["data"] == [2, 3]


def test_embed_sampling_falls_back_to_step_when_x_not_numeric() -> None:
    option = {
        "xAxis": [{"type": "value"}],
        "series": [{"type": "line", "data": [[f"t{index}", 1] for index in range(2001)]}],
    }
    sampled, before, after = sample_echarts_option_for_embed(option, max_points=10)
    assert before == 2001
    # per_cap=2000，步长 ceil(2001/2000)=2
    assert after == 1001
    assert len(sampled["series"][0]["data"]) == 1001


def test_resample_trace_arrays_keeps_fields_with_other_lengths() -> None:
    trace = {"x": [1, 2, 3], "marker": {"color": ["only-one"]}}
    _resample_trace_arrays(trace, 3, step=2)
    assert trace["x"] == [1, 3]
    # 长度与 x 不一致的嵌套数组原样保留
    assert trace["marker"]["color"] == ["only-one"]
