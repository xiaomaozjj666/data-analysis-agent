"""散点图 option 构建：分组/大小维度、IQR 离群点、结构注记（趋势线 + 均值线）。"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_BASE_GRID,
    _ECHARTS_BASE_LEGEND,
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLBOX,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_PALETTE,
    _JsFunction,
)
from data_agent.render.primitives import (
    _echarts_value_axis,
    _format_number,
    _hex_to_rgba,
    _rows_data,
    _series_color,
)
from data_agent.tools._helpers import _human_column_label as _build_axis_label
from data_agent.tools._helpers import _scatter_structure


def _append_structure_annotations(
    base: dict[str, Any],
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    target: dict[str, Any],
) -> None:
    """给散点主系列追加结构注记：OLS 趋势线 + 双轴均值象限线。

    大点云没有视觉锚点就是一堵"点墙"（实测用户反馈眼花缭乱）：趋势线
    给出整体方向，均值参考线把画面切成四象限，r 值量化关系强度——
    Tableau / 专业报表的标配做法。markLine 坐标基于全量数据计算（比
    HTML 嵌入的抽样点更精确），与既有 IQR 边界线合并而非覆盖。
    """
    if not (x and y and x in df.columns and y in df.columns):
        return
    pair = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if pair.empty:
        return
    structure = _scatter_structure(pair[x].astype(float).tolist(), pair[y].astype(float).tolist())
    mark_items: list[dict[str, Any]] = []
    trend = structure.get("trend")
    r_value = structure.get("r")
    if trend:
        x0, y0, x1, y1 = trend
        trend_label = f"趋势 r={r_value:.2f}" if r_value is not None else "趋势线"
        mark_items.append(
            [
                {
                    "coord": [x0, y0],
                    "lineStyle": {"color": "#E15759", "width": 2.5},
                    "label": {
                        "formatter": trend_label,
                        "position": "insideEndTop",
                        "color": "#E15759",
                        "fontSize": 11,
                        "fontWeight": 600,
                    },
                },
                {"coord": [x1, y1]},
            ]
        )
    mean_x, mean_y = structure.get("mean_x"), structure.get("mean_y")
    if mean_x is not None:
        mark_items.append(
            {
                "xAxis": mean_x,
                "lineStyle": {"color": "#9aa0a6", "width": 1.2, "type": "dashed", "opacity": 0.9},
                # 竖线标签默认沿轴贴在最上端并旋转 90°，实测压在绘图区顶边、
                # 与标题抢位且半截被裁。改成水平放在竖线下端上方（insideStartTop），
                # 加白底衬垫，既读得清也不越界。
                "label": {
                    "formatter": f"x 均值 {_format_number(mean_x)}",
                    "position": "insideStartTop",
                    "rotate": 0,
                    "color": "#6b7280",
                    "fontSize": 10,
                    "padding": [2, 4],
                    "borderRadius": 3,
                    "backgroundColor": "rgba(255,255,255,0.82)",
                },
            }
        )
    if mean_y is not None:
        mark_items.append(
            {
                "yAxis": mean_y,
                "lineStyle": {"color": "#9aa0a6", "width": 1.2, "type": "dashed", "opacity": 0.9},
                # 横线标签放在线**左端内侧**（insideStartTop 会贴到绘图区左边界被裁），
                # 白底衬垫保证压在点数上时也能读。
                "label": {
                    "formatter": f"y 均值 {_format_number(mean_y)}",
                    # 右对齐锚在线的右端内侧：左对齐会从锚点向右延伸、越过绘图区被裁
                    "position": "insideEndTop",
                    "align": "right",
                    "color": "#6b7280",
                    "fontSize": 10,
                    "padding": [2, 4],
                    "borderRadius": 3,
                    "backgroundColor": "rgba(255,255,255,0.82)",
                },
            }
        )
    if not mark_items:
        return
    existing = target.setdefault(
        "markLine",
        {"silent": True, "symbol": "none", "label": {"color": "#6b7280", "fontSize": 11}},
    )
    existing.setdefault("data", []).extend(mark_items)


def _scatter_outlier_overlay(
    df: pd.DataFrame,
    x: str,
    y: str | None,
) -> tuple[pd.Series, list[dict[str, Any]]]:
    """IQR 离群点掩码 + 正常边界参考线（仅无颜色分组时使用）。"""
    outlier_mask = pd.Series(False, index=df.index)
    mark_data: list[dict[str, Any]] = []
    if y and pd.api.types.is_numeric_dtype(df[x]) and pd.api.types.is_numeric_dtype(df[y]):
        for col, axis_key in ((x, "xAxis"), (y, "yAxis")):
            vals = pd.to_numeric(df[col], errors="coerce")
            q1, q3 = vals.quantile(0.25), vals.quantile(0.75)
            iqr = float(q3 - q1)
            if iqr <= 0:
                continue
            lo, hi = float(q1 - 1.5 * iqr), float(q3 + 1.5 * iqr)
            col_mask = (vals < lo) | (vals > hi)
            if bool(col_mask.any()):
                outlier_mask |= col_mask
                col_label = _build_axis_label(col)
                # 标签位置按方向差异化：y 轴上界标线上方、下界标线下方，
                # x 轴（竖线）标线底端；避免量程被极端值拉大后
                # 两条边界线贴近时标签文字互相重叠。
                if bool((vals > hi).any()):
                    mark_data.append(
                        {
                            axis_key: hi,
                            "label": {
                                "formatter": f"{col_label} 正常上界 {_format_number(hi)}",
                                "position": "insideEndTop"
                                if axis_key == "yAxis"
                                else "insideStartTop",
                            },
                        }
                    )
                if bool((vals < lo).any()):
                    mark_data.append(
                        {
                            axis_key: lo,
                            "label": {
                                "formatter": f"{col_label} 正常下界 {_format_number(lo)}",
                                "position": "insideEndBottom"
                                if axis_key == "yAxis"
                                else "insideStartBottom",
                            },
                        }
                    )
    return outlier_mask, mark_data


def _scatter_plain_series(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    size: str | None,
    y_label: str,
    x_label: str,
    base_symbol: float,
    base_opacity: float,
    base_border: float,
    size_min: float,
    size_max: float,
) -> tuple[list[dict[str, Any]], list[str] | None, str | None]:
    """无颜色分组的散点 series：主系列 + （可选）IQR 离群点系列。

    返回 ``(series, 图例数据, 副标题覆盖)``；未触发离群点高亮时后两项为 ``None``。
    """
    data_cols = [x, y] + ([size] if size and size in df.columns else [])
    # IQR 离群点自动高亮（仅无颜色分组时启用，避免与分组语义冲突）：
    # x/y 任一维超出 1.5 倍四分位距即视为离群，拆独立系列暖红标出
    # 并画正常边界参考虚线；离群占比 >20% 时视为重尾分布的整体特征，
    # 不再逐点高亮以免满屏红点变成噪声。
    outlier_mask, mark_data = _scatter_outlier_overlay(df, x, y)
    n_outliers = int(outlier_mask.sum())
    if 0 < n_outliers <= max(1, int(len(df) * 0.2)):
        normal_df, outlier_df = df[~outlier_mask], df[outlier_mask]
    else:
        normal_df, outlier_df, mark_data = df, df.iloc[0:0], []

    series = [
        {
            "name": y_label,
            "type": "scatter",
            "data": _rows_data(normal_df, data_cols),
            "large": True,
            "largeThreshold": 4000,
            "symbolSize": _size_func(size, size_min, size_max) if size else base_symbol,
            "itemStyle": {
                "color": _ECHARTS_PALETTE[0],
                "opacity": base_opacity,
                "borderWidth": base_border,
                "borderColor": "#fff",
                "shadowBlur": 4 if base_border else 0,
                "shadowColor": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.3),
            },
            "emphasis": {"scale": 1.4, "itemStyle": {"opacity": 1, "shadowBlur": 10}},
        }
    ]
    if not len(outlier_df):
        return series, None, None
    # 离群点要比普通点略大、略实，才能在密集点云里被看见；
    # 但同样随总点数收敛，避免几万个离群点把画面糊满。
    series.append(
        {
            "name": "离群点",
            "type": "scatter",
            "data": _rows_data(outlier_df, data_cols),
            "symbolSize": _size_func(size, size_min, size_max)
            if size
            else max(base_symbol + 3.0, 6.0),
            "itemStyle": {
                "color": _ECHARTS_PALETTE[3],
                "opacity": min(0.92, base_opacity + 0.18),
                "borderWidth": 1.0,
                "borderColor": "#fff",
                "shadowBlur": 6 if base_border else 0,
                "shadowColor": _hex_to_rgba(_ECHARTS_PALETTE[3], 0.35),
            },
            "emphasis": {"scale": 1.5, "itemStyle": {"opacity": 1, "shadowBlur": 12}},
            # 参考虚线挂在离群系列上：点击图例隐藏离群点时边界线同步隐藏
            "markLine": {
                "silent": True,
                "symbol": "none",
                "lineStyle": {"type": "dashed", "color": "#6b7280", "width": 1},
                "label": {"color": "#6b7280", "fontSize": 11, "position": "insideEndTop"},
                "data": mark_data,
            },
        }
    )
    return (
        series,
        [y_label, "离群点"],
        f"{x_label} × {y_label} · IQR 检出 {n_outliers} 个离群点（红色标出）",
    )


def _echarts_scatter(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    size: str | None,
    title: str,
) -> dict[str, Any]:
    """散点图：支持颜色分组、大小维度、视觉映射。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else ""

    # 大数据语义与 Plotly 分支对齐：数十万点全量绘制时 10px 不透明点
    # 互相覆盖，最后绘制的系列把其它分组的颜色完全盖住（视觉上"只有
    # 一个颜色"）。点径/透明度按点数分档，与 Plotly 分支共用同一张表。
    # 注：实现位于 ``tools/_plotly_render``（builder 仅做重新导出），
    # 这里直接引用叶子模块，避免 render 子包反向依赖 tools/builder。
    from data_agent.tools._plotly_render import scatter_symbol_style

    base_symbol, base_opacity, base_border = scatter_symbol_style(len(df))

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} × {y_label}"},
        "tooltip": {
            **_ECHARTS_BASE_TOOLTIP,
            "trigger": "item",
            "formatter": _scatter_tooltip_formatter(x_label, y_label, size),
        },
        "legend": {**_ECHARTS_BASE_LEGEND},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [_echarts_value_axis(df, x, name=x_label, scale=True, is_x=True)],
        "yAxis": [_echarts_value_axis(df, y, name=y_label, scale=True)],
        # 显式 id（dz-x/dz-y）：模板里的缩放自适应脚本按 id 区分事件来自
        # 哪个轴的 dataZoom（e.batch[].dataZoomId），未加 id 时 ECharts 生成
        # 内部自动 id，脚本无法归因轴。
        "dataZoom": [
            {"id": "dz-x", "type": "inside", "xAxisIndex": 0, "filterMode": "none"},
            {"id": "dz-y", "type": "inside", "yAxisIndex": 0, "filterMode": "none"},
        ],
    }

    # 若启用 size 维度，提前计算全局 min/max 用于 symbolSize 归一化到 [6, 28]。
    size_vals = []
    if size and size in df.columns:
        size_vals = pd.to_numeric(df[size], errors="coerce").dropna().tolist()
    size_min = float(min(size_vals)) if size_vals else 0.0
    size_max = float(max(size_vals)) if size_vals else 0.0

    if color:
        color_levels = list(pd.unique(df[color].dropna()))
        data_cols = [x, y] + ([size] if size and size in df.columns else [])
        series = []
        for idx, level in enumerate(color_levels):
            sub = df[df[color] == level]
            data = _rows_data(sub, data_cols)
            series.append(
                {
                    "name": str(level),
                    "type": "scatter",
                    "data": data,
                    # 超过阈值自动启用 large 模式：万级点量下浏览器仍能流畅交互
                    "large": True,
                    "largeThreshold": 4000,
                    "symbolSize": _size_func(size, size_min, size_max) if size else base_symbol,
                    "itemStyle": {
                        "color": _series_color(idx),
                        "opacity": base_opacity,
                        "borderWidth": base_border,
                        "borderColor": "#fff",
                        # 光晕只在点少时加：几千点以上每点一圈 shadow 既拖慢渲染、
                        # 又让密集区糊成一片（点径缩小后尤其明显）。
                        "shadowBlur": 4 if base_border else 0,
                        "shadowColor": _hex_to_rgba(_series_color(idx), 0.3),
                    },
                    "emphasis": {"scale": 1.4, "itemStyle": {"opacity": 1, "shadowBlur": 10}},
                }
            )
        base["legend"]["data"] = [str(item) for item in color_levels]
        # 结构注记挂主系列：趋势线 + 均值象限线（全量数据计算，跨组全局参考）
        _append_structure_annotations(base, df, x=x, y=y, target=series[0])
        base["series"] = series
    else:
        series, legend_data, subtext = _scatter_plain_series(
            df,
            x=x,
            y=y,
            size=size,
            y_label=y_label,
            x_label=x_label,
            base_symbol=base_symbol,
            base_opacity=base_opacity,
            base_border=base_border,
            size_min=size_min,
            size_max=size_max,
        )
        if legend_data is not None:
            base["legend"]["data"] = legend_data
        if subtext is not None:
            base["title"]["subtext"] = subtext
        # 结构注记挂主系列：趋势线 + 均值象限线（与离群边界线分属不同系列，
        # 点击图例隐藏离群点时结构参考线仍在）
        _append_structure_annotations(base, df, x=x, y=y, target=series[0])
        base["series"] = series

    return base


def _size_func(size: str | None, size_min: float = 0.0, size_max: float = 0.0):
    """根据 size 列生成 symbolSize JS 函数，并把数值归一化到 [6, 28]。"""
    if not size:
        return 10
    # 用 _JsFunction 包装，避免 json.dumps 把函数源码加引号，
    # 导致 ECharts 把 symbolSize 当字符串解析而点不渲染。
    return _JsFunction(
        "function(val){"
        "var v=val[2];"
        "if(v==null||isNaN(v))return 8;"
        f"var min={size_min},max={size_max};"
        "if(max<=min)return 17;"
        "return 6+(v-min)/(max-min)*22;"
        "}"
    )


def _scatter_tooltip_formatter(x_label: str, y_label: str, size: str | None):
    # x_label / y_label / size_label 来自 CSV 列名，用 json.dumps 转义后拼入 JS
    # 字符串字面量，防止列名含特殊字符导致 XSS 注入（与 _bar_tooltip_formatter 一致）。
    # 整个函数用 _JsFunction 包装，避免序列化后被加引号变成普通字符串。
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    y_label_js = json.dumps(y_label, ensure_ascii=False)
    size_label = _build_axis_label(size) if size else None
    size_label_js = json.dumps(size_label, ensure_ascii=False) if size_label else "null"
    size_line = (
        f"html+='<span style=\"color:var(--tt-muted,#6b7280);\">'+{size_label_js}+'：</span><b>'+val[2]+'</b><br/>';"
        if size_label
        else ""
    )
    return _JsFunction(
        "function(params){"
        "var val=params.value;"
        "var html='<div style=\"font-weight:600;margin-bottom:6px;\">'+params.seriesName+'</div>';"
        f"html+='<span style=\"color:var(--tt-muted,#6b7280);\">'+{x_label_js}+'：</span><b>'+val[0]+'</b><br/>';"
        f"html+='<span style=\"color:var(--tt-muted,#6b7280);\">'+{y_label_js}+'：</span><b>'+val[1]+'</b><br/>';"
        f"{size_line}"
        "return html;"
        "}"
    )
