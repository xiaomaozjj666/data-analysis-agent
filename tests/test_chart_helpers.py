"""图表可访问数据表与 3D 辅助函数的边界分支。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from data_agent.tools._plotly_render import (
    _plotly_trace_colors,
    _plotly_trace_values,
    _scatter_3d_aspect,
    _summary_cell,
    append_html_body_block,
    build_data_summary,
    render_data_table_html,
    scatter3d_data_summary,
)


def test_summary_cell_normalizes_values():
    assert _summary_cell(None) is None
    assert _summary_cell(np.int64(3)) == 3
    assert _summary_cell(float("inf")) is None
    assert _summary_cell(float("nan")) is None
    assert _summary_cell("x") == "x"
    assert _summary_cell(True) is True
    assert _summary_cell(pd.Timestamp("2026-11-19")) == "2026-11-19T00:00:00"
    assert _summary_cell(object()) .startswith("<object")


def test_build_and_render_table():
    summary = build_data_summary(
        ["a", "b"],
        [["1", None], ["<script>", 2]],
        points=2,
        sampled=True,
        max_rows=1,
    )
    assert summary["rows"] == [["1", None]]
    assert summary["sampled"] is True
    assert summary["points"] == 2
    html = render_data_table_html(summary)
    assert "<table>" in html and "scope=\"col\"" in html
    assert "—" in html
    assert render_data_table_html(None) == ""
    assert render_data_table_html({"columns": [], "rows": []}) == ""

    escaped = render_data_table_html(
        build_data_summary(["x"], [["<script>alert(1)</script>"]], points=1)
    )
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_trace_value_and_color_helpers():
    class Broken:
        def __array__(self, *args, **kwargs):
            raise TypeError("no array")

        def __iter__(self):
            raise TypeError("no iter")

    assert _plotly_trace_values(SimpleNamespace(x=None), "x") == []
    assert _plotly_trace_values(SimpleNamespace(x=Broken()), "x") == []
    assert _plotly_trace_values(SimpleNamespace(x=[1, 2]), "x") == [1, 2]
    assert _plotly_trace_values(SimpleNamespace(x=np.array([3, 4])), "x") == [3, 4]

    assert _plotly_trace_colors(SimpleNamespace(marker=None), 2) is None
    assert _plotly_trace_colors(SimpleNamespace(marker=SimpleNamespace(color="#fff")), 2) is None
    assert _plotly_trace_colors(
        SimpleNamespace(marker=SimpleNamespace(color=[1, 2, 3])), 2
    ) is None
    assert _plotly_trace_colors(
        SimpleNamespace(marker=SimpleNamespace(color=["#111", "#222"])), 2
    ) is None
    assert _plotly_trace_colors(
        SimpleNamespace(marker=SimpleNamespace(color=[1.5, 2.5])), 2
    ) == [1.5, 2.5]


def test_scatter3d_summary_handles_traces_and_colors():
    first = go.Scatter3d(
        x=[1, 2],
        y=[3, 4],
        z=[5, 6],
        name="A",
        marker={"color": ["#111", "#222"]},
    )
    second = go.Scatter3d(x=[7], y=[8], z=[9], name="B")
    other = go.Scatter(x=[1], y=[2])
    fig = go.Figure(data=[first, second, other])

    summary = scatter3d_data_summary(fig, x="x", y="y", z="z", color="group", sampled=True)
    assert summary["columns"] == ["x", "y", "z", "group"]
    assert summary["points"] == 3
    assert summary["sampled"] is True
    assert summary["rows"][0] == [1, 3, 5, "A"]

    plain = scatter3d_data_summary(fig, x="x", y="y", z="z", color=None, sampled=False)
    assert plain["columns"] == ["x", "y", "z"]
    assert plain["rows"][0] == [1, 3, 5]


def test_scatter3d_aspect_ignores_missing_and_invalid_axes():
    fig = go.Figure(
        data=[
            go.Scatter3d(x=[1, 2], y=[1, 1000], z=[1, 1]),
            go.Scatter3d(x=["a", "b"], y=None, z=[1, 2]),
        ]
    )
    aspect = _scatter_3d_aspect(fig)
    assert set(aspect) == {"x", "y", "z"}
    assert all(math.isfinite(value) for value in aspect.values())


def test_append_html_block():
    assert append_html_body_block("<html><body>x</body></html>", "<p>t</p>") == (
        "<html><body>x<p>t</p></body></html>"
    )
    assert append_html_body_block("<div>x</div>", "<p>t</p>") == "<div>x</div><p>t</p>"
    assert append_html_body_block("x", "") == "x"


def test_stratified_sample_and_density_guards():
    from data_agent.tools._plotly_render import _density_view_for, _stratified_sample

    df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
    sampled = _stratified_sample(df, "missing", 2)
    assert len(sampled) == 2
    empty = df.iloc[0:0]
    assert _stratified_sample(empty, None, 2).empty
    view, scale = _density_view_for(df, x="x", y="missing", color=None, scale_mode="auto")
    assert view is None and scale is None
    view, scale = _density_view_for(pd.DataFrame({"x": [1], "y": [2]}), x="x", y="y", color=None, scale_mode="auto")
    assert view is None and scale is None

    broken = SimpleNamespace(marker=SimpleNamespace(color=object()))
    assert _plotly_trace_colors(broken, 1) is None
