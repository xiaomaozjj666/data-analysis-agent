"""大数据散点 → 密度视图：视图构建与各图层的 series 数据（不含 option 装配）。

与 Plotly 分支共享 ``data_agent.density`` 的同一份网格、档位与裁剪实现，
保证双引擎"同一份数据 → 同一张密度图"。
"""

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
    build_density_view,
    clip_segment,
    extreme_points,
    format_number,
    sample_detail_points,
    should_use_density,
)
from data_agent.echarts_constants import (
    _DENSITY_ACCENT,
    _DENSITY_DETAIL_POINTS,
    _DENSITY_EXTREME_POINTS,
    _DENSITY_FACET_PANEL_SIZE,
    _DENSITY_NUMBER_JS,
    _DENSITY_PANEL_SIZE,
    _DENSITY_TREND_MIN_R,
    _JsFunction,
)
from data_agent.render.primitives import (
    _bin_index,
    _round_significant,
    _snap_edges,
)
from data_agent.tools._helpers import _scatter_structure


def _density_view_for(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    scale_mode: str = "auto",
) -> tuple[DensityView | None, dict[str, Any] | None]:
    """超大数据散点 → 密度视图；返回 ``(view, scale_details)``，不适用时 ``(None, None)``。

    触发条件与 Plotly 分支严格一致：scatter 图型、x/y 都是数值列、行数达到
    ``should_use_density`` 阈值。``scale_mode="auto"`` 时对极端值做"主体尺度"
    判定（与 ``charts._severe_axis_compression`` 同一函数）：少数离群点把
    坐标轴拉大后主体点云会被压成一条线，超出范围的记录不计入网格——条数
    写进解读文案，不静默丢数据。
    """
    if chart_type != "scatter" or not (x and y and x in df.columns and y in df.columns):
        return None, None
    if not should_use_density(len(df)):
        return None, None
    if not (pd.api.types.is_numeric_dtype(df[x]) and pd.api.types.is_numeric_dtype(df[y])):
        return None, None
    x_values = pd.to_numeric(df[x], errors="coerce").to_numpy(dtype=float)
    y_values = pd.to_numeric(df[y], errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(x_values) & np.isfinite(y_values)
    if int(finite.sum()) < 2:
        return None, None

    x_guard = y_guard = None
    if scale_mode != "full":
        # 延迟导入：charts 依赖 plotly，而本模块在无 plotly 的预览链路上也要能导入。
        from data_agent.tools.charts import _severe_axis_compression

        x_guard = _severe_axis_compression(x_values[finite].tolist())
        y_guard = _severe_axis_compression(y_values[finite].tolist())

    groups = df[color].tolist() if color and color in df.columns else None
    faceted = bool(groups is not None and df[color].nunique(dropna=True) > 1)
    # 分面后面板变窄，网格同步变少，保证每格在屏幕上仍接近正方形。
    view = build_density_view(
        x_values,
        y_values,
        groups=groups,
        panel_size=_DENSITY_FACET_PANEL_SIZE if faceted else _DENSITY_PANEL_SIZE,
        x_range=(x_guard["lower"], x_guard["upper"]) if x_guard else None,
        y_range=(y_guard["lower"], y_guard["upper"]) if y_guard else None,
    )
    if view is None:
        return None, None

    axis_ranges: dict[str, list[float]] = {}
    if x_guard:
        axis_ranges["x"] = [x_guard["lower"], x_guard["upper"]]
    if y_guard:
        axis_ranges["y"] = [y_guard["lower"], y_guard["upper"]]
    scale_details = {
        "scale_mode": "robust" if axis_ranges else "full",
        "extreme_points": max(
            x_guard["extreme_count"] if x_guard else 0,
            y_guard["extreme_count"] if y_guard else 0,
        ),
        "axis_ranges": axis_ranges,
    }
    return view, scale_details


def _density_panel_structure(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    color: str | None,
    panel: DensityPanel,
) -> dict[str, Any] | None:
    """某个密度面板对应原始记录的结构注记（趋势线端点、均值、皮尔逊 r）。

    与 Plotly 分支的 ``density_plotly.panel_structure`` 同口径：按面板覆盖的
    原始分组取值筛行（"其他 N 类"面板会覆盖多个取值），保证两个引擎画出的
    趋势线与标题里的 r 完全一致。
    """
    if x not in df.columns or y not in df.columns:
        return None
    subset = df
    if color and color in df.columns and panel.levels:
        subset = df[df[color].astype(str).isin(list(panel.levels))]
    pair = subset[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(pair) < 3:
        return None
    return _scatter_structure(pair[x].astype(float).tolist(), pair[y].astype(float).tolist())


def _density_panel_title(panel: DensityPanel, structure: dict[str, Any] | None) -> str:
    """面板标题：``分组 · 1,234 条（40%）· r=0.62``（弱相关不写 r）。"""
    name = panel.name if len(panel.name) <= 14 else panel.name[:13] + "…"
    text = f"{name} · {panel.grid.total:,} 条（{panel.share * 100:.0f}%）"
    r_value = structure.get("r") if structure else None
    if r_value is not None and abs(float(r_value)) >= _DENSITY_TREND_MIN_R:
        text += f" · r={float(r_value):.2f}"
    return text


def _density_cells(grid: DensityGrid) -> list[Any]:
    """密度格子 → ECharts heatmap 数据项 ``[xIndex, yIndex, 记录数, x0, x1, y0, y1]``。

    数据项自带分箱区间，悬浮提示不必再回查任何映射表（ECharts 的 heatmap
    hover 只给行列下标与值）。零记录格**完全不发数据项**（datashader 同样
    规定 0 值不参与着色）：网格背景透出来，"点云覆盖到哪"才读得出来，同时
    把 12k 格的体积压到实际非空格数量级。

    峰值格（仅当该面板确有聚集时）带 itemStyle 描边与 ``最密 N 条`` 标签：
    数据项级描边正好贴住那一格，并随 dataZoom 一起缩放——markArea 用类目
    坐标画不出来（同一个下标两角是零面积矩形，实测渲染为空）。
    """
    counts = grid.counts
    if counts.size == 0 or grid.total <= 0:
        return []
    ny, nx = counts.shape
    x_edges = _round_significant(_snap_edges(grid.x_edges))
    y_edges = _round_significant(_snap_edges(grid.y_edges))
    peak_flat = int(np.argmax(counts)) if grid.has_clusters else -1
    peak_y, peak_x = divmod(peak_flat, nx) if peak_flat >= 0 else (-1, -1)
    cells: list[Any] = []
    for row_index in range(ny):
        row = counts[row_index]
        for column in np.nonzero(row)[0]:
            x_index = int(column)
            count = int(row[x_index])
            value = [
                x_index,
                row_index,
                count,
                float(x_edges[x_index]),
                float(x_edges[x_index + 1]),
                float(y_edges[row_index]),
                float(y_edges[row_index + 1]),
            ]
            if row_index == peak_y and x_index == peak_x:
                cells.append(
                    {
                        "value": value,
                        "itemStyle": {
                            "borderColor": _DENSITY_ACCENT,
                            "borderWidth": 1.5,
                            "shadowBlur": 6,
                            "shadowColor": "rgba(225,87,89,0.55)",
                        },
                        # 标签底/字色固定（白底 + 深红字，与 Plotly 分支的注记同款），
                        # 两种主题下都清晰，因此显式给 color；暗色脚本只翻"亮色深格
                        # 白字"那一种约定（见 _ECHARTS_DARK_MODE_SCRIPT）。
                        "label": {
                            "show": True,
                            "position": "top",
                            "distance": 4,
                            "fontSize": 10,
                            "fontWeight": 600,
                            "color": "#B23A3C",
                            "backgroundColor": "rgba(255,255,255,0.86)",
                            "borderColor": _DENSITY_ACCENT,
                            "borderWidth": 1,
                            "borderRadius": 3,
                            "padding": [2, 5],
                            "formatter": f"最密 {format_number(count)} 条",
                        },
                    }
                )
            else:
                cells.append(value)
    return cells


def _density_trend_series(
    grid: DensityGrid,
    structure: dict[str, Any] | None,
    *,
    axis_index: int,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    min_r: float | None = None,
) -> dict[str, Any] | None:
    """面板趋势线：逐格给出趋势线的 y 位置（类目轴只接受整数下标）。

    ECharts 的类目轴会把小数坐标取整，所以不能用两个端点画斜线，而是按每
    一列的预测值取最近的一格连成折线——纵向误差恒在半格（约 3px）以内，
    视觉上与 Plotly 的直线一致。

    **必须先裁剪到可视范围**（``clip_segment``，与 Plotly 分支同一实现）：
    趋势端点按面板数据的 min/max 算，而坐标轴用的是共享的"主体尺度"范围，
    不裁剪的话超出画布的那一段会被类目下标夹到最上一格，在面板顶部横着铺
    一整条线，读起来像"平趋势"（实测截图可见）。裁剪后完全落在范围外时
    返回 ``None``（不画线），与 Plotly 分支"skip the line"一致。

    ``min_r`` 给分面用：弱相关（|r| < 0.25）的斜线没有解读价值；整体布局
    不设门槛（与 Plotly 的 ``_add_structure`` 一致，只要拟合出趋势就画）。
    """
    if not structure:
        return None
    trend = structure.get("trend")
    if not trend:
        return None
    r_value = structure.get("r")
    if min_r is not None and (r_value is None or abs(float(r_value)) < min_r):
        return None
    clipped = clip_segment(trend, x_range=x_range, y_range=y_range)
    if clipped is None:
        return None
    x0, y0, x1, y1 = (float(value) for value in clipped)
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)) or x1 <= x0:
        return None
    slope = (y1 - y0) / (x1 - x0)
    edges_x, edges_y = grid.x_edges, grid.y_edges
    nx = grid.counts.shape[1]
    data: list[int | None] = []
    for index in range(nx):
        center = (float(edges_x[index]) + float(edges_x[index + 1])) / 2.0
        if center < x0 or center > x1:
            data.append(None)
            continue
        data.append(_bin_index(y0 + slope * (center - x0), edges_y, nearest=True))
    if sum(item is not None for item in data) < 2:
        # 裁剪后只剩不到两格（极陡的线）：折线画不出来，不如不画
        return None
    return {
        "name": "趋势线",
        "type": "line",
        "xAxisIndex": axis_index,
        "yAxisIndex": axis_index,
        "data": data,
        "showSymbol": False,
        "symbol": "none",
        "connectNulls": False,
        "silent": True,
        "legendHoverLink": False,
        "z": 6,
        "lineStyle": {"color": _DENSITY_ACCENT, "width": 2.5, "opacity": 0.95},
        "tooltip": {"show": False},
    }


def _density_extreme_series(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    color: str | None,
    panel: DensityPanel,
    view: DensityView,
    axis_index: int,
    x_label: str,
    y_label: str,
) -> dict[str, Any] | None:
    """最外围原始记录（原始点叠加层）。

    密度视图按定义丢掉了单点身份（datashader 的 inspection reductions 正是
    为此存在）：把最外围的少数真实记录以原始点叠加回来，既保住"个别极端
    记录长什么样"，又不会重新把画面糊掉。数据项 ``[xIndex, yIndex, 原始x,
    原始y]``——前两位定位到所属格子的中心（类目轴只能整数定位），后两位让
    hover 显示真实数值而不是格子中心。
    """
    if x not in df.columns or y not in df.columns:
        return None
    subset = df
    if color and color in df.columns and panel.levels:
        subset = df[df[color].astype(str).isin(list(panel.levels))]
    pair = subset[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(pair) < 10:
        return None
    raw_x, raw_y = extreme_points(
        pair[x].to_numpy(dtype=float),
        pair[y].to_numpy(dtype=float),
        x_range=view.x_range,
        y_range=view.y_range,
        top=_DENSITY_EXTREME_POINTS,
    )
    if len(raw_x) == 0:
        return None
    grid = panel.grid
    rounded_x = _round_significant(raw_x)
    rounded_y = _round_significant(raw_y)
    data = [
        [
            _bin_index(float(x_value), grid.x_edges),
            _bin_index(float(y_value), grid.y_edges),
            float(x_value),
            float(y_value),
        ]
        for x_value, y_value in zip(rounded_x, rounded_y, strict=True)
    ]
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    y_label_js = json.dumps(y_label, ensure_ascii=False)
    return {
        "name": "最外围记录",
        "type": "scatter",
        "xAxisIndex": axis_index,
        "yAxisIndex": axis_index,
        "data": data,
        "symbolSize": 5,
        "z": 8,
        "itemStyle": {
            "color": "rgba(225,87,89,0.85)",
            "borderColor": "rgba(255,255,255,0.9)",
            "borderWidth": 0.8,
        },
        "emphasis": {"scale": 1.6, "itemStyle": {"opacity": 1, "shadowBlur": 8}},
        "tooltip": {
            "formatter": _JsFunction(
                "function(p){" + _DENSITY_NUMBER_JS + "var v=p.value;"
                "return '<div style=\"font-weight:600;margin-bottom:6px;\">最外围记录（原始点）</div>'"
                f"+'<span style=\"color:var(--tt-muted,#6b7280);\">'+{x_label_js}+'：</span><b>'+_dn(v[2])+'</b><br/>'"
                f"+'<span style=\"color:var(--tt-muted,#6b7280);\">'+{y_label_js}+'：</span><b>'+_dn(v[3])+'</b><br/>'"
                "+'<span style=\"color:var(--tt-muted,#6b7280);font-size:11px;\">红点定位在所属网格中心，数值为原始记录</span>';"
                "}"
            )
        },
    }


def _density_detail_series(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    color: str | None,
    view: DensityView,
    x_label: str,
    y_label: str,
) -> dict[str, Any] | None:
    """密度图的"放大看原始记录"图层（与 Plotly 分支同名同义）。

    ECharts 没有图内按钮，改用**原生图例开关**：series 进 legend.data，
    并在 legend.selected 里默认设为 false —— 用户点图例即显示/隐藏，
    不依赖任何自定义 JS（注入脚本与单文件下载都照常可用）。

    与 Plotly 一样只给单面板布局加：分面图上一整层点会落到错误面板。
    """
    pair = df[[x, y]].apply(pd.to_numeric, errors="coerce")
    groups = df[color].tolist() if color and color in df.columns else None
    xs, ys = sample_detail_points(
        pair.iloc[:, 0].to_numpy(dtype=float),
        pair.iloc[:, 1].to_numpy(dtype=float),
        x_range=view.x_range,
        y_range=view.y_range,
        groups=groups,
        top=_DENSITY_DETAIL_POINTS,
    )
    if xs.size == 0:
        return None
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    y_label_js = json.dumps(y_label, ensure_ascii=False)
    data = [
        [float(xv), float(yv)]
        for xv, yv in zip(_round_significant(xs), _round_significant(ys), strict=True)
    ]
    return {
        "name": f"抽样原始点（{format_number(xs.size)} 条）",
        "type": "scatter",
        "xAxisIndex": 0,
        "yAxisIndex": 0,
        "data": data,
        "symbolSize": 4,
        "z": 6,
        "itemStyle": {"color": "rgba(74,111,165,0.55)"},
        "emphasis": {"scale": 1.8, "itemStyle": {"opacity": 1, "shadowBlur": 8}},
        "tooltip": {
            "formatter": _JsFunction(
                "function(p){" + _DENSITY_NUMBER_JS + "var v=p.value;"
                "return '<div style=\"font-weight:600;margin-bottom:6px;\">抽样原始点</div>'"
                f"+'<span style=\"color:var(--tt-muted,#6b7280);\">'+{x_label_js}+'：</span><b>'+_dn(v[0])+'</b><br/>'"
                f"+'<span style=\"color:var(--tt-muted,#6b7280);\">'+{y_label_js}+'：</span><b>'+_dn(v[1])+'</b><br/>'"
                "+'<span style=\"color:var(--tt-muted,#6b7280);font-size:11px;\">分层抽样，用于放大后查看具体记录</span>';"
                "}"
            )
        },
    }
