"""render.primitives 的边界分支回归。

这些辅助函数被柱状图 / 分布图 / 3D 图共用，输入来自用户数据与模型推断，
容易出现空数组、全 NaN、零宽度等退化情况。此前这些分支没有测试覆盖，
一旦改动就容易把图表渲染打挂，因此集中补上。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from data_agent.render.primitives import (
    _axis_label_interval,
    _bin_index,
    _bin_labels,
    _round_significant,
    _snap_edges,
    _stacked_totals,
    _within_range,
)


def test_stacked_totals_requires_usable_columns() -> None:
    frame = pd.DataFrame({"x": ["a"], "y": [1.0], "c": ["East"]})
    assert _stacked_totals(frame, x="", y="y", color="c") is None
    assert _stacked_totals(frame, x="x", y=None, color="c") is None
    assert _stacked_totals(frame, x="x", y="y", color="c") == [1.0]
    assert _stacked_totals(frame, x="missing", y="y", color=None) is None


def test_stacked_totals_sums_duplicate_points_after_numeric_coercion() -> None:
    frame = pd.DataFrame(
        {"x": ["a", "a", "b", "b"], "y": [1.0, 2.0, 7.0, "not-a-number"]}
    )
    totals = _stacked_totals(frame, x="x", y="y", color="c")
    assert totals == [3.0, 7.0]


def test_round_significant_handles_empty_array() -> None:
    rounded = _round_significant(np.array([], dtype=float))
    assert rounded.size == 0


def test_round_significant_trims_to_requested_digits() -> None:
    values = np.array([0.001234, 12.3456, 1234.56])
    assert np.allclose(_round_significant(values, sig=3), [0.00123, 12.3, 1230.0])
    # 6 位有效数字下基本保持原值
    assert np.allclose(_round_significant(values), values)


def test_snap_edges_returns_input_for_degenerate_cases() -> None:
    single = np.array([1.0])
    assert np.array_equal(_snap_edges(single), single)

    constant = np.array([2.0, 2.0])
    assert np.array_equal(_snap_edges(constant), constant)

    with_nan = np.array([np.nan, np.nan])
    assert np.all(np.isnan(_snap_edges(with_nan)))


def test_snap_edges_keeps_monotonic_edges() -> None:
    edges = np.array([0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
    snapped = _snap_edges(edges)
    assert np.all(np.diff(snapped) > 0)


def test_bin_labels_requires_at_least_two_edges() -> None:
    assert _bin_labels(np.array([1.0])) == []
    assert _bin_labels(np.array([], dtype=float)) == []


def test_bin_labels_falls_back_to_precision_when_short_labels_collide() -> None:
    # 100.x 在短格式下都会显示成 "100"，必须自动补足小数位避免标签重复
    edges = np.array([100.0, 100.1, 100.2, 100.3, 100.4])
    labels = _bin_labels(edges)
    assert len(labels) == 4
    assert len(set(labels)) == 4


def test_bin_index_handles_degenerate_edges_and_values() -> None:
    assert _bin_index(5.0, np.array([1.0])) == 0
    assert _bin_index(float("nan"), np.array([0.0, 1.0, 2.0])) == 0

    constant_edges = np.array([1.0, 1.0])
    assert _bin_index(1.0, constant_edges) == 0

    edges = np.array([0.0, 10.0, 20.0, 30.0])
    assert _bin_index(-5.0, edges) == 0
    assert _bin_index(100.0, edges) == len(edges) - 2
    assert _bin_index(15.0, edges) == 1
    assert _bin_index(14.0, edges, nearest=True) == 1
    assert _bin_index(21.0, edges, nearest=True) == 2


def test_within_range_rejects_invalid_values() -> None:
    assert _within_range("abc", (0.0, 10.0)) is False
    assert _within_range(None, (0.0, 10.0)) is False
    assert _within_range(float("nan"), (0.0, 10.0)) is False
    assert _within_range(5.0, (0.0, 10.0)) is True
    assert _within_range(11.0, (0.0, 10.0)) is False


def test_axis_label_interval_only_widens_for_dense_axes() -> None:
    assert _axis_label_interval(5, 10) == 0
    assert _axis_label_interval(100, 14) == 7
    assert _axis_label_interval(0, 14) == 0
