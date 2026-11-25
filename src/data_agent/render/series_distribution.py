"""分布类图型：环形饼图、直方图、箱线/小提琴图、热力/相关性热力图。"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_BASE_AXIS,
    _ECHARTS_BASE_GRID,
    _ECHARTS_BASE_LEGEND,
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLBOX,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_PALETTE,
    _ECHARTS_TEXT_COLOR,
    _ECHARTS_TEXT_SECONDARY,
    _ECHARTS_X_AXIS_NAME,
    _JsFunction,
)
from data_agent.render.primitives import (
    _build_linear_gradient,
    _echarts_value_axis,
    _format_number,
    _hex_to_rgba,
    _safe_value,
    _series_color,
)
from data_agent.render.series_cartesian import _echarts_bar
from data_agent.tools._helpers import _human_column_label as _build_axis_label


def _echarts_pie(
    df: pd.DataFrame,
    *,
    x: str,
    values: str | None,
    y: str | None,
    title: str,
) -> dict[str, Any]:
    """环形饼图：内嵌总数、悬浮占比、图例隐藏。"""
    value_col = values or y
    if not value_col or value_col not in df.columns:
        # 退化为计数（按 x 聚合，避免重复类别产生多个同名扇区）
        counts = df[x].value_counts()
        data = [{"name": str(k), "value": int(v)} for k, v in counts.items()]
    else:
        # 按 x 聚合求和，避免 x 含重复行（如 product×channel 未分组）导致同名扇区错乱。
        grouped = df.groupby(x, dropna=False)[value_col].sum()
        data = [{"name": str(k), "value": _safe_value(v)} for k, v in grouped.items()]
    total = sum(d["value"] or 0 for d in data)

    return {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            "subtext": f"共 {len(data)} 类 · 合计 {_format_number(total)}",
        },
        "tooltip": {
            **_ECHARTS_BASE_TOOLTIP,
            "trigger": "item",
            "formatter": _JsFunction(
                "function(p){return '<div style=\"font-weight:600;margin-bottom:4px;\">'+p.name+'</div><span style=\"color:var(--tt-muted,#6b7280);\">'+p.seriesName+'：</span><b>'+p.value.toLocaleString()+'</b> ('+p.percent+'%)';}"
            ),
        },
        "legend": {
            **_ECHARTS_BASE_LEGEND,
            "orient": "vertical",
            "right": 16,
            "top": "middle",
            "itemGap": 12,
        },
        "toolbox": {
            "right": 70,
            "top": 24,
            "feature": {"saveAsImage": {"title": "导出 PNG", "pixelRatio": 2}},
        },
        "color": _ECHARTS_PALETTE,
        "series": [
            {
                "name": _build_axis_label(value_col) if value_col else "计数",
                "type": "pie",
                "radius": ["42%", "68%"],
                "center": ["40%", "52%"],
                "avoidLabelOverlap": True,
                "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "borderRadius": 6},
                "label": {
                    "show": True,
                    "color": _ECHARTS_TEXT_COLOR,
                    "fontSize": 12,
                    "formatter": "{b}\n{d}%",
                },
                "labelLine": {"length": 12, "length2": 14, "smooth": True},
                "emphasis": {
                    "itemStyle": {"shadowBlur": 16, "shadowColor": "rgba(0,0,0,0.15)"},
                    "label": {"fontSize": 14, "fontWeight": 600},
                },
                "data": data,
            }
        ],
    }


def _echarts_histogram(
    df: pd.DataFrame,
    *,
    x: str,
    color: str | None,
    bins: int,
    title: str,
) -> dict[str, Any]:
    """直方图：自动分箱 + 边际箱线图（叠加在底部）。"""
    x_label = _build_axis_label(x)
    series_data = df[x].dropna().astype(float)
    if series_data.empty:
        return _echarts_bar(df, x=x, y=None, color=color, aggregation="none", title=title)

    counts, edges = np.histogram(series_data, bins=min(max(bins, 2), 200))
    categories = [f"{edges[i]:.1f}-{edges[i + 1]:.1f}" for i in range(len(counts))]
    data = [int(c) for c in counts]

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} 分布直方图"},
        "tooltip": {
            **_ECHARTS_BASE_TOOLTIP,
            "trigger": "axis",
            "axisPointer": {"type": "shadow"},
            "formatter": _JsFunction(
                "function(params){var p=params[0];return '<div style=\"font-weight:600;\">区间：'+p.axisValue+'</div><span style=\"color:var(--tt-muted,#6b7280);\">频数：</span><b>'+p.value.toLocaleString()+'</b>';}"
            ),
        },
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": [_ECHARTS_PALETTE[0]],
        "xAxis": [
            {
                **_ECHARTS_BASE_AXIS,
                "type": "category",
                "data": categories,
                "name": x_label,
                **_ECHARTS_X_AXIS_NAME,
                "axisLabel": {**_ECHARTS_BASE_AXIS["axisLabel"], "rotate": 35},
            }
        ],
        # Y 轴范围必须按分箱频数计算（并入 0 作基线）；若误用原始 x 列取值，
        # 数据量级与频数不匹配时柱体会被压扁或冲出绘图区。
        "yAxis": [
            _echarts_value_axis(
                df, None, name="频数", scale=False, values=[0.0, *(float(c) for c in data)]
            )
        ],
        "series": [
            {
                "name": "频数",
                "type": "bar",
                "data": data,
                "barWidth": "92%",
                "itemStyle": {
                    "color": _build_linear_gradient(_ECHARTS_PALETTE[0]),
                    "borderRadius": [4, 4, 0, 0],
                },
                "emphasis": {"itemStyle": {"color": _ECHARTS_PALETTE[0]}},
            }
        ],
        "dataZoom": [{"type": "inside", "start": 0, "end": 100}],
    }
    return base


def _box_stats(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
) -> tuple[
    list[str],
    list[dict[str, Any]],
    list[list[Any]],
    list[list[Any]],
    list[dict[str, Any]],
]:
    """按 x 分组计算箱线图五数概括、离群点、均值点与样本统计。"""
    groups = df.groupby(x, dropna=False)[y]
    categories: list[str] = []
    box_data: list[dict[str, Any]] = []
    outlier_points: list[list[Any]] = []
    mean_points: list[list[Any]] = []
    stats_meta: list[dict[str, Any]] = []
    for key, group in groups:
        vals = pd.to_numeric(group, errors="coerce").dropna().astype(float)
        if vals.empty:
            continue  # 空组跳过，避免画出 [0,0,0,0,0] 的假箱体
        name = str(key)
        q1 = float(vals.quantile(0.25))
        q2 = float(vals.quantile(0.5))
        q3 = float(vals.quantile(0.75))
        iqr = q3 - q1
        lower = max(float(vals.min()), q1 - 1.5 * iqr)
        upper = min(float(vals.max()), q3 + 1.5 * iqr)
        idx = len(categories)
        categories.append(name)
        box_data.append(
            {
                "value": [round(v, 4) for v in (lower, q1, q2, q3, upper)],
                "itemStyle": {
                    "color": _hex_to_rgba(_series_color(idx), 0.18),
                    "borderColor": _series_color(idx),
                    "borderWidth": 1.5,
                },
            }
        )
        outliers = vals[(vals < lower) | (vals > upper)]
        if len(outliers) > 100:
            # 离群点过多时等距抽样，避免 option 体积膨胀拖慢渲染
            outliers = outliers.iloc[:: len(outliers) // 100 + 1]
        outlier_points.extend([name, round(float(v), 4)] for v in outliers)
        mean_points.append([name, round(float(vals.mean()), 4)])
        stats_meta.append({"count": int(vals.size), "mean": round(float(vals.mean()), 2)})
    return categories, box_data, outlier_points, mean_points, stats_meta


def _box_tooltip_formatter(stats_meta: list[dict[str, Any]]) -> _JsFunction:
    """箱线图 tooltip：五数概括 + 样本数 + 均值（按 dataIndex 内嵌查表）。

    # tooltip：boxplot 的 value 可能带类目索引前缀（6 元），统一取末 5 位五数概括；
    # 样本数/均值通过 json 内嵌进函数体，按 dataIndex 对齐查表。
    """
    stats_js = json.dumps(stats_meta, ensure_ascii=False)
    return _JsFunction(
        "function(p){"
        "var f=function(v){return (v===null||v===undefined)?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:2});};"
        "if(p.seriesType==='scatter'){"
        "return '<div style=\"font-weight:600;margin-bottom:4px;\">'+p.value[0]+'</div>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">'+p.seriesName+'：</span><b>'+f(p.value[1])+'</b>';}"
        "var v=p.value;var b=v.length>5?[v[1],v[2],v[3],v[4],v[5]]:v;"
        "var s=(" + stats_js + ")[p.dataIndex]||{};"
        "return '<div style=\"font-weight:600;margin-bottom:4px;\">'+p.name+'</div>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">上须：</span><b>'+f(b[4])+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">上四分位 Q3：</span><b>'+f(b[3])+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">中位数：</span><b>'+f(b[2])+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">下四分位 Q1：</span><b>'+f(b[1])+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">下须：</span><b>'+f(b[0])+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">样本数：</span><b>'+(s.count===undefined?'—':s.count.toLocaleString())+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">均值：</span><b>'+f(s.mean)+'</b>';"
        "}"
    )


def _echarts_box(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    title: str,
    violin: bool = False,
) -> dict[str, Any]:
    """箱线图 / 小提琴图：按 x 分组展示 y 分布。

    细节设计：
    - ECharts boxplot data 只接受五数概括，离群点拆为独立 scatter 系列叠加
    - 均值以菱形标记叠加，便于与中位数对比判断偏态
    - tooltip 展示五数概括 + 样本数 + 均值；空组直接跳过不画假箱体
    - 每组独立配色，类别多时旋转标签并启用滚轮缩放
    """
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else ""
    if y is None:
        return _echarts_histogram(df, x=x, color=color, bins=30, title=title)

    categories, box_data, outlier_points, mean_points, stats_meta = _box_stats(df, x=x, y=y)
    tooltip_formatter = _box_tooltip_formatter(stats_meta)

    series: list[dict[str, Any]] = [
        {
            "name": y_label,
            "type": "boxplot",
            "data": box_data,
            "boxWidth": [10, 44],
            "emphasis": {"itemStyle": {"borderWidth": 2, "shadowBlur": 8}},
        }
    ]
    if outlier_points:
        series.append(
            {
                "name": "离群值",
                "type": "scatter",
                "data": outlier_points,
                "symbolSize": 7,
                "itemStyle": {
                    "color": _hex_to_rgba(_ECHARTS_PALETTE[3], 0.55),
                    "borderColor": _ECHARTS_PALETTE[3],
                    "borderWidth": 1,
                },
                "z": 3,
            }
        )
    series.append(
        {
            "name": "均值",
            "type": "scatter",
            "data": mean_points,
            "symbol": "diamond",
            "symbolSize": 9,
            "itemStyle": {
                "color": "#ffffff",
                "borderColor": _ECHARTS_TEXT_COLOR,
                "borderWidth": 1.5,
            },
            "z": 4,
        }
    )

    option: dict[str, Any] = {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            "subtext": f"{x_label} 分组 × {y_label} 分布",
        },
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item", "formatter": tooltip_formatter},
        "legend": {**_ECHARTS_BASE_LEGEND, "data": [s["name"] for s in series]},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [
            {
                **_ECHARTS_BASE_AXIS,
                "type": "category",
                "data": categories,
                "name": x_label,
                **_ECHARTS_X_AXIS_NAME,
                "axisLabel": {
                    **_ECHARTS_BASE_AXIS["axisLabel"],
                    "rotate": 30 if len(categories) > 8 else 0,
                },
            }
        ],
        "yAxis": [_echarts_value_axis(df, y, name=y_label, scale=True)],
        "series": series,
    }
    if len(categories) > 10:
        option["dataZoom"] = [{"type": "inside", "xAxisIndex": 0}]
    return option


def _heatmap_matrix(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    values: str | None,
    is_correlation: bool,
) -> tuple[list[str], list[str], list[list[Any]], float, float, str] | None:
    """热力图的轴名、矩阵与色标范围；无法构建时返回 ``None``。"""
    if is_correlation:
        numeric = df.select_dtypes(include="number")
        if numeric.empty:
            return None
        corr = numeric.corr()
        x_names = [_build_axis_label(str(c)) for c in corr.columns]
        y_names = [_build_axis_label(str(i)) for i in corr.index]
        raw: list[list[Any]] = [
            [
                None if math.isnan(float(corr.iloc[i, j])) else round(float(corr.iloc[i, j]), 3)
                for j in range(len(x_names))
            ]
            for i in range(len(y_names))
        ]
        vmin, vmax = -1.0, 1.0
        value_label = "相关系数"
        return x_names, y_names, raw, vmin, vmax, value_label

    if not x or not y or not values:
        return None
    pivot = df.pivot_table(index=y, columns=x, values=values, aggfunc="mean")
    x_names = [str(c) for c in pivot.columns]
    y_names = [str(i) for i in pivot.index]
    raw = [
        [_safe_value(pivot.iloc[i, j]) for j in range(len(x_names))]
        for i in range(len(y_names))
    ]
    valid = [v for row in raw for v in row if v is not None]
    vmin = float(min(valid)) if valid else 0.0
    vmax = float(max(valid)) if valid else 1.0
    if vmax - vmin < 1e-12:
        vmin, vmax = vmin - 0.5, vmax + 0.5
    value_label = _build_axis_label(values)
    return x_names, y_names, raw, vmin, vmax, value_label


def _echarts_heatmap(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    values: str | None,
    title: str,
    is_correlation: bool = False,
) -> dict[str, Any]:
    """热力图 / 相关性热力图。

    细节设计：
    - tooltip 内嵌行列名映射（JS 作用域里拿不到 Python 变量，必须序列化进函数体）
    - 深色单元格标签自动切白字，缺失格显示空白而非 "null"
    - 相关性用 RdBu 蓝—白—红 7 段发散色板（红=正相关，符合统计惯例），普通数值用顺序蓝色渐变
    - 单元格过多时隐藏标签防重叠；全值相同时给 visualMap 撑出非零区间
    """
    matrix = _heatmap_matrix(df, x=x, y=y, values=values, is_correlation=is_correlation)
    if matrix is None:
        return {"title": {"text": title}}
    x_names, y_names, raw, vmin, vmax, value_label = matrix

    # 深色格标签切白字：发散色板看 |v|（两端都深），顺序色板看归一化位置
    span = vmax - vmin
    cells: list[dict[str, Any]] = []
    for i in range(len(y_names)):
        for j in range(len(x_names)):
            v = raw[i][j]
            cell: dict[str, Any] = {"value": [j, i, v]}
            if v is not None:
                dark = abs(v) >= 0.75 if is_correlation else (v - vmin) / span >= 0.6
                if dark:
                    cell["label"] = {"color": "#ffffff"}
            cells.append(cell)

    show_label = len(x_names) * len(y_names) <= 200
    label_formatter = _JsFunction(
        "function(p){var v=p.value[2];if(v===null||v===undefined){return '';}"
        + (
            "return v.toFixed(2);}"
            if is_correlation
            else "return Math.abs(v)>=10000?(v/10000).toFixed(1)+'万':Number(v.toFixed(2)).toLocaleString();}"
        )
    )
    tooltip_formatter = _JsFunction(
        "function(p){var X="
        + json.dumps(x_names, ensure_ascii=False)
        + ",Y="
        + json.dumps(y_names, ensure_ascii=False)
        + ";"
        "var v=p.value[2];"
        "var t=(v===null||v===undefined)?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:3});"
        "return '<div style=\"font-weight:600;margin-bottom:4px;\">'+Y[p.value[1]]+' × '+X[p.value[0]]+'</div>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">'+"
        + json.dumps(value_label, ensure_ascii=False)
        + "+'：</span><b>'+t+'</b>';}"
    )

    return _heatmap_option(
        title=title,
        is_correlation=is_correlation,
        x=x,
        y=y,
        x_names=x_names,
        y_names=y_names,
        cells=cells,
        show_label=show_label,
        label_formatter=label_formatter,
        tooltip_formatter=tooltip_formatter,
        vmin=vmin,
        vmax=vmax,
        value_label=value_label,
    )


def _heatmap_option(
    *,
    title: str,
    is_correlation: bool,
    x: str,
    y: str | None,
    x_names: list[str],
    y_names: list[str],
    cells: list[dict[str, Any]],
    show_label: bool,
    label_formatter: _JsFunction,
    tooltip_formatter: _JsFunction,
    vmin: float,
    vmax: float,
    value_label: str,
) -> dict[str, Any]:
    """热力图最终的 option 字典（轴、visualMap、series）。"""
    # 矩阵型图表跳过任何刻度都会误导阅读，类目数可控时强制逐个显示
    axis_interval: Any = 0 if max(len(x_names), len(y_names)) <= 25 else "auto"
    return {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            "subtext": "相关性矩阵"
            if is_correlation
            else f"{_build_axis_label(x)} × {_build_axis_label(y)}",
        },
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item", "formatter": tooltip_formatter},
        "grid": {**_ECHARTS_BASE_GRID, "left": 100, "bottom": 100},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "xAxis": [
            {
                "type": "category",
                "data": x_names,
                "splitArea": {"show": True},
                "axisTick": {"show": False},
                "axisLabel": {
                    "color": _ECHARTS_TEXT_SECONDARY,
                    "fontSize": 11,
                    "rotate": 30,
                    "interval": axis_interval,
                },
            }
        ],
        "yAxis": [
            {
                "type": "category",
                "data": y_names,
                "splitArea": {"show": True},
                "axisTick": {"show": False},
                "axisLabel": {
                    "color": _ECHARTS_TEXT_SECONDARY,
                    "fontSize": 11,
                    "interval": axis_interval,
                },
            }
        ],
        "visualMap": {
            "min": vmin,
            "max": vmax,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 16,
            "precision": 2,
            "textStyle": {"color": _ECHARTS_TEXT_SECONDARY},
            # 相关性：ColorBrewer RdBu 反转 7 段（蓝=负、红=正），多色标让中段不发灰
            "inRange": {
                "color": [
                    "#2166AC",
                    "#67A9CF",
                    "#D1E5F0",
                    "#F7F7F7",
                    "#FDDBC7",
                    "#EF8A62",
                    "#B2182B",
                ]
                if is_correlation
                else ["#EDF3F9", "#8FB3D1", "#2C5F8D"]
            },
        },
        "series": [
            {
                "name": value_label,
                "type": "heatmap",
                "data": cells,
                "label": {
                    "show": show_label,
                    "color": _ECHARTS_TEXT_COLOR,
                    "fontSize": 11,
                    "formatter": label_formatter,
                },
                "itemStyle": {"borderColor": "#ffffff", "borderWidth": 1},
                "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.3)"}},
            }
        ],
    }
