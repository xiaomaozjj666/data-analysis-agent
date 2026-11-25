"""柱状 / 折线 / 面积图 option 构建（直角坐标系类目图）。"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_BASE_AXIS,
    _ECHARTS_BASE_GRID,
    _ECHARTS_BASE_LEGEND,
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLBOX,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_PALETTE,
    _ECHARTS_TEXT_SECONDARY,
    _ECHARTS_VALUE_LABEL_JS,
    _ECHARTS_X_AXIS_NAME,
    _JsFunction,
)
from data_agent.render.primitives import (
    _build_linear_gradient,
    _echarts_value_axis,
    _format_time_categories,
    _hex_to_rgba,
    _safe_value,
    _series_color,
    _stacked_totals,
)
from data_agent.tools._helpers import _human_column_label as _build_axis_label


def _bar_axis_context(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    aggregation: str,
) -> tuple[str, str, str, list[Any], list[str]]:
    """柱状图的轴标签、聚合后缀与去重后的类目序列（含时间标签格式化）。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else "计数"
    agg_suffix = {
        "mean": "（平均）",
        "median": "（中位）",
        "sum": "（合计）",
        "count": "（计数）",
        "min": "（最小）",
        "max": "（最大）",
    }.get(aggregation, "")
    # raw_keys 仅作非时间列的轴标签兜底；categories 仅作轴标签展示。
    # color 分组时聚合结果是 x×color 长表，x 每个层级重复 N 次，必须去重
    # （保持首现顺序），否则类目轴每月出现 N 次、系列值也被复制 N 遍。
    axis_values_x = list(pd.unique(df[x])) if color else df[x].tolist()
    raw_keys = [str(v) for v in axis_values_x]
    categories = _format_time_categories(pd.Series(axis_values_x)) or raw_keys
    return x_label, y_label, agg_suffix, axis_values_x, categories


def _bar_series(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    stacked: bool,
    axis_values_x: list[Any],
    categories: list[str],
    y_label: str,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """柱状图 series 列表 + （可选）图例数据。"""
    if color:
        # 分组柱状：每个 color level 一个 series
        color_levels = list(pd.unique(df[color].dropna()))
        series = []
        for idx, level in enumerate(color_levels):
            sub = df[df[color] == level]
            # 用原始 x 值（非 str）reindex：datetime 轴用字符串键会对不上索引全部变 NaN
            data = (
                [_safe_value(v) for v in sub.set_index(x).reindex(axis_values_x)[y].tolist()]
                if y
                else []
            )
            series.append(
                {
                    "name": str(level),
                    "type": "bar",
                    # 堆叠模式：同类目各系列叠起来，Y 轴按堆叠总和计算（下方 yAxis）
                    **({"stack": "Total", "barGap": "0%"} if stacked else {}),
                    "data": data,
                    "barMaxWidth": 32 if not stacked else 48,
                    "barGap": "20%" if not stacked else "0%",
                    "barCategoryGap": "30%",
                    "itemStyle": {
                        "color": _build_linear_gradient(_series_color(idx)),
                        "borderRadius": [4, 4, 0, 0],
                    },
                    "emphasis": {
                        "itemStyle": {
                            "color": _series_color(idx),
                            "shadowBlur": 12,
                            "shadowColor": _hex_to_rgba(_series_color(idx), 0.4),
                        }
                    },
                }
            )
        return series, [str(item) for item in color_levels]

    if y is None:
        return [], None
    data = [_safe_value(v) for v in df[y].tolist()]
    return (
        [
            {
                "name": y_label,
                "type": "bar",
                "data": data,
                "barMaxWidth": 48,
                "barCategoryGap": "40%",
                "itemStyle": {
                    "color": _build_linear_gradient(_ECHARTS_PALETTE[0]),
                    "borderRadius": [4, 4, 0, 0],
                },
                "emphasis": {
                    "itemStyle": {
                        "color": _ECHARTS_PALETTE[0],
                        "shadowBlur": 12,
                        "shadowColor": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.4),
                    }
                },
                "label": {
                    "show": len(categories) <= 12,
                    "position": "top",
                    "color": _ECHARTS_TEXT_SECONDARY,
                    "fontSize": 11,
                    "formatter": _ECHARTS_VALUE_LABEL_JS,
                },
            }
        ],
        None,
    )


def _echarts_bar(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str,
    stacked: bool = False,
) -> dict[str, Any]:
    """分组/堆叠柱状图：默认分组，color 维度自动展开为多系列。

    ``stacked=True`` 时同一类目的多个系列堆叠（ECharts 的 ``series.stack``），
    Y 轴范围按**各类目堆叠总和**计算并强制含 0。此前只支持分组柱，模型按
    "堆叠"写标题时图上却是并排柱——标题与图形不一致，用户会以为数据错了。
    """
    # 轴方向容错：模型偶尔把类目列写到 y、数值列写到 x（"各省订单量"却给成
    # x=订单量、y=省份）。ECharts 的 category 轴会拿到数字、value 轴会拿到
    # 字符串，结果是一张空图（实测：天猫 TOP10 省份柱状图一根柱子都没有）。
    # 类目轴放文本列、数值轴放数值列是硬约束，颠倒时直接交换。
    if y and x in df.columns and y in df.columns:
        if pd.api.types.is_numeric_dtype(df[x]) and not pd.api.types.is_numeric_dtype(df[y]):
            x, y = y, x

    x_label, y_label, agg_suffix, axis_values_x, categories = _bar_axis_context(
        df, x=x, y=y, color=color, aggregation=aggregation
    )

    base: dict[str, Any] = {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            "subtext": f"{x_label} × {y_label}{agg_suffix}",
        },
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {**_ECHARTS_BASE_LEGEND},
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
                    "hideOverlap": True,
                    "rotate": 30 if len(categories) > 8 else 0,
                },
            }
        ],
        "yAxis": [
            _echarts_value_axis(
                df,
                y,
                name=f"{y_label}{agg_suffix}",
                scale=False,
                zero_base=True,
                values=_stacked_totals(df, x=x, y=y, color=color)
                if (stacked and color and y)
                else None,
            )
        ],
    }

    series, legend_data = _bar_series(
        df,
        x=x,
        y=y,
        color=color,
        stacked=stacked,
        axis_values_x=axis_values_x,
        categories=categories,
        y_label=y_label,
    )
    if legend_data is not None:
        base["legend"]["data"] = legend_data
    base["series"] = series
    base["tooltip"]["formatter"] = _bar_tooltip_formatter(x_label, y_label, agg_suffix)
    return base


def _bar_tooltip_formatter(x_label: str, y_label: str, agg_suffix: str):
    """柱状图 tooltip 自定义 formatter：展示 x、各系列值、合计。"""
    # 返回 _JsFunction，前端 echarts 拿到真实 JS 函数；用 json.dumps 转义 x_label
    # 后拼入 JS 字符串字面量，防止列名含 ' / " / </script> 导致 JS 语法错误或 XSS 注入。
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    return _JsFunction(
        "function(params){"
        f"var html='<div style=\"font-weight:600;margin-bottom:6px;\">'+{x_label_js}+'：'+params[0].axisValue+'</div>';"
        "var total=0;"
        "params.forEach(function(p){"
        "total+=p.value||0;"
        "html+='<div style=\"display:flex;align-items:center;gap:8px;margin:4px 0;\">';"
        "html+='<span style=\"width:8px;height:8px;border-radius:50%;background:'+p.color+';\"></span>';"
        "html+='<span style=\"flex:1;\">'+p.seriesName+'</span>';"
        "html+='<span style=\"font-weight:500;\">'+(p.value==null?'—':p.value.toLocaleString())+'</span>';"
        "html+='</div>';"
        "});"
        "if(params.length>1){html+='<div style=\"margin-top:6px;border-top:1px solid var(--tt-border,#e4e6ea);padding-top:6px;\">合计：'+total.toLocaleString()+'</div>';}"
        "return html;"
        "}"
    )


def _line_axis_context(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    aggregation: str,
) -> tuple[str, str, str, list[Any], list[str]]:
    """折线/面积图的轴标签、聚合后缀与去重后的类目序列。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else "计数"
    agg_suffix = {
        "mean": "（平均）",
        "median": "（中位）",
        "sum": "（合计）",
        "count": "（计数）",
    }.get(aggregation, "")
    # raw_keys 仅作非时间列的轴标签兜底；categories 仅作轴标签展示，
    # 时间列按粒度智能格式化（月度→YYYY-MM，整日→YYYY-MM-DD）。
    # color 分组时聚合结果是 x×color 长表，x 每个层级重复 N 次，必须去重
    # （保持首现顺序），否则类目轴每月出现 N 次、趋势线变成阶梯平台。
    axis_values_x = list(pd.unique(df[x])) if color else df[x].tolist()
    raw_keys = [str(v) for v in axis_values_x]
    categories = _format_time_categories(pd.Series(axis_values_x)) or raw_keys
    return x_label, y_label, agg_suffix, axis_values_x, categories


def _line_series(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    area: bool,
    axis_values_x: list[Any],
    categories: list[str],
    y_label: str,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """折线/面积图 series 列表 + （可选）图例数据。"""
    chart_type = "line"

    if color:
        color_levels = list(pd.unique(df[color].dropna()))
        series = []
        for idx, level in enumerate(color_levels):
            # 用原始 x 值（非 str）reindex：datetime 轴用字符串键会对不上索引全部变 NaN
            sub = df[df[color] == level].set_index(x).reindex(axis_values_x)
            data = [_safe_value(v) for v in sub[y].tolist()] if y else []
            s: dict[str, Any] = {
                "name": str(level),
                "type": chart_type,
                "data": data,
                "smooth": True,
                "smoothMonotone": "x",
                "symbol": "circle",
                "symbolSize": 7,
                "showSymbol": len(categories) <= 30,
                # LTTB 降采样：点数远超像素宽度时保留趋势形状降低绘制开销，
                # 小数据量下无副作用（ECharts 大数据量优化的主流方案）。
                "sampling": "lttb",
                "lineStyle": {"width": 2.5, "color": _series_color(idx)},
                "itemStyle": {"color": _series_color(idx), "borderWidth": 2, "borderColor": "#fff"},
                "emphasis": {"focus": "series", "blurScope": "coordinateSystem"},
            }
            if area:
                # 堆叠面积图：柔和渐变填充
                s["stack"] = "Total"
                s["areaStyle"] = {"color": _build_linear_gradient(_series_color(idx))}
                s["lineStyle"]["width"] = 2
            series.append(s)
        return series, [str(item) for item in color_levels]

    if y is None:
        return [], None
    data = [_safe_value(v) for v in df[y].tolist()]
    s = {
        "name": y_label,
        "type": chart_type,
        "data": data,
        "smooth": True,
        "smoothMonotone": "x",
        "symbol": "circle",
        "symbolSize": 8,
        "showSymbol": len(categories) <= 60,
        "sampling": "lttb",
        "lineStyle": {"width": 3, "color": _ECHARTS_PALETTE[0]},
        "itemStyle": {
            "color": _ECHARTS_PALETTE[0],
            "borderWidth": 2,
            "borderColor": "#fff",
        },
        "emphasis": {"focus": "series"},
    }
    # 峰谷标记 + 均值参考线（ECharts 官方折线示例标配）：单系列才加，
    # 多系列叠加会成视觉噪声；不足 5 个有效点时极值/均值无解读价值。
    if sum(1 for v in data if v is not None) >= 5:
        _mark_value_js = _JsFunction(
            "function(p){var v=p.value;if(v==null)return '';"
            "return Math.abs(v)>=10000?(v/10000).toFixed(1)+'\u4e07':v;}"
        )
        s["markPoint"] = {
            "symbol": "pin",
            "symbolSize": 42,
            "label": {"color": "#fff", "fontSize": 10, "formatter": _mark_value_js},
            "itemStyle": {"color": _ECHARTS_PALETTE[0], "opacity": 0.88},
            "data": [{"type": "max", "name": "峰值"}, {"type": "min", "name": "谷值"}],
        }
        s["markLine"] = {
            "silent": True,
            "symbol": "none",
            "lineStyle": {"type": "dashed", "color": "#6b7280", "width": 1},
            "label": {
                "color": "#6b7280",
                "fontSize": 11,
                "position": "insideEndTop",
                "formatter": _JsFunction(
                    "function(p){var v=p.value;"
                    "return '\u5747\u503c '+(Math.abs(v)>=10000?(v/10000).toFixed(2)+'\u4e07':Math.round(v*100)/100);}"
                ),
            },
            "data": [{"type": "average", "name": "均值"}],
        }
    if area:
        s["areaStyle"] = {"color": _build_linear_gradient(_ECHARTS_PALETTE[0])}
    return [s], None


def _echarts_line(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str,
    area: bool = False,
) -> dict[str, Any]:
    """折线/面积图：支持平滑、多系列、区间缩放。"""
    x_label, y_label, agg_suffix, axis_values_x, categories = _line_axis_context(
        df, x=x, y=y, color=color, aggregation=aggregation
    )

    # 堆叠面积图（area + color 多系列）：Y 轴范围必须按各类目堆叠总和计算，
    # 否则用单列 min/max 会让堆叠后的线/面冲出绘图区顶部；并入 0 保证堆叠基线可见。
    stacked = area and bool(color) and y is not None
    axis_values: list[float] | None = None
    if stacked:
        totals = df.groupby(x, sort=False)[y].sum(min_count=1).dropna()
        axis_values = [0.0, *(float(v) for v in totals.tolist())]

    base: dict[str, Any] = {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            "subtext": f"{x_label} × {y_label}{agg_suffix}",
        },
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "axis", "axisPointer": {"type": "line"}},
        "legend": {**_ECHARTS_BASE_LEGEND},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [
            {
                **_ECHARTS_BASE_AXIS,
                "type": "category",
                "boundaryGap": False,
                "data": categories,
                "name": x_label,
                **_ECHARTS_X_AXIS_NAME,
                # hideOverlap：密集时间标签自动抽疏，避免重叠成墨团（ECharts v5 主流做法）
                "axisLabel": {**_ECHARTS_BASE_AXIS["axisLabel"], "hideOverlap": True},
            }
        ],
        # 堆叠时 scale=False 从 0 起；非堆叠 scale=True 让 Y 轴自适应非零起点
        "yAxis": [
            _echarts_value_axis(
                df, y, name=f"{y_label}{agg_suffix}", scale=not stacked, values=axis_values
            )
        ],
        "dataZoom": [
            {"type": "inside", "start": 0, "end": 100},
            {
                "type": "slider",
                "start": 0,
                "end": 100,
                "height": 22,
                "bottom": 16,
                "borderColor": "transparent",
                "backgroundColor": "#eceef1",
                "fillerColor": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.12),
                "handleStyle": {"color": _ECHARTS_PALETTE[0]},
                "textStyle": {"color": _ECHARTS_TEXT_SECONDARY},
            },
        ],
    }

    series, legend_data = _line_series(
        df,
        x=x,
        y=y,
        color=color,
        area=area,
        axis_values_x=axis_values_x,
        categories=categories,
        y_label=y_label,
    )
    if legend_data is not None:
        base["legend"]["data"] = legend_data
    base["series"] = series
    return base
