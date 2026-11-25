"""散点矩阵（SPLOM）：N×N 多 grid 网格，非对角为两两散点，对角为分布直方图。"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_AXIS_LABEL_FORMATTER_JS,
    _ECHARTS_BASE_LEGEND,
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLBOX,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_BORDER_COLOR,
    _ECHARTS_GRID_COLOR,
    _ECHARTS_PALETTE,
    _ECHARTS_TEXT_SECONDARY,
    _JsFunction,
)
from data_agent.render.primitives import _hex_to_rgba, _rows_data, _series_color
from data_agent.render.series_scatter import _scatter_tooltip_formatter
from data_agent.tools._helpers import _human_column_label as _build_axis_label


def _splom_cell_axis(
    *,
    name: str | None,
    show_label: bool,
    is_category: bool,
    data: list[str] | None = None,
) -> dict[str, Any]:
    """SPLOM 单元格坐标轴：仅边缘格显示刻度标签与变量名，内部格留白防拥挤。"""
    axis: dict[str, Any] = {
        "type": "category" if is_category else "value",
        "scale": not is_category,
        "axisLine": {"lineStyle": {"color": _ECHARTS_GRID_COLOR}},
        "axisTick": {"show": False},
        "axisLabel": {
            "show": show_label,
            "color": _ECHARTS_TEXT_SECONDARY,
            "fontSize": 10,
            # 小格子里大数值标签会互相重叠，用万/亿缩写压短
            **({} if is_category else {"formatter": _ECHARTS_AXIS_LABEL_FORMATTER_JS}),
        },
        "splitLine": {
            "show": not is_category,
            "lineStyle": {"color": _ECHARTS_BORDER_COLOR, "type": "dashed"},
        },
    }
    if is_category:
        axis["data"] = data or []
    if name:
        axis.update(
            {
                "name": name,
                "nameLocation": "middle",
                "nameTextStyle": {
                    "color": _ECHARTS_TEXT_SECONDARY,
                    "fontSize": 11,
                    "fontWeight": 600,
                },
            }
        )
    return axis


def _splom_diagonal_cell(
    work: pd.DataFrame,
    *,
    dim_x: str,
    dim_y: str,
    index: int,
    grid_index: int,
    bottom: bool,
    leftmost: bool,
    x_label: str,
    y_label: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """对角格：该变量的分布直方图，颜色取该变量在色板中的专属色。"""
    counts, edges = np.histogram(work[dim_x].astype(float), bins=10)
    mids = [f"{(edges[b] + edges[b + 1]) / 2:.4g}" for b in range(len(counts))]
    xa = _splom_cell_axis(
        name=x_label if bottom else None, show_label=False, is_category=True, data=mids
    )
    ya = _splom_cell_axis(name=y_label if leftmost else None, show_label=False, is_category=False)
    ya["scale"] = False
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    series = {
        "name": f"__hist_{index}",
        "type": "bar",
        "xAxisIndex": grid_index,
        "yAxisIndex": grid_index,
        "data": [int(c) for c in counts],
        "barWidth": "88%",
        "itemStyle": {
            "color": _hex_to_rgba(_series_color(index), 0.68),
            "borderColor": _series_color(index),
            "borderWidth": 1,
            "borderRadius": [2, 2, 0, 0],
        },
        "tooltip": {
            "formatter": _JsFunction(
                "function(p){return '<div style=\"font-weight:600;margin-bottom:6px;\">'+"
                f"{x_label_js}+' 分布</div>'"
                "+'<span style=\"color:var(--tt-muted,#6b7280);\">区间中值：</span><b>'+p.name+'</b><br/>'"
                "+'<span style=\"color:var(--tt-muted,#6b7280);\">频数：</span><b>'+p.value+'</b>';}"
            )
        },
    }
    return xa, ya, series


def _splom_pair_cells(
    work: pd.DataFrame,
    *,
    dim_x: str,
    dim_y: str,
    grid_index: int,
    bottom: bool,
    leftmost: bool,
    x_label: str,
    y_label: str,
    levels: list[Any],
    color: str | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """非对角格：dim_x × dim_y 两两散点，颜色分组与其他格联动。"""
    xa = _splom_cell_axis(name=x_label if bottom else None, show_label=bottom, is_category=False)
    ya = _splom_cell_axis(
        name=y_label if leftmost else None, show_label=leftmost, is_category=False
    )
    pair_fmt = _scatter_tooltip_formatter(x_label, y_label, None)
    groups = (
        [(str(lv), work[work[color] == lv], _series_color(idx)) for idx, lv in enumerate(levels)]
        if levels
        else [("样本", work, _ECHARTS_PALETTE[0])]
    )
    series: list[dict[str, Any]] = []
    for gname, sub, gcolor in groups:
        series.append(
            {
                "name": gname,
                "type": "scatter",
                "xAxisIndex": grid_index,
                "yAxisIndex": grid_index,
                "data": _rows_data(sub, [dim_x, dim_y]),
                "symbolSize": 6,
                "itemStyle": {
                    "color": gcolor,
                    "opacity": 0.7,
                    "borderWidth": 0.5,
                    "borderColor": "#fff",
                },
                "emphasis": {"scale": 1.6, "itemStyle": {"opacity": 1}},
                "tooltip": {"formatter": pair_fmt},
            }
        )
    return xa, ya, series


def _echarts_scatter_matrix(
    df: pd.DataFrame,
    *,
    dimensions: list[str],
    color: str | None,
    title: str,
) -> dict[str, Any]:
    """散点矩阵（SPLOM）：N×N 多 grid 网格，非对角为两两散点，对角为分布直方图。"""
    numeric = [d for d in dimensions if d in df.columns and pd.api.types.is_numeric_dtype(df[d])]
    if not numeric:
        return {"title": {"text": title}}

    notes: list[str] = []
    if len(numeric) > 4:
        notes.append(f"维度较多，仅展示前 4 个（共 {len(numeric)} 个）")
        numeric = numeric[:4]
    n = len(numeric)

    # 对齐样本：数值列全部有效的行；样本过多时抽样，避免 N² 格渲染卡顿
    cols = [*numeric] + ([color] if color and color in df.columns and color not in numeric else [])
    work = df[cols].copy()
    for c in numeric:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    work = work.dropna(subset=numeric)
    if len(work) > 400:
        notes.append(f"样本较多，随机抽样 400 条展示（共 {len(work)} 条）")
        work = work.sample(n=400, random_state=42)

    # N×N 网格布局：百分比定位随容器缩放，左/下留白给刻度标签与变量名
    left0, top0, gap_x, gap_y = 7.0, 15.0, 2.4, 3.2
    cell_w = (95.0 - left0 - gap_x * (n - 1)) / n
    cell_h = (86.0 - top0 - gap_y * (n - 1)) / n

    grids: list[dict[str, Any]] = []
    x_axes: list[dict[str, Any]] = []
    y_axes: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    levels = list(pd.unique(work[color].dropna())) if color and color in work.columns else []

    for i, dim_y in enumerate(numeric):  # 行：y 变量
        for j, dim_x in enumerate(numeric):  # 列：x 变量
            k = i * n + j
            grids.append(
                {
                    "left": f"{left0 + j * (cell_w + gap_x):.2f}%",
                    "top": f"{top0 + i * (cell_h + gap_y):.2f}%",
                    "width": f"{cell_w:.2f}%",
                    "height": f"{cell_h:.2f}%",
                }
            )
            bottom = i == n - 1
            leftmost = j == 0
            x_label, y_label = _build_axis_label(dim_x), _build_axis_label(dim_y)

            if i == j:
                xa, ya, cell_series = _splom_diagonal_cell(
                    work,
                    dim_x=dim_x,
                    dim_y=dim_y,
                    index=i,
                    grid_index=k,
                    bottom=bottom,
                    leftmost=leftmost,
                    x_label=x_label,
                    y_label=y_label,
                )
                series.append(cell_series)
            else:
                xa, ya, cell_series = _splom_pair_cells(
                    work,
                    dim_x=dim_x,
                    dim_y=dim_y,
                    grid_index=k,
                    bottom=bottom,
                    leftmost=leftmost,
                    x_label=x_label,
                    y_label=y_label,
                    levels=levels,
                    color=color,
                )
                series.extend(cell_series)

            if bottom:
                xa["nameGap"] = 24
            if leftmost:
                ya["nameGap"] = 38
            xa["gridIndex"] = ya["gridIndex"] = k
            x_axes.append(xa)
            y_axes.append(ya)

    base: dict[str, Any] = {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            "subtext": "；".join(notes) if notes else f"{n}×{n} 散点矩阵，对角线为分布直方图",
        },
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item"},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "grid": grids,
        "xAxis": x_axes,
        "yAxis": y_axes,
        "series": series,
    }
    if levels:
        # 图例只收录颜色分组，隐藏对角直方图的内部系列名；点击图例可全矩阵联动筛选
        base["legend"] = {**_ECHARTS_BASE_LEGEND, "data": [str(item) for item in levels]}
    return base
