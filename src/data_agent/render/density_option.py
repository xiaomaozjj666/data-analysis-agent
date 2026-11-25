"""密度视图的 ECharts option 装配：坐标轴/网格布局、档位色标、各图层 series。"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd

from data_agent.density import (
    DensityGrid,
    DensityPanel,
    DensityView,
    band_label,
    band_legend,
)
from data_agent.echarts_constants import (
    _DENSITY_ACCENT,
    _DENSITY_FACET_LAYOUT,
    _DENSITY_NUMBER_JS,
    _DENSITY_SINGLE_LAYOUT,
    _DENSITY_TREND_MIN_R,
    _ECHARTS_BASE_AXIS,
    _ECHARTS_BASE_LEGEND,
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLBOX,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_GRID_COLOR,
    _ECHARTS_PALETTE,
    _ECHARTS_TEXT_SECONDARY,
    _JsFunction,
)
from data_agent.render.density_view import (
    _density_cells,
    _density_detail_series,
    _density_extreme_series,
    _density_panel_structure,
    _density_panel_title,
    _density_trend_series,
)
from data_agent.render.primitives import (
    _axis_label_interval,
    _bin_index,
    _bin_labels,
    _format_number,
    _series_color,
    _within_range,
)
from data_agent.tools._helpers import _human_column_label as _build_axis_label


def _density_category_axis(
    edges: np.ndarray,
    *,
    grid_index: int,
    show_labels: bool,
    interval: int = 0,
    name: str | None = None,
    name_gap: float | None = None,
) -> dict[str, Any]:
    """密度图类目轴：类目就是分箱（边界格式化成短标签）。

    ECharts 的 heatmap 要求两个维度都是类目轴（否则报 "Rectangular coordinate
    must have two categories to use it"），所以"连续的数值轴"这一步被搬到了
    Python 侧。刻度只在面板外圈显示（共享轴的内圈重复刻度是噪声），inner
    面板留空防拥挤。
    """
    axis: dict[str, Any] = {
        "type": "category",
        "gridIndex": grid_index,
        "data": _bin_labels(edges),
        "boundaryGap": True,
        "axisTick": {"show": False},
        "axisLine": {"show": show_labels, "lineStyle": {"color": _ECHARTS_GRID_COLOR}},
        "splitLine": {"show": False},
        "axisLabel": {
            "show": show_labels,
            "interval": interval,
            "hideOverlap": True,
            "color": _ECHARTS_TEXT_SECONDARY,
            "fontSize": 11,
        },
    }
    if name:
        axis["name"] = name
        axis["nameLocation"] = "middle"
        axis["nameTextStyle"] = dict(_ECHARTS_BASE_AXIS["nameTextStyle"])
        if name_gap is not None:
            axis["nameGap"] = name_gap
    return axis


def _density_marginal_value_axis(grid_index: int) -> dict[str, Any]:
    """边缘直方图的数值轴：只负责柱长，刻度/网格/轴线全部隐藏。

    与 Plotly 分支的收尾样式一致（边缘面板去掉刻度与网格线）：柱长与主面板
    对齐才是信息，刻度数值是噪声。
    """
    return {
        "type": "value",
        "gridIndex": grid_index,
        "min": 0,
        "scale": False,
        "axisLine": {"show": False},
        "axisTick": {"show": False},
        "axisLabel": {"show": False},
        "splitLine": {"show": False},
    }


def _density_marginal_series(
    grid: DensityGrid,
    *,
    bar_color: str,
    x_label: str,
    y_label: str,
) -> list[dict[str, Any]]:
    """单面板的边缘分布（顶部 x 直方图 + 右侧 y 直方图）。

    分箱与主面板完全一致（直接复用网格的行列求和），因此每根柱子与主面板的
    每一列/行严格对齐——用户能把"分布形状"与"密度图"对上，这是 jointplot
    布局最核心的价值。柱色取 ``_CHART_COLORS[4]``，与 Plotly 分支同色。
    """

    def formatter(label: str) -> _JsFunction:
        # 列名来自 CSV 表头，用 json.dumps 转义后作为 JS 字符串字面量拼入，
        # 防止列名里的引号/尖括号破坏函数体（与 _scatter_tooltip_formatter 一致）。
        label_js = json.dumps(label, ensure_ascii=False)
        return _JsFunction(
            "function(p){"
            + _DENSITY_NUMBER_JS
            + "return '<div style=\"font-weight:600;margin-bottom:6px;\">'+p.seriesName+'</div>'"
            "+'<span style=\"color:var(--tt-muted,#6b7280);\">'+"
            + label_js
            + "+'：</span><b>'+_dn(p.name)+'</b><br/>'"
            "+'<span style=\"color:var(--tt-muted,#6b7280);\">记录数：</span><b>'+Number(p.value).toLocaleString()+'</b>';"
            "}"
        )

    x_marginal, y_marginal = grid.marginal_x(), grid.marginal_y()
    return [
        {
            "name": "x 分布",
            "type": "bar",
            "xAxisIndex": 1,
            "yAxisIndex": 1,
            "data": [int(value) for value in x_marginal],
            # 类目轴 band 即分箱：柱宽 100% + 类目间隙 0 才能与热力图逐格对齐
            "barWidth": "100%",
            "barCategoryGap": "0%",
            "z": 3,
            "itemStyle": {"color": bar_color, "opacity": 0.9},
            "tooltip": {"formatter": formatter(x_label)},
        },
        {
            "name": "y 分布",
            "type": "bar",
            "xAxisIndex": 2,
            "yAxisIndex": 2,
            "data": [int(value) for value in y_marginal],
            "barWidth": "100%",
            "barCategoryGap": "0%",
            "z": 3,
            "itemStyle": {"color": bar_color, "opacity": 0.9},
            "tooltip": {"formatter": formatter(y_label)},
        },
    ]


def _density_tooltip_formatter(x_label: str, y_label: str) -> _JsFunction:
    """密度格子的悬浮提示：分箱区间 + 记录数（数据项自带边界，无需回查）。"""
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    y_label_js = json.dumps(y_label, ensure_ascii=False)
    return _JsFunction(
        "function(p){" + _DENSITY_NUMBER_JS + "var v=p.value;if(!v||v.length<7)return '';"
        "return '<div style=\"font-weight:600;margin-bottom:6px;\">'+p.seriesName+'</div>'"
        f"+'<span style=\"color:var(--tt-muted,#6b7280);\">'+{x_label_js}+'：</span><b>'+_dn(v[3])+' ~ '+_dn(v[4])+'</b><br/>'"
        f"+'<span style=\"color:var(--tt-muted,#6b7280);\">'+{y_label_js}+'：</span><b>'+_dn(v[5])+' ~ '+_dn(v[6])+'</b><br/>'"
        "+'<span style=\"color:var(--tt-muted,#6b7280);\">记录数：</span><b>'+Number(v[2]).toLocaleString()+'</b>';"
        "}"
    )


def _density_visual_map(
    bands: list[tuple[int, int | None]],
    colors: list[str],
    series_indexes: list[int],
) -> dict[str, Any]:
    """档位色标（piecewise）：一个 visualMap 就是全部面板共用的颜色键。

    为什么用分档（classed）而不是连续渐变：记录数天然跨数量级（密集格上千、
    边缘格只有一两条），线性映射会把非密集区全压成一个颜色。图例直接标出每
    档的记录数区间，用户不需要理解"对数色标"就能读懂深浅。
    ``selectedMode: False``：这是颜色键不是筛选器，点某档不应把其它档的格子
    藏起来（与 Plotly 的 colorbar 行为一致）。
    """
    pieces: list[dict[str, Any]] = []
    for index, band in enumerate(bands):
        low, high = band
        piece: dict[str, Any] = {"label": band_label(band), "color": colors[index]}
        if high is None:
            piece["min"] = low
        elif high <= low:
            piece["value"] = low
        else:
            piece["min"] = low
            piece["max"] = high
        pieces.append(piece)
    return {
        "type": "piecewise",
        "dimension": 2,
        "seriesIndex": series_indexes,
        "pieces": pieces,
        "orient": "horizontal",
        "left": "center",
        "bottom": 8,
        "itemWidth": 14,
        "itemHeight": 12,
        "itemGap": 8,
        "selectedMode": False,
        "textStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 11},
    }


def _density_panel_series(
    df,
    *,
    x: str,
    y: str,
    color: str | None,
    panel: DensityPanel,
    structure: dict[str, Any] | None,
    view: DensityView,
    grid_index: int,
    trend_min_r: float | None,
    x_label: str,
    y_label: str,
) -> tuple[list[dict[str, Any]], bool]:
    """一个密度面板 = 热力图 + （可选）趋势线 + （可选）最外围记录。

    ``trend_min_r`` 为 ``None`` 时不设相关系数门槛（整体布局），给数值时
    只画 |r| 达标的趋势线（分面）。返回 ``(series, 是否含最外围记录)``，
    series 顺序与拆分前逐项一致（热力图 → 趋势线 → 最外围记录）。
    """
    items: list[dict[str, Any]] = [
        {
            "name": panel.name or f"{y_label}密度",
            "type": "heatmap",
            "xAxisIndex": grid_index,
            "yAxisIndex": grid_index,
            "data": _density_cells(panel.grid),
            # 无描边：格子紧贴铺满，零记录格自然透出背景（点云覆盖范围才是信息）
            "itemStyle": {"borderWidth": 0},
            "label": {"show": False},
            "emphasis": {
                "itemStyle": {
                    "borderColor": _DENSITY_ACCENT,
                    "borderWidth": 1.5,
                    "shadowBlur": 8,
                    "shadowColor": "rgba(0,0,0,0.25)",
                }
            },
        }
    ]
    trend_series = _density_trend_series(
        panel.grid,
        structure,
        axis_index=grid_index,
        x_range=view.x_range,
        y_range=view.y_range,
        min_r=trend_min_r,
    )
    if trend_series is not None:
        items.append(trend_series)
    extreme_series = _density_extreme_series(
        df,
        x=x,
        y=y,
        color=color,
        panel=panel,
        view=view,
        axis_index=grid_index,
        x_label=x_label,
        y_label=y_label,
    )
    has_extremes = extreme_series is not None
    if extreme_series is not None:
        items.append(extreme_series)
    return items, has_extremes


def _density_facet_axes(
    panels: list[DensityPanel],
    view: DensityView,
    structures: list[dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """分面布局：返回 ``(grids, x_axes, y_axes, panel_titles)``。"""
    count = len(panels)
    cols = 2 if count in (2, 4) else 3
    rows = int(math.ceil(count / cols))
    layout = _DENSITY_FACET_LAYOUT
    usable_w = 100.0 - layout["left"] - layout["right"]
    usable_h = 100.0 - layout["top"] - layout["bottom"]
    cell_w = (usable_w - layout["gap_x"] * (cols - 1)) / cols
    block_h = (usable_h - layout["gap_y"] * (rows - 1)) / rows
    panel_h = block_h - layout["title"]
    x_interval = _axis_label_interval(view.bin_shape[0], 6)
    y_interval = _axis_label_interval(view.bin_shape[1], 5)
    grids: list[dict[str, Any]] = []
    x_axes: list[dict[str, Any]] = []
    y_axes: list[dict[str, Any]] = []
    panel_titles: list[dict[str, Any]] = []
    for index, panel in enumerate(panels):
        row, col = divmod(index, cols)
        left = layout["left"] + col * (cell_w + layout["gap_x"])
        top = layout["top"] + row * (block_h + layout["gap_y"])
        grids.append(
            {
                "left": f"{left:.2f}%",
                "top": f"{top + layout['title']:.2f}%",
                "width": f"{cell_w:.2f}%",
                "height": f"{panel_h:.2f}%",
                "containLabel": False,
            }
        )
        # 共享轴：只有最下一行写 x 刻度、最左一列写 y 刻度，内圈重复刻度是噪声；
        # 轴名改由副标题统一交代（分面格子小，轴名会挤掉数据）。
        x_axes.append(
            _density_category_axis(
                panel.grid.x_edges,
                grid_index=index,
                show_labels=row == rows - 1,
                interval=x_interval,
            )
        )
        y_axes.append(
            _density_category_axis(
                panel.grid.y_edges,
                grid_index=index,
                show_labels=col == 0,
                interval=y_interval,
            )
        )
        panel_titles.append(
            {
                "text": _density_panel_title(panel, structures[index]),
                "left": f"{left:.2f}%",
                "top": f"{top:.2f}%",
                "textStyle": {"fontSize": 12, "fontWeight": 600, "color": "#245C55"},
            }
        )
    return grids, x_axes, y_axes, panel_titles


def _density_single_axes(
    panel: DensityPanel,
    view: DensityView,
    *,
    x_label: str,
    y_label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """单面板 jointplot 布局：返回 ``(grids, x_axes, y_axes)``。"""
    layout = _DENSITY_SINGLE_LAYOUT
    main_left = layout["left"]
    main_width = (
        100.0 - layout["left"] - layout["right"] - layout["hist_width"] - layout["hist_gap"]
    )
    main_top = layout["top"] + layout["hist"] + layout["hist_gap"]
    main_height = 100.0 - main_top - layout["bottom"]
    hist_left = main_left + main_width + layout["hist_gap"]
    grids: list[dict[str, Any]] = [
        {
            "left": f"{main_left:.2f}%",
            "top": f"{main_top:.2f}%",
            "width": f"{main_width:.2f}%",
            "height": f"{main_height:.2f}%",
            "containLabel": False,
        },
        {
            "left": f"{main_left:.2f}%",
            "top": f"{layout['top']:.2f}%",
            "width": f"{main_width:.2f}%",
            "height": f"{layout['hist']:.2f}%",
            "containLabel": False,
        },
        {
            "left": f"{hist_left:.2f}%",
            "top": f"{main_top:.2f}%",
            "width": f"{layout['hist_width']:.2f}%",
            "height": f"{main_height:.2f}%",
            "containLabel": False,
        },
    ]
    x_axes: list[dict[str, Any]] = [
        _density_category_axis(
            panel.grid.x_edges,
            grid_index=0,
            show_labels=True,
            interval=_axis_label_interval(view.bin_shape[0], 14),
            name=x_label,
            name_gap=34,
        )
    ]
    y_axes: list[dict[str, Any]] = [
        _density_category_axis(
            panel.grid.y_edges,
            grid_index=0,
            show_labels=True,
            interval=_axis_label_interval(view.bin_shape[1], 8),
            name=y_label,
            name_gap=54,
        )
    ]
    x_axes.append(
        _density_category_axis(
            panel.grid.x_edges,
            grid_index=1,
            show_labels=False,
        )
    )
    y_axes.append(_density_marginal_value_axis(1))
    x_axes.append(_density_marginal_value_axis(2))
    y_axes.append(
        _density_category_axis(
            panel.grid.y_edges,
            grid_index=2,
            show_labels=False,
        )
    )
    return grids, x_axes, y_axes


def _density_mean_markline(
    panel: DensityPanel,
    structure: dict[str, Any] | None,
    view: DensityView,
) -> dict[str, Any] | None:
    """整体布局的均值参考线 markLine（坐标换算成类目下标）。

    # 整体布局保留均值参考线（分面格子太小，画进去只会变成噪声）。
    # 坐标同样要换算成类目下标：markLine 的 xAxis/yAxis 在类目轴上按
    # "类目下标"解释，直接给原始数值会落到轴外而不显示。均值落在可视
    # 范围外时直接不画——类目下标会被夹到边界格上，反而画出一条位置
    # 错误的线（Plotly 的 add_vline/add_hline 在范围外同样不可见）。
    """
    mark_items: list[dict[str, Any]] = []
    mean_x = structure.get("mean_x") if structure else None
    mean_y = structure.get("mean_y") if structure else None
    if mean_x is not None and _within_range(mean_x, view.x_range):
        mark_items.append(
            {
                "xAxis": _bin_index(float(mean_x), panel.grid.x_edges, nearest=True),
                "lineStyle": {
                    "color": "#9aa0a6",
                    "width": 1.2,
                    "type": "dashed",
                    "opacity": 0.9,
                },
                "label": {
                    "formatter": f"x 均值 {_format_number(mean_x)}",
                    "position": "insideEndTop",
                    "color": "#9aa0a6",
                    "fontSize": 10,
                },
            }
        )
    if mean_y is not None and _within_range(mean_y, view.y_range):
        mark_items.append(
            {
                "yAxis": _bin_index(float(mean_y), panel.grid.y_edges, nearest=True),
                "lineStyle": {
                    "color": "#9aa0a6",
                    "width": 1.2,
                    "type": "dashed",
                    "opacity": 0.9,
                },
                "label": {
                    "formatter": f"y 均值 {_format_number(mean_y)}",
                    "position": "insideEndRight",
                    "color": "#9aa0a6",
                    "fontSize": 10,
                },
            }
        )
    if not mark_items:
        return None
    return {
        "silent": True,
        "symbol": "none",
        "label": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 10},
        "data": mark_items,
    }


def _density_legend(
    df,
    *,
    x: str,
    y: str,
    color: str | None,
    view: DensityView,
    faceted: bool,
    has_extremes: bool,
    x_label: str,
    y_label: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """图例配置 + 单面板的「抽样原始点」细节图层（默认隐藏，靠图例开关）。"""
    legend: dict[str, Any] = {**_ECHARTS_BASE_LEGEND, "top": "13.5%", "right": 24}
    if has_extremes:
        legend["data"] = ["最外围记录"]
    detail_series: dict[str, Any] | None = None
    if not faceted:
        # 细节图层只在单面板布局出现（分面图一层点会落到错误面板）；
        # 默认隐藏，靠图例开关，与 Plotly 的图内按钮同一语义。
        detail_series = _density_detail_series(
            df,
            x=x,
            y=y,
            color=color,
            view=view,
            x_label=x_label,
            y_label=y_label,
        )
        if detail_series is not None:
            legend["data"] = (legend.get("data") or []) + [detail_series["name"]]
            legend["selected"] = {detail_series["name"]: False}

    return legend, detail_series


def _density_option_envelope(
    *,
    titles: Any,
    faceted: bool,
    x_label: str,
    y_label: str,
    legend: dict[str, Any],
    grids: list[dict[str, Any]],
    x_axes: list[dict[str, Any]],
    y_axes: list[dict[str, Any]],
    bands: list[tuple[int, int | None]],
    view: DensityView,
    heatmap_indexes: list[int],
    series: list[dict[str, Any]],
    data_zoom: list[dict[str, Any]],
    light_bands: list[list[Any]],
    dark_bands: list[list[Any]],
) -> dict[str, Any]:
    """密度图的最终 option 字典（标题、图例、坐标轴、色标、series）。"""
    option: dict[str, Any] = {
        "title": titles if faceted else titles[0],
        "tooltip": {
            **_ECHARTS_BASE_TOOLTIP,
            "trigger": "item",
            "formatter": _density_tooltip_formatter(x_label, y_label),
        },
        "legend": legend,
        "grid": grids,
        "xAxis": x_axes,
        "yAxis": y_axes,
        # 一个 visualMap 就是所有面板共用的颜色键（分面共享同一套档位才能横向比较）
        "visualMap": _density_visual_map(bands, view.band_colors(), heatmap_indexes),
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        # 网格图不需要入场动画：dataZoom 重排一万多个格子时动画只会带来闪烁
        "animation": False,
        "series": series,
        "dataZoom": data_zoom,
        # 顶层非标准键：亮/暗两套档位色板随图带过去，供注入的换肤脚本与前端
        # 缩略图整组切换（同一颜色在两种主题下必须代表同一档记录数）。
        "densityBands": {"light": light_bands, "dark": dark_bands},
    }
    if not faceted:
        # 主面板与边缘直方图的分箱一一对应，联动十字线让"这一列的分布"对上"哪一根柱子"
        option["axisPointer"] = {
            "link": [{"xAxisIndex": [0, 1]}, {"yAxisIndex": [0, 2]}],
            "label": {"backgroundColor": _ECHARTS_TEXT_SECONDARY},
        }
    return option


def _density_layout_and_series(
    df,
    *,
    x: str,
    y: str,
    color: str | None,
    view: DensityView,
    x_label: str,
    y_label: str,
    color_label: str | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[int],
    bool,
    str,
]:
    """按布局（单面板 / 分面）生成坐标轴与 series，返回副标题所需的全部中间结果。"""
    panels = list(view.panels)
    faceted = view.is_faceted
    structures = [
        _density_panel_structure(df, x=x, y=y, color=color, panel=panel) for panel in panels
    ]

    series: list[dict[str, Any]] = []
    heatmap_indexes: list[int] = []
    has_extremes = False
    if faceted:
        grids, x_axes, y_axes, panel_titles = _density_facet_axes(panels, view, structures)
        for index, panel in enumerate(panels):
            heatmap_indexes.append(len(series))
            items, extreme = _density_panel_series(
                df,
                x=x,
                y=y,
                color=color,
                panel=panel,
                structure=structures[index],
                view=view,
                grid_index=index,
                trend_min_r=_DENSITY_TREND_MIN_R,
                x_label=x_label,
                y_label=y_label,
            )
            series.extend(items)
            has_extremes = has_extremes or extreme
        # 分面副标题：按分组拆成 N 个面板
        subtext = (
            f"{x_label} × {y_label} · 按{color_label or '分组'}拆成 {len(panels)} 个密度面板"
            f" · {view.bin_label} 网格"
        )
    else:
        panel = panels[0]
        structure = structures[0]
        grids, x_axes, y_axes = _density_single_axes(panel, view, x_label=x_label, y_label=y_label)
        panel_titles = []
        heatmap_indexes.append(len(series))
        items, extreme = _density_panel_series(
            df,
            x=x,
            y=y,
            color=color,
            panel=panel,
            structure=structure,
            view=view,
            grid_index=0,
            trend_min_r=None,
            x_label=x_label,
            y_label=y_label,
        )
        series.extend(items)
        has_extremes = has_extremes or extreme
        mark_line = _density_mean_markline(panel, structure, view)
        if mark_line is not None:
            series[heatmap_indexes[0]]["markLine"] = mark_line
        series.extend(
            _density_marginal_series(
                panel.grid,
                bar_color=_series_color(4),
                x_label=x_label,
                y_label=y_label,
            )
        )
        only = f" · {panels[0].name}" if panels[0].name else ""
        subtext = f"{x_label} × {y_label} · {view.bin_label} 密度网格 · {view.total:,} 条记录{only}"

    return (
        grids,
        x_axes,
        y_axes,
        panel_titles,
        series,
        heatmap_indexes,
        has_extremes,
        subtext,
    )


def _echarts_scatter_density(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    color: str | None,
    title: str,
    view: DensityView,
) -> dict[str, Any]:
    """超大数据散点 → 分档密度热力图（与 Plotly 分支同一份网格与档位色板）。

    两种布局（与 Plotly 分支一致）：
    - 无颜色分组：主密度面板 + 顶部/右侧边缘直方图（jointplot 布局），叠加
      整体趋势线、均值参考线与最外围原始记录；
    - 有颜色分组（≤6 类）：每类一个密度面板、共享同一套档位色标，面板标题
      给出记录数、占比与相关系数（颜色只表达密度，类别用分面表达）。

    ECharts 特有的三处取舍（都源于"heatmap 只认类目轴"）：
    1. 连续数值先离散成格子，轴刻度只是"读到大概位置"，精确区间在 tooltip；
    2. 按数值定位的结构锚点（峰值框除外）先映射成格子下标，单点精度半格；
    3. 峰值框用数据项级 itemStyle 描边（贴住那一格、随缩放移动），
       markArea 的类目坐标两角同下标会渲染成零面积。
    """
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y)
    color_label = _build_axis_label(color) if color else None
    panels = list(view.panels)
    bands = view.bands
    light_bands = band_legend(bands, view.band_colors())
    dark_bands = band_legend(bands, view.band_colors(dark=True))
    faceted = view.is_faceted
    (
        grids,
        x_axes,
        y_axes,
        panel_titles,
        series,
        heatmap_indexes,
        has_extremes,
        subtext,
    ) = _density_layout_and_series(
        df,
        x=x,
        y=y,
        color=color,
        view=view,
        x_label=x_label,
        y_label=y_label,
        color_label=color_label,
    )

    titles: Any = [{**_ECHARTS_BASE_TITLE, "text": title, "subtext": subtext}]
    if faceted:
        titles.extend(panel_titles)

    legend, detail_series = _density_legend(
        df,
        x=x,
        y=y,
        color=color,
        view=view,
        faceted=faceted,
        has_extremes=has_extremes,
        x_label=x_label,
        y_label=y_label,
    )
    if detail_series is not None:
        series.append(detail_series)

    data_zoom: list[dict[str, Any]] = []
    if faceted:
        indexes = list(range(len(panels)))
        data_zoom.append({"type": "inside", "xAxisIndex": indexes, "filterMode": "none"})
        data_zoom.append({"type": "inside", "yAxisIndex": indexes, "filterMode": "none"})
    else:
        data_zoom.append({"type": "inside", "xAxisIndex": [0, 1], "filterMode": "none"})
        data_zoom.append({"type": "inside", "yAxisIndex": [0, 2], "filterMode": "none"})

    return _density_option_envelope(
        titles=titles,
        faceted=faceted,
        x_label=x_label,
        y_label=y_label,
        legend=legend,
        grids=grids,
        x_axes=x_axes,
        y_axes=y_axes,
        bands=bands,
        view=view,
        heatmap_indexes=heatmap_indexes,
        series=series,
        data_zoom=data_zoom,
        light_bands=light_bands,
        dark_bands=dark_bands,
    )
