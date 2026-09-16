"""折线/面积图的 LTTB（保峰）降采样测试。

背景：超过 ``_EMBED_MAX_POINTS``（5 万点）的折线原本一律用等距步进
``data[::k]`` 抽样，短促尖峰只要落在两个保留点之间就整段消失——30 万行的
稀疏脉冲序列在预览里渲染成一条平滑带，峰值直接读不出来。折线/面积改用
LTTB（Largest-Triangle-Three-Buckets）后每个桶必留一个点，峰谷得以保留。

本文件锁住四件事，顺序与风险一致：
1. LTTB 自身的性质（首尾保留、点数精确、下标严格递增）；
2. **回归本体**：全局极值恰好落在等距采样格之间时，LTTB 保住、等距丢掉；
3. 两条引擎接入路径：Plotly（数值 x 折线 → LTTB；纯 markers/类目轴/乱序 x
   → 维持原等距行为）与 ECharts（数值轴折线/面积 → LTTB；类目轴 → 仍等距
   且仍与 xAxis.data 对齐）；
4. 退化输入不抛异常、阈值以下字节不变、30 万点性能有预算。
"""

from __future__ import annotations

import copy
import json
import math
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from data_agent import chart_sampling
from data_agent.chart_sampling import (
    _EMBED_MAX_POINTS,
    _lttb_indices,
    sample_echarts_option_for_embed,
    sample_plotly_figure_for_embed,
)
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

#: 单 trace 的嵌入预算（_EMBED_MAX_POINTS // 1），下限 2000。
_ONE_TRACE_CAP = max(2000, _EMBED_MAX_POINTS)


def _spike_series(n: int, spike_at: int, peak: float = 500.0) -> list[float]:
    """全局极值（尖峰）落在 ``spike_at`` 的平基线序列。"""
    values = [0.0] * n
    values[spike_at] = peak
    return values


def _plotly_line_figure(
    n: int,
    spike_at: int,
    *,
    mode: str = "lines",
    trace_type: str = "scattergl",
    x: list | None = None,
) -> dict:
    """模拟 ``json.loads(fig.to_json())``：数值 x 的 scattergl 折线 + 文本 + 颜色。"""
    return {
        "data": [
            {
                "type": trace_type,
                "mode": mode,
                "name": "信号",
                "x": list(range(n)) if x is None else x,
                "y": _spike_series(n, spike_at),
                "text": [f"t{i}" for i in range(n)],
                "marker": {"color": ["#111111"] * n},
            }
        ],
        "layout": {"title": {"text": "6 万行折线"}, "yaxis": {"title": {"text": "v"}}},
    }


def _lttb_spy(calls: list[int]):
    """包一层 ``_lttb_indices``：记录 threshold 调用，行为不变。"""
    real = chart_sampling._lttb_indices

    def _wrapped(x, y, threshold):
        calls.append(threshold)
        return real(x, y, threshold)

    return _wrapped


# ---------------------------------------------------------------------------
# 1. LTTB 自身的性质
# ---------------------------------------------------------------------------


def test_lttb_keeps_ends_count_and_order():
    """首尾恒定保留、点数恰为 threshold、下标严格递增。"""
    rng = np.random.default_rng(20260915)
    n = 5_000
    x = np.arange(n, dtype=float)
    y = rng.normal(0, 1, n)
    indices = _lttb_indices(x, y, 400)
    assert len(indices) == 400
    assert indices[0] == 0
    assert indices[-1] == n - 1
    assert all(b > a for a, b in zip(indices, indices[1:], strict=False)), "下标必须严格递增"
    assert max(indices) < n


def test_lttb_matches_reference_bucket_choice():
    """与参考实现同口径：每桶取"面积最大"的点，而不是桶内第一个点。

    手工算一个小例子：3 个候选点（下标 1..3）、threshold=4 时 every=1.5，
    桶 0 = {1, 2}、桶 1 = {3}；桶 0 里点 2 与首点/次桶均值构成的三角形
    面积显然更大（点 1 与首点几乎重合）。
    """
    x = [0.0, 0.001, 1.0, 2.0, 3.0]
    y = [0.0, 0.0, 5.0, 0.0, 0.0]
    assert _lttb_indices(x, y, 4) == [0, 2, 3, 4]


# ---------------------------------------------------------------------------
# 2. 回归本体：尖峰落在等距采样格之间
# ---------------------------------------------------------------------------


def test_lttb_keeps_spike_that_equidistant_stepping_drops():
    """6 万点里下标 1 的尖峰：等距 step=2 必丢，LTTB 必保。

    这正是被修掉的问题——``data[::2]`` 只保留下标为偶数的点，紧挨首点的
    尖峰（下标 1）连同它的峰值一起消失，图上只剩一条平线。
    """
    n = 60_000
    y = _spike_series(n, spike_at=1)
    step = math.ceil(n / _ONE_TRACE_CAP)
    assert step == 2
    equidistant = y[::step]
    assert max(equidistant) == 0.0, "前提：等距步进确实丢掉了这个尖峰"
    sampled = [y[i] for i in _lttb_indices(np.arange(n, dtype=float), y, _ONE_TRACE_CAP)]
    assert max(sampled) == 500.0, "LTTB 必须保住尖峰"


def test_lttb_keeps_spike_in_long_flat_series():
    """长平线中段一个孤立尖峰：LTTB 仍然把它留下。"""
    n = 120_000
    spike_at = 77_777
    y = _spike_series(n, spike_at=spike_at, peak=99.0)
    indices = _lttb_indices(np.arange(n, dtype=float), y, 4_000)
    assert spike_at in indices
    assert len(indices) == 4_000


def test_lttb_preserves_visual_envelope_of_bursty_signal():
    """脉冲序列：LTTB 抽出的极值包络与全量一致（等距则系统性偏低）。"""
    n = 90_000
    rng = np.random.default_rng(7)
    y = rng.normal(0, 1, n)
    burst_at = [3, 101, 5_000, 44_444, 89_998]
    for index in burst_at:
        y[index] = 40.0 + index % 7
    full_peak = float(y.max())
    step = math.ceil(n / 3_000)
    assert float(y[::step].max()) < full_peak, "前提：等距抽样丢失了至少一处尖峰"
    sampled = y[_lttb_indices(np.arange(n, dtype=float), y, 3_000)]
    assert float(sampled.max()) == full_peak


# ---------------------------------------------------------------------------
# 3a. Plotly 接入
# ---------------------------------------------------------------------------


def test_plotly_embed_line_uses_lttb_and_keeps_spike():
    """6 万行数值 x 的 scattergl 折线：走 LTTB、预算内、尖峰保住、输入不变。"""
    figure = _plotly_line_figure(60_000, spike_at=1)
    snapshot = copy.deepcopy(figure)
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_plotly_figure_for_embed(figure)

    assert calls == [_ONE_TRACE_CAP], "折线必须走 LTTB，且用单 trace 预算"
    assert before == 60_000
    assert after == 50_000 <= _EMBED_MAX_POINTS
    trace = sampled["data"][0]
    assert len(trace["x"]) == len(trace["y"]) == after
    assert max(trace["y"]) == 500.0, "尖峰在嵌入数据里必须还在"
    assert trace["x"] == sorted(trace["x"]), "LTTB 不改变 x 顺序"
    # 同一套下标落到所有按点数组：颜色/悬浮文本与坐标不错位
    peak_pos = trace["y"].index(500.0)
    assert trace["x"][peak_pos] == 1
    assert trace["text"][peak_pos] == "t1"
    assert trace["marker"]["color"][peak_pos] == "#111111"
    # 等距步进会丢掉尖峰（对照，说明这条断言不是无意义的）
    assert 500.0 not in figure["data"][0]["y"][::2]
    # 输入不被修改（解码会重建全部 dict/list，抽样只动副本）
    assert figure == snapshot
    assert sampled["layout"] == snapshot["layout"]


def test_plotly_embed_marker_scatter_stays_equidistant():
    """纯 markers 散点保持原行为：等距步进，绝不换点（点语义不变）。"""
    figure = _plotly_line_figure(60_000, spike_at=1, mode="markers")
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_plotly_figure_for_embed(figure)
    assert calls == [], "markers 散点不得走 LTTB"
    assert before == 60_000
    assert after == 30_000
    assert sampled["data"][0]["x"] == list(range(0, 60_000, 2))


def test_plotly_embed_without_mode_defaults_to_lines():
    """未声明 mode：plotly.js 默认 lines+markers（画线），因此按折线走 LTTB。"""
    figure = _plotly_line_figure(60_000, spike_at=1)
    figure["data"][0].pop("mode")
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, _, after = sample_plotly_figure_for_embed(figure)
    assert calls == [_ONE_TRACE_CAP]
    assert after == 50_000
    assert max(sampled["data"][0]["y"]) == 500.0


@pytest.mark.parametrize(
    "x_factory",
    [
        pytest.param(lambda n: [f"c{i}" for i in range(n)], id="categorical"),
        pytest.param(lambda n: [f"2026-01-01T00:00:{i % 60:02d}" for i in range(n)], id="datetime-str"),
        pytest.param(lambda n: list(range(n))[::-1], id="unsorted"),
    ],
)
def test_plotly_embed_ineligible_x_falls_back_to_equidistant(x_factory):
    """非数值/乱序 x 退回等距步进：不猜语义，保持可预期的降采样。"""
    n = 60_000
    figure = _plotly_line_figure(n, spike_at=1, x=x_factory(n))
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_plotly_figure_for_embed(figure)
    assert calls == [], "x 不合格时不得调用 LTTB"
    assert (before, after) == (n, n // 2)
    original_x = figure["data"][0]["x"]
    assert sampled["data"][0]["x"] == original_x[::2]


def test_plotly_embed_duplicated_x_still_uses_lttb():
    """重复 x（不递减）仍算合格曲线：LTTB 不依赖 x 严格递增，不抛异常。"""
    n = 60_000
    figure = _plotly_line_figure(n, spike_at=1, x=[0.0] * n)
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_plotly_figure_for_embed(figure)
    assert calls == [_ONE_TRACE_CAP]
    assert (before, after) == (n, _EMBED_MAX_POINTS)
    assert max(sampled["data"][0]["y"]) == 500.0


def test_plotly_embed_missing_x_falls_back_to_equidistant():
    """缺 x 的按点 trace（如按索引画的线）退回等距步进。"""
    n = 60_000
    figure = _plotly_line_figure(n, spike_at=1)
    figure["data"][0].pop("x")
    sampled, before, after = sample_plotly_figure_for_embed(figure)
    assert (before, after) == (n, n // 2)
    assert len(sampled["data"][0]["y"]) == n // 2


def test_plotly_embed_big_line_through_real_tool(tmp_path):
    """真实工具链：6 万行数值 x 折线图的 HTML 嵌入确实走了 LTTB。

    单元测试证明抽样器行为，这里证明**接线**：``create_visualization`` 生成
    的 figure（px.line(markers=True) → scattergl 的 lines+markers）确实带上
    了数值 x，因而命中 LTTB 分支，而不是被配置或轨迹形状悄悄绕开。
    """
    n = 60_000
    frame = pd.DataFrame({"t": np.arange(n), "v": _spike_series(n, spike_at=1)})
    workspace = DataWorkspace(tmp_path / "runs", session_id="line_sampling")
    workspace.save_upload("data.csv", b"placeholder")
    workspace._df = frame  # 直接注入测试数据，绕过文件加载
    workspace._profile_cache.clear()
    bundle = tmp_path / "runs" / "line_sampling" / "artifacts" / "plotly.min.js"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("/* mock plotly.min.js */", encoding="utf-8")
    tools = {tool.name: tool for tool in build_tools(workspace)}
    calls: list[int] = []
    with (
        patch.object(DataWorkspace, "ensure_plotly_bundle", return_value=bundle),
        patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)),
    ):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "line", "x": "t", "y": "v", "chart_engine": "plotly",
        }))
    assert result["status"] == "ok"
    assert calls == [_ONE_TRACE_CAP], f"真实折线图未走 LTTB：{calls}"
    sampling = result["sampling"]
    assert sampling["embedded_points"] <= _EMBED_MAX_POINTS
    # 全量数据仍在 .plotly.json 产物里，HTML 只是抽样副本
    assert sampling["original_points"] >= n
    full = chart_sampling._decode_plotly_typed_arrays(
        json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    )
    assert max(max(trace["y"]) for trace in full["data"]) == 500.0
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert "LTTB" in html_text and "等距抽样" in html_text


# ---------------------------------------------------------------------------
# 3b. ECharts 接入
# ---------------------------------------------------------------------------


def test_echarts_value_axis_line_uses_lttb():
    """数值轴折线（[x, y] 点对）：走 LTTB、点数落在预算内、尖峰保住。"""
    n, cap, spike_at = 6_000, 2_000, 1
    pairs = [[float(i), y] for i, y in enumerate(_spike_series(n, spike_at))]
    option = {"xAxis": [{"type": "value"}], "series": [{"type": "line", "data": pairs}]}
    snapshot = copy.deepcopy(option)
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_echarts_option_for_embed(option, max_points=cap)

    assert calls == [cap], "数值轴折线必须走 LTTB"
    assert (before, after) == (n, cap)
    data = sampled["series"][0]["data"]
    assert len(data) == cap
    assert max(row[1] for row in data) == 500.0
    assert [row[0] for row in data] == sorted(row[0] for row in data)
    # 等距步进（step=3）会把这个尖峰跳过
    assert max(row[1] for row in pairs[::3]) == 0.0
    assert option == snapshot, "原始 option 不被修改"


def test_echarts_value_axis_line_flat_values_and_area_type():
    """数值轴上的 ``[y, ...]`` 写法与 ``type="area"``：同样走 LTTB 保峰。"""
    n, cap, spike_at = 6_000, 2_000, 1
    values = _spike_series(n, spike_at)
    option = {
        "xAxis": [{"type": "value"}],
        "series": [
            {"type": "line", "data": list(values)},
            {"type": "area", "data": list(values)},
        ],
    }
    sampled, before, after = sample_echarts_option_for_embed(option, max_points=2 * cap)
    assert (before, after) == (2 * n, 2 * cap)
    for item in sampled["series"]:
        assert len(item["data"]) == cap
        assert max(item["data"]) == 500.0


def test_echarts_category_axis_line_keeps_peaks_and_stays_aligned():
    """类目轴折线也保峰：LTTB 下标**同时**裁 xAxis.data 与各系列，绝不错位。

    ECharts 的折线图（含本项目"月度趋势"）一律用具名类目轴——如果这类图仍用
    等距步进，界面上看到的尖峰会被抹掉，LTTB 只在数值轴上生效等于没生效。
    多系列共享同一组下标，因此选点依据是逐点最大值（上包络）。
    """
    n = 6_000
    option = {
        "xAxis": [{"type": "category", "data": [f"c{i}" for i in range(n)]}],
        "series": [
            {"type": "line", "data": [i * 2 for i in range(n)]},
            {"type": "line", "data": _spike_series(n, spike_at=1, peak=500.0)},
        ],
    }
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_echarts_option_for_embed(option, max_points=2_000)

    assert calls == [2_000], "类目轴纯折线应走 LTTB"
    assert (before, after) == (n, 2_000)
    axis = sampled["xAxis"][0]["data"]
    assert len(axis) == after
    for item in sampled["series"]:
        assert len(item["data"]) == len(axis)
    # 对齐：系列数据仍对应 xAxis.data 的同一个类目，而不是被整体挪位
    assert axis == [f"c{i}" for i in range(n)][:1] + axis[1:]
    peak_index = sampled["series"][1]["data"].index(500.0)
    assert axis[peak_index] == "c1", "尖峰必须落在它原本的类目上"
    assert max(sampled["series"][1]["data"]) == 500.0, "尖峰不能被抽样丢掉"


def test_echarts_category_axis_bar_stays_equidistant():
    """类目轴上只要还有柱状系列，就必须退回等距：抽掉一个类目等于抽掉一根柱子。"""
    n = 6_000
    option = {
        "xAxis": [{"type": "category", "data": [f"c{i}" for i in range(n)]}],
        "series": [
            {"type": "bar", "data": [i % 7 for i in range(n)]},
            {"type": "line", "data": _spike_series(n, spike_at=1, peak=500.0)},
        ],
    }
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_echarts_option_for_embed(option, max_points=2_000)

    assert calls == [], "混合柱线图不得走 LTTB"
    axis = sampled["xAxis"][0]["data"]
    assert axis == [f"c{i}" for i in range(0, n, 3)], "必须是等距抽样"
    assert sampled["series"][0]["data"] == [i % 7 for i in range(0, n, 3)]
    for item in sampled["series"]:
        assert len(item["data"]) == len(axis) == after


def test_echarts_category_axis_line_falls_back_when_values_are_not_numeric():
    """类目轴折线的 y 含非数值（缺失标记）时退回等距，而不是抛异常。"""
    n = 6_000
    values: list[Any] = [float(i % 11) for i in range(n)]
    values[3] = "-"
    option = {
        "xAxis": [{"type": "category", "data": [f"c{i}" for i in range(n)]}],
        "series": [{"type": "line", "data": values}],
    }
    sampled, before, after = sample_echarts_option_for_embed(option, max_points=2_000)
    assert (before, after) == (n, 2_000)
    assert sampled["xAxis"][0]["data"] == [f"c{i}" for i in range(0, n, 3)]


def test_echarts_value_axis_scatter_stays_equidistant():
    """数值轴散点仍是等距抽样（点语义不变，大散点另走密度视图）。"""
    n, cap = 6_000, 2_000
    pairs = [[float(i), float(i % 13)] for i in range(n)]
    option = {"xAxis": [{"type": "value"}], "series": [{"type": "scatter", "data": pairs}]}
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_echarts_option_for_embed(option, max_points=cap)
    assert calls == []
    assert (before, after) == (n, cap)
    assert sampled["series"][0]["data"] == pairs[::3]


def test_echarts_value_axis_bar_untouched():
    """数值轴柱状图不抽样：柱子的 x 是类别维度，重排会改变图形语义。"""
    n = 6_000
    option = {"xAxis": [{"type": "value"}], "series": [{"type": "bar", "data": list(range(n))}]}
    sampled, _, _ = sample_echarts_option_for_embed(option, max_points=2_000)
    assert sampled["series"][0]["data"] == list(range(n))


# ---------------------------------------------------------------------------
# 4. 阈值以下不变 / 退化输入 / 性能
# ---------------------------------------------------------------------------


def test_below_threshold_inputs_unchanged():
    """未超上限的输入字节不变：不抽样、不重排、不调用 LTTB。"""
    n = 30_000
    figure = _plotly_line_figure(n, spike_at=1)
    calls: list[int] = []
    with patch.object(chart_sampling, "_lttb_indices", side_effect=_lttb_spy(calls)):
        sampled, before, after = sample_plotly_figure_for_embed(figure)
    assert calls == []
    assert (before, after) == (n, n)
    assert sampled == figure

    pairs = [[float(i), float(i)] for i in range(n)]
    option = {"xAxis": [{"type": "value"}], "series": [{"type": "line", "data": pairs}]}
    sampled_option, echarts_before, echarts_after = sample_echarts_option_for_embed(option)
    assert (echarts_before, echarts_after) == (n, n)
    assert sampled_option == option


@pytest.mark.parametrize(
    ("x", "y", "threshold"),
    [
        pytest.param([], [], 10, id="empty"),
        pytest.param([0.0], [1.0], 10, id="single-point"),
        pytest.param([0.0, 1.0, 2.0], [0.0, 1.0, 2.0], 3, id="threshold-eq-len"),
        pytest.param([0.0, 1.0, 2.0], [0.0, 1.0, 2.0], 99, id="threshold-gt-len"),
        pytest.param(list(range(50)), [float("nan")] * 50, 10, id="all-nan-y"),
        pytest.param([0.0] * 50, list(range(50)), 10, id="duplicated-x"),
        pytest.param([0.0, 1.0, 2.0, 3.0], [0.0, 1.0], 2, id="x-y-mismatch"),
        pytest.param(list(range(10)), list(range(10)), 1, id="threshold-1"),
    ],
)
def test_lttb_degenerate_inputs_do_not_raise(x, y, threshold):
    """退化输入：不抛异常，且返回的下标仍然可用（递增、端点正确、不越界）。"""
    indices = _lttb_indices(x, y, threshold)
    usable = min(len(x), len(y))
    assert len(indices) <= max(usable, 1)
    assert all(0 <= i < usable for i in indices)
    assert indices == sorted(set(indices))
    if usable == 0:
        assert indices == []
    elif threshold <= 1:
        assert indices == [0], "只要 1 个点时退化为首点"
    else:
        assert indices[0] == 0
        assert indices[-1] == usable - 1


def test_samplers_tolerate_degenerate_series():
    """抽样器遇到全 NaN / 重复 x / 单点也不抛异常，且不越界。"""
    n = 60_000
    nan_figure = _plotly_line_figure(n, spike_at=1)
    nan_figure["data"][0]["y"] = [float("nan")] * n
    sampled, before, after = sample_plotly_figure_for_embed(nan_figure)
    assert (before, after) == (n, _EMBED_MAX_POINTS)
    assert len(sampled["data"][0]["y"]) == _EMBED_MAX_POINTS

    duplicated = {"xAxis": [{"type": "value"}], "series": [
        {"type": "line", "data": [[0.0, float(i % 5)] for i in range(6_000)]},
    ]}
    sampled_option, dup_before, dup_after = sample_echarts_option_for_embed(duplicated, 2_000)
    assert (dup_before, dup_after) == (6_000, 2_000)
    assert len(sampled_option["series"][0]["data"]) == 2_000

    single = {"xAxis": [{"type": "value"}], "series": [{"type": "line", "data": [[1.0, 2.0]]}]}
    assert sample_echarts_option_for_embed(single, 1_000)[1:] == (1, 1)


def test_lttb_300k_within_time_budget():
    """30 万点降采样的开销要"可接受"，但**不能**用挂钟绝对值卡硬件。

    本机实测 30 万→5 万约 0.18s；CI runner 明显更慢（实测 1.17s），
    原来写死 1s 会变成"看机器脸色"的假红。改为：
    1. 相对同一台机器上的等距抽样基线（同样是 30 万点、同样的数组重建），
       LTTB 不得慢过 4 倍——算法是 numpy 向量化的，慢一个数量级才是回归；
    2. 另留一个宽松的绝对上限（15s）纯粹当"挂死/退化到逐点循环"的护栏。
    """
    rng = np.random.default_rng(2026)
    n = 300_000
    x = np.arange(n, dtype=float)
    y = rng.normal(0, 1, n)

    started = time.perf_counter()
    indices = _lttb_indices(x, y, _ONE_TRACE_CAP)
    lttb_elapsed = time.perf_counter() - started

    # 基线：同规模数据的等距抽样（numpy 切片 + list 化，代表"最省的实现"）
    started = time.perf_counter()
    step = math.ceil(n / _ONE_TRACE_CAP)
    baseline = x[::step].tolist(), y[::step].tolist()
    baseline_elapsed = time.perf_counter() - started

    assert len(indices) == _ONE_TRACE_CAP
    assert len(baseline[0]) == _ONE_TRACE_CAP
    assert lttb_elapsed < 15.0, f"30 万点 LTTB 用时 {lttb_elapsed:.3f}s，疑似退化为逐点循环"
    assert lttb_elapsed < max(0.5, baseline_elapsed * 4 + 0.5), (
        f"LTTB {lttb_elapsed:.3f}s 相对等距基线 {baseline_elapsed:.3f}s 慢得过多"
    )


def test_plotly_embed_300k_within_time_budget():
    """端到端（解码 typed array + 抽样）同样只看"相对基线 + 宽松上限"。"""
    rng = np.random.default_rng(2027)
    n = 300_000
    x_values = np.arange(n, dtype=float).tolist()
    y_values = rng.normal(0, 1, n).tolist()

    figure = {"data": [{
        "type": "scattergl", "mode": "lines",
        "x": list(x_values), "y": list(y_values),
    }]}
    started = time.perf_counter()
    _, before, after = sample_plotly_figure_for_embed(figure)
    lttb_elapsed = time.perf_counter() - started

    # 基线：同规模数据的等距抽样（走类目轴分支，即未改动的旧路径成本）
    baseline_figure = {"data": [{
        "type": "scattergl", "mode": "lines",
        "x": [f"c{index}" for index in range(0, n, 1000)], "y": y_values[::1000],
    }]}
    started = time.perf_counter()
    sample_plotly_figure_for_embed(baseline_figure, 1_000)
    baseline_elapsed = time.perf_counter() - started

    assert (before, after) == (n, _EMBED_MAX_POINTS)
    assert lttb_elapsed < 20.0, f"30 万点嵌入抽样用时 {lttb_elapsed:.3f}s，疑似挂死"
    # 基线只抽样 300 个点（构造 x 的开销在计时之外），因此这里给足余量：
    # 只要不是数量级级别的退化就算通过。
    assert lttb_elapsed < max(2.0, baseline_elapsed * 20), (
        f"端到端抽样 {lttb_elapsed:.3f}s 相对基线 {baseline_elapsed:.3f}s 退化过多"
    )
