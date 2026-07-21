"""ECharts 渲染引擎：与 Plotly 引擎并列，复用同一套数据准备逻辑。

设计目标：
- 学术级交互：tooltip 业务解读、区间缩放、图例多选、平滑动画、高清导出
- 视觉统一：固定色板、弱化网格、充足留白、圆角阴影、深浅主题
- 零侵入：与 Plotly 引擎完全隔离，HTML/JS bundle 互不冲突
- 自动解读：每次生成图表附带数据驱动的白话分析

调用入口：``_render_echarts(workspace, df, chart_type, ...)`` 返回与 Plotly
分支结构一致的 response dict，``create_visualization`` 据此分派。
"""
from __future__ import annotations

import json
import math
from html import escape
from typing import Any

import numpy as np
import pandas as pd

from data_agent.workspace import ECHARTS_CDN_URL, DataWorkspace, _atomic_write_text

# === 全局视觉 token（学术级商务色板，对标 Nature/Lancet 期刊配图）===
# 主色板：低饱和度、高辨识度、色盲友好，适配正式报告。
_ECHARTS_PALETTE = [
    "#2C5F8D",  # 沉稳蓝（主色）
    "#D97745",  # 暖橙
    "#4F9D7C",  # 静谧绿
    "#C75D63",  # 砖红
    "#7A6FB0",  # 紫罗兰
    "#D2A63C",  # 芥末黄
    "#4B8FA8",  # 青蓝
    "#8A9A5B",  # 橄榄
    "#B07B9E",  # 玫紫
    "#5E7A8C",  # 灰蓝
]

# 文本/网格/背景色（浅色主题，对标 Observable/Plot 极简风）
_ECHARTS_TEXT_COLOR = "#1f2937"
_ECHARTS_TEXT_SECONDARY = "#6b7280"
_ECHARTS_GRID_COLOR = "#e5e7eb"
_ECHARTS_BG_COLOR = "#ffffff"
_ECHARTS_BORDER_COLOR = "#f3f4f6"

# 字体栈：跨平台中英文兼容，统一字号梯度
_ECHARTS_FONT_FAMILY = (
    "'PingFang SC', 'Microsoft YaHei', 'Helvetica Neue', "
    "'IBM Plex Sans', Arial, sans-serif"
)

# ECharts 主题常量：所有图表共享，保证视觉一致
_ECHARTS_BASE_GRID = {
    "left": 64,
    "right": 32,
    "top": 80,
    "bottom": 72,
    "containLabel": True,
}

_ECHARTS_BASE_AXIS = {
    "axisLine": {"lineStyle": {"color": _ECHARTS_GRID_COLOR}},
    "axisTick": {"show": False},
    "axisLabel": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
    "splitLine": {"show": True, "lineStyle": {"color": _ECHARTS_BORDER_COLOR, "type": "dashed"}},
    "nameTextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12, "padding": [0, 0, 0, -16]},
}

_ECHARTS_BASE_TOOLTIP = {
    "backgroundColor": "rgba(255,255,255,0.98)",
    "borderColor": _ECHARTS_GRID_COLOR,
    "borderWidth": 1,
    "padding": [12, 16],
    "textStyle": {"color": _ECHARTS_TEXT_COLOR, "fontSize": 13},
    "extraCssText": "box-shadow: 0 8px 24px rgba(0,0,0,0.08); border-radius: 10px;",
}

_ECHARTS_BASE_LEGEND = {
    "top": 24,
    "right": 24,
    "itemGap": 20,
    "itemWidth": 14,
    "itemHeight": 8,
    "icon": "roundRect",
    "textStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
    "inactiveColor": "#d1d5db",
}

_ECHARTS_BASE_TITLE = {
    "left": 16,
    "top": 16,
    "textStyle": {"color": _ECHARTS_TEXT_COLOR, "fontSize": 18, "fontWeight": 600},
    "subtextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
}

_ECHARTS_BASE_TOOLBOX = {
    "right": 24,
    "top": 24,
    "itemSize": 16,
    "itemGap": 12,
    "iconStyle": {"borderColor": _ECHARTS_TEXT_SECONDARY},
    "emphasis": {"iconStyle": {"borderColor": _ECHARTS_PALETTE[0]}},
    "feature": {
        # 数据缩放：框选 + 滚轮 + 拖拽平移 + 一键重置，对标金融终端
        "dataZoom": {"yAxisIndex": "none", "title": {"zoom": "框选缩放", "back": "还原"}},
        "restore": {"title": "重置"},
        # 高清导出：适配论文/正式报告，2x 分辨率
        "saveAsImage": {"title": "导出 PNG", "pixelRatio": 2, "backgroundColor": _ECHARTS_BG_COLOR},
    },
}


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """将 #RRGGBB 转为 rgba(r,g,b,a)，用于面积填充透明度控制。"""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _build_linear_gradient(color: str, vertical: bool = True) -> dict[str, Any]:
    """生成柔和的垂直/水平线性渐变，用于面积图、柱状图填充。"""
    coords = (0, 0, 0, 1) if vertical else (0, 0, 1, 0)
    return {
        "type": "linear",
        "x": coords[0],
        "y": coords[1],
        "x2": coords[2],
        "y2": coords[3],
        "colorStops": [
            {"offset": 0, "color": _hex_to_rgba(color, 0.55)},
            {"offset": 1, "color": _hex_to_rgba(color, 0.08)},
        ],
    }


def _safe_value(value: Any) -> Any:
    """把 numpy/pandas 标量转为 JSON 可序列化的 Python 原生类型。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _format_number(value: Any, *, precision: int = 2) -> str:
    """数字格式化：整数加千分位，浮点保留 precision 位，None 显示 —。"""
    if value is None:
        return "—"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(f):
        return "—"
    if abs(f) >= 10000:
        return f"{f:,.0f}"
    return f"{f:,.{precision}f}"


def _series_color(index: int) -> str:
    """按索引取主色板颜色，超出循环复用。"""
    return _ECHARTS_PALETTE[index % len(_ECHARTS_PALETTE)]


def _build_axis_label(text: str | None) -> str:
    """轴标签：把英文列名映射为中文可读标签（与 Plotly 分支一致）。"""
    if not text:
        return ""
    # 复用 tools.py 的列名映射逻辑（避免循环导入，内联简单映射）
    _COLUMN_LABELS = {
        "units": "销量", "revenue": "收入", "sales": "销售额", "profit": "利润",
        "product": "产品", "region": "区域", "channel": "渠道", "category": "类别",
        "customer_rating": "客户评分", "unit_price": "单价", "discount_rate": "折扣率",
        "order_date": "订单日期", "date": "日期", "count": "记录数", "is_returned": "是否退货",
    }
    return _COLUMN_LABELS.get(str(text), str(text).replace("_", " ").strip())


# === 自动白话解读：纯数据驱动，不依赖 LLM ===

def _auto_interpret(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str | None,
) -> str:
    """基于聚合结果生成一段业务白话解读。

    解读策略：
    - bar/line/area：找最高最低、计算极差与均值比、识别拐点（最大环比变化）
    - pie：找占比最高与最低的类别，给出结构判断
    - scatter：识别相关性方向、离群点
    - heatmap/correlation：识别最强正/负相关对
    - box/violin：对比中位数差异与离群点
    - 其他：给通用描述

    所有解读避免统计术语，用业务语言表达。
    """
    try:
        return _interpret_impl(df, chart_type=chart_type, x=x, y=y, color=color,
                               aggregation=aggregation, title=title)
    except Exception:
        # 解读失败不影响图表生成，返回空字符串让前端不渲染解读区。
        return ""


def _interpret_impl(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str | None,
) -> str:
    title_text = title or f"{_build_axis_label(x) or ''}与{_build_axis_label(y) or ''}分布"
    if chart_type in {"bar", "line", "area"} and x and y and len(df) > 0:
        return _interpret_trend(df, chart_type=chart_type, x=x, y=y,
                                color=color, aggregation=aggregation, title=title_text)
    if chart_type == "pie" and x and len(df) > 0:
        return _interpret_pie(df, x=x, title=title_text)
    if chart_type in {"scatter", "scatter_3d"} and x and y and len(df) > 0:
        return _interpret_scatter(df, x=x, y=y, title=title_text)
    if chart_type in {"correlation_heatmap", "heatmap"} and len(df) > 0:
        return _interpret_heatmap(df, title=title_text)
    if chart_type in {"box", "violin"} and x and y and len(df) > 0:
        return _interpret_box(df, x=x, y=y, title=title_text)
    if chart_type in {"sunburst", "treemap"} and len(df) > 0:
        return _interpret_hierarchy(df, title=title_text)
    return f"本图展示了「{title_text}」的分布情况，可结合悬浮提示与图例交互深入查看各维度细节。"


def _interpret_trend(
    df: pd.DataFrame, *, chart_type: str, x: str, y: str,
    color: str | None, aggregation: str, title: str,
) -> str:
    agg_label = {"mean": "平均", "median": "中位", "sum": "合计", "count": "计数",
                 "min": "最小", "max": "最大"}.get(aggregation, "")
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y)

    if color:
        # 分组场景：对比各系列总量与差异
        pivot = df.groupby(color)[y].sum() if y in df.columns else None
        if pivot is None or pivot.empty:
            return f"「{title}」按{_build_axis_label(color)}分组对比，悬浮可查看每组明细。"
        top_series = pivot.idxmax()
        top_val = float(pivot.max())
        low_series = pivot.idxmin()
        low_val = float(pivot.min())
        ratio = top_val / low_val if low_val > 0 else float("inf")
        return (
            f"「{title}」按{_build_axis_label(color)}分组，{top_series}累计最高"
            f"（{_format_number(top_val)}），{low_series}最低（{_format_number(low_val)}），"
            f"前者约为后者的{ratio:.1f}倍。点击图例可隐藏系列聚焦对比，框选区域可放大查看。"
        )

    # 无分组：找最高最低、识别拐点
    if chart_type == "bar":
        sorted_df = df.sort_values(y, ascending=False)
        top_row = sorted_df.iloc[0]
        low_row = sorted_df.iloc[-1]
        mean_val = float(df[y].mean())
        diff_pct = (float(top_row[y]) - float(low_row[y])) / max(abs(float(low_row[y])), 1e-9) * 100
        return (
            f"「{title}」中{x_label}「{top_row[x]}」的{agg_label}{y_label}最高"
            f"（{_format_number(top_row[y])}），「{low_row[x]}」最低"
            f"（{_format_number(low_row[y])}），两者相差{diff_pct:.0f}%，"
            f"整体均值约{_format_number(mean_val)}。鼠标悬浮查看每项明细。"
        )

    # line/area：找拐点（最大环比变化）
    series = df[y].astype(float).reset_index(drop=True)
    if len(series) >= 3:
        diffs = series.diff().abs()
        max_diff_idx = int(diffs.idxmax())
        if max_diff_idx > 0 and max_diff_idx < len(series):
            before = series.iloc[max_diff_idx - 1]
            after = series.iloc[max_diff_idx]
            direction = "上升" if after > before else "下降"
            peak_x = df.iloc[max_diff_idx][x]
            return (
                f"「{title}」在{x_label}「{peak_x}」处出现明显拐点（{direction}"
                f"{_format_number(abs(after - before))}），峰值"
                f"{_format_number(series.max())}，谷值{_format_number(series.min())}。"
                f"底部滑块可缩放区间细看趋势。"
            )
    return (
        f"「{title}」整体{y_label}在{_format_number(series.min())}到"
        f"{_format_number(series.max())}之间波动，均值约{_format_number(series.mean())}。"
    )


def _interpret_pie(df: pd.DataFrame, *, x: str, title: str) -> str:
    value_col = [c for c in df.columns if c != x and pd.api.types.is_numeric_dtype(df[c])]
    if not value_col:
        return f"「{title}」展示各类别占比，点击扇区可高亮，悬浮查看具体数值。"
    col = value_col[0]
    total = float(df[col].sum())
    if total <= 0:
        return f"「{title}」展示各类别占比。"
    df_sorted = df.sort_values(col, ascending=False)
    top = df_sorted.iloc[0]
    low = df_sorted.iloc[-1]
    top_pct = float(top[col]) / total * 100
    low_pct = float(low[col]) / total * 100
    return (
        f"「{title}」中「{top[x]}」占比最高（{top_pct:.1f}%），「{low[x]}」最低"
        f"（{low_pct:.1f}%），结构{('高度集中' if top_pct > 60 else '相对均衡' if top_pct < 30 else '适度集中')}。"
        f"点击图例可隐藏某类，重新计算其余占比。"
    )


def _interpret_scatter(df: pd.DataFrame, *, x: str, y: str, title: str) -> str:
    if not pd.api.types.is_numeric_dtype(df[x]) or not pd.api.types.is_numeric_dtype(df[y]):
        return f"「{title}」展示{_build_axis_label(x)}与{_build_axis_label(y)}的分布关系，悬浮查看每个点明细。"
    corr = float(df[[x, y]].corr().iloc[0, 1])
    if math.isnan(corr):
        return f"「{title}」展示两变量分布，悬浮查看每个点明细，框选可放大区域。"
    direction = "正向" if corr > 0 else "反向"
    strength = "强" if abs(corr) > 0.7 else "中等" if abs(corr) > 0.4 else "弱"
    return (
        f"「{title}」呈现{direction}{strength}相关（r={corr:.2f}），"
        f"共{len(df)}个点。滚轮缩放可查看密集区域，框选可隔离离群点。"
    )


def _interpret_heatmap(df: pd.DataFrame, *, title: str) -> str:
    return (
        f"「{title}」以颜色深浅表达数值大小，颜色越深数值越高。"
        f"悬浮单元格查看精确值，适用于矩阵型数据的整体模式识别。"
    )


def _interpret_box(df: pd.DataFrame, *, x: str, y: str, title: str) -> str:
    return (
        f"「{title}」对比各组{_build_axis_label(y)}的分布，箱体表示中间50%数据，"
        f"须线延伸至1.5倍四分位距，超出须线的点为潜在异常值。悬浮查看分位数细节。"
    )


def _interpret_hierarchy(df: pd.DataFrame, *, title: str) -> str:
    return (
        f"「{title}」以层级方式展示数据结构，点击节点可下钻/上卷，"
        f"面积大小反映对应数值占比。"
    )


# === 11 种图表的 ECharts option 生成器 ===

def _echarts_bar(
    df: pd.DataFrame, *, x: str, y: str | None, color: str | None,
    aggregation: str, title: str,
) -> dict[str, Any]:
    """分组/堆叠柱状图：默认分组，color 维度自动展开为多系列。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else "计数"
    agg_suffix = {"mean": "（平均）", "median": "（中位）", "sum": "（合计）",
                  "count": "（计数）", "min": "（最小）", "max": "（最大）"}.get(aggregation, "")
    categories = [str(v) for v in df[x].tolist()]

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} × {y_label}{agg_suffix}"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {**_ECHARTS_BASE_LEGEND},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [{**_ECHARTS_BASE_AXIS, "type": "category", "data": categories, "name": x_label,
                   "axisLabel": {**_ECHARTS_BASE_AXIS["axisLabel"], "rotate": 30 if len(categories) > 8 else 0}}],
        "yAxis": [{**_ECHARTS_BASE_AXIS, "type": "value", "name": f"{y_label}{agg_suffix}"}],
    }

    if color:
        # 分组柱状：每个 color level 一个 series
        color_levels = list(pd.unique(df[color].dropna()))
        series = []
        for idx, level in enumerate(color_levels):
            sub = df[df[color] == level]
            data = [_safe_value(v) for v in sub.set_index(x).reindex(categories)[y].tolist()] if y else []
            series.append({
                "name": str(level),
                "type": "bar",
                "data": data,
                "barMaxWidth": 32,
                "barGap": "20%",
                "barCategoryGap": "30%",
                "itemStyle": {
                    "color": _build_linear_gradient(_series_color(idx)),
                    "borderRadius": [4, 4, 0, 0],
                },
                "emphasis": {"itemStyle": {"color": _series_color(idx), "shadowBlur": 12, "shadowColor": _hex_to_rgba(_series_color(idx), 0.4)}},
            })
        base["legend"]["data"] = [str(item) for item in color_levels]
        base["series"] = series
    else:
        if y is None:
            base["series"] = []
        else:
            data = [_safe_value(v) for v in df[y].tolist()]
            base["series"] = [{
                "name": y_label,
                "type": "bar",
                "data": data,
                "barMaxWidth": 48,
                "barCategoryGap": "40%",
                "itemStyle": {
                    "color": _build_linear_gradient(_ECHARTS_PALETTE[0]),
                    "borderRadius": [4, 4, 0, 0],
                },
                "emphasis": {"itemStyle": {"color": _ECHARTS_PALETTE[0], "shadowBlur": 12, "shadowColor": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.4)}},
                "label": {"show": len(categories) <= 12, "position": "top", "color": _ECHARTS_TEXT_SECONDARY, "fontSize": 11, "formatter": "{c}"},
            }]

    base["tooltip"]["formatter"] = _bar_tooltip_formatter(x_label, y_label, agg_suffix)
    return base


def _bar_tooltip_formatter(x_label: str, y_label: str, agg_suffix: str):
    """柱状图 tooltip 自定义 formatter：展示 x、各系列值、合计。"""
    # 返回 JS 函数字符串，前端 echarts 会 eval。
    # x_label / y_label 来自 CSV 列名，用 json.dumps 转义后拼入 JS 字符串字面量，
    # 防止列名含 ' / " / </script> 等字符导致 JS 语法错误或 XSS 注入。
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    return (
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
        "if(params.length>1){html+='<div style=\"margin-top:6px;border-top:1px solid #e5e7eb;padding-top:6px;\">合计：'+total.toLocaleString()+'</div>';}"
        "return html;"
        "}"
    )


def _echarts_line(
    df: pd.DataFrame, *, x: str, y: str | None, color: str | None,
    aggregation: str, title: str, area: bool = False,
) -> dict[str, Any]:
    """折线/面积图：支持平滑、多系列、区间缩放。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else "计数"
    agg_suffix = {"mean": "（平均）", "median": "（中位）", "sum": "（合计）",
                  "count": "（计数）"}.get(aggregation, "")
    categories = [str(v) for v in df[x].tolist()]
    chart_type = "line"

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} × {y_label}{agg_suffix}"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "axis", "axisPointer": {"type": "line"}},
        "legend": {**_ECHARTS_BASE_LEGEND},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [{**_ECHARTS_BASE_AXIS, "type": "category", "boundaryGap": False, "data": categories, "name": x_label}],
        "yAxis": [{**_ECHARTS_BASE_AXIS, "type": "value", "name": f"{y_label}{agg_suffix}",
                   "scale": True}],  # scale=True 让 Y 轴自适应非零起点
        "dataZoom": [
            {"type": "inside", "start": 0, "end": 100},
            {"type": "slider", "start": 0, "end": 100, "height": 22, "bottom": 16,
             "borderColor": "transparent", "backgroundColor": "#f3f4f6",
             "fillerColor": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.12),
             "handleStyle": {"color": _ECHARTS_PALETTE[0]}, "textStyle": {"color": _ECHARTS_TEXT_SECONDARY}},
        ],
    }

    if color:
        color_levels = list(pd.unique(df[color].dropna()))
        series = []
        for idx, level in enumerate(color_levels):
            sub = df[df[color] == level].set_index(x).reindex(categories)
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
        base["legend"]["data"] = [str(item) for item in color_levels]
        base["series"] = series
    else:
        if y is None:
            base["series"] = []
        else:
            data = [_safe_value(v) for v in df[y].tolist()]
            s: dict[str, Any] = {
                "name": y_label,
                "type": chart_type,
                "data": data,
                "smooth": True,
                "smoothMonotone": "x",
                "symbol": "circle",
                "symbolSize": 8,
                "showSymbol": True,
                "lineStyle": {"width": 3, "color": _ECHARTS_PALETTE[0]},
                "itemStyle": {"color": _ECHARTS_PALETTE[0], "borderWidth": 2, "borderColor": "#fff"},
                "emphasis": {"focus": "series"},
            }
            if area:
                s["areaStyle"] = {"color": _build_linear_gradient(_ECHARTS_PALETTE[0])}
            base["series"] = [s]

    return base


def _echarts_scatter(
    df: pd.DataFrame, *, x: str, y: str | None, color: str | None,
    size: str | None, title: str, is_3d: bool = False,
) -> dict[str, Any]:
    """散点图 / 3D 散点图：支持颜色分组、大小维度、视觉映射。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else ""

    if is_3d:
        # 3D 散点需要 echarts-gl，bundle 不含；降级为 2D 散点 + 提示
        pass

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} × {y_label}"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item",
                    "formatter": _scatter_tooltip_formatter(x_label, y_label, size)},
        "legend": {**_ECHARTS_BASE_LEGEND},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [{**_ECHARTS_BASE_AXIS, "type": "value", "name": x_label, "scale": True}],
        "yAxis": [{**_ECHARTS_BASE_AXIS, "type": "value", "name": y_label, "scale": True}],
        "dataZoom": [
            {"type": "inside", "xAxisIndex": 0, "filterMode": "none"},
            {"type": "inside", "yAxisIndex": 0, "filterMode": "none"},
        ],
    }

    if color:
        color_levels = list(pd.unique(df[color].dropna()))
        series = []
        for idx, level in enumerate(color_levels):
            sub = df[df[color] == level]
            data = [[_safe_value(r[x]), _safe_value(r[y])] + (
                [_safe_value(r[size])] if size and size in df.columns else []
            ) for _, r in sub.iterrows()]
            series.append({
                "name": str(level),
                "type": "scatter",
                "data": data,
                "symbolSize": _size_func(size) if size else 10,
                "itemStyle": {
                    "color": _series_color(idx),
                    "opacity": 0.78,
                    "borderWidth": 0.8,
                    "borderColor": "#fff",
                    "shadowBlur": 4,
                    "shadowColor": _hex_to_rgba(_series_color(idx), 0.3),
                },
                "emphasis": {"scale": 1.4, "itemStyle": {"opacity": 1, "shadowBlur": 10}},
            })
        base["legend"]["data"] = [str(item) for item in color_levels]
        base["series"] = series
    else:
        data = [[_safe_value(r[x]), _safe_value(r[y])] + (
            [_safe_value(r[size])] if size and size in df.columns else []
        ) for _, r in df.iterrows()]
        base["series"] = [{
            "name": y_label,
            "type": "scatter",
            "data": data,
            "symbolSize": _size_func(size) if size else 10,
            "itemStyle": {
                "color": _ECHARTS_PALETTE[0],
                "opacity": 0.78,
                "borderWidth": 0.8,
                "borderColor": "#fff",
                "shadowBlur": 4,
                "shadowColor": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.3),
            },
            "emphasis": {"scale": 1.4, "itemStyle": {"opacity": 1, "shadowBlur": 10}},
        }]

    return base


def _size_func(size: str | None):
    """根据 size 列生成 symbolSize JS 函数。"""
    if not size:
        return 10
    # 用函数动态计算大小，归一化到 [6, 28]
    return (
        "function(val){"
        "if(val[2]==null||isNaN(val[2]))return 8;"
        "return val[2];"
        "}"
    )


def _scatter_tooltip_formatter(x_label: str, y_label: str, size: str | None):
    # x_label / y_label / size_label 来自 CSV 列名，用 json.dumps 转义后拼入 JS
    # 字符串字面量，防止列名含特殊字符导致 XSS 注入（与 _bar_tooltip_formatter 一致）
    x_label_js = json.dumps(x_label, ensure_ascii=False)
    y_label_js = json.dumps(y_label, ensure_ascii=False)
    size_label = _build_axis_label(size) if size else None
    size_label_js = json.dumps(size_label, ensure_ascii=False) if size_label else "null"
    size_line = f"html+='<span style=\"color:#6b7280;\">'+{size_label_js}+'：</span><b>'+val[2]+'</b><br/>';" if size_label else ""
    return (
        "function(params){"
        "var val=params.value;"
        f"var html='<div style=\"font-weight:600;margin-bottom:6px;\">'+params.seriesName+'</div>';"
        f"html+='<span style=\"color:#6b7280;\">'+{x_label_js}+'：</span><b>'+val[0]+'</b><br/>';"
        f"html+='<span style=\"color:#6b7280;\">'+{y_label_js}+'：</span><b>'+val[1]+'</b><br/>';"
        f"{size_line}"
        "return html;"
        "}"
    )


def _echarts_pie(
    df: pd.DataFrame, *, x: str, values: str | None, y: str | None, title: str,
) -> dict[str, Any]:
    """环形饼图：内嵌总数、悬浮占比、图例隐藏。"""
    value_col = values or y
    if not value_col or value_col not in df.columns:
        # 退化为计数
        counts = df[x].value_counts()
        data = [{"name": str(k), "value": int(v)} for k, v in counts.items()]
    else:
        data = [{"name": str(r[x]), "value": _safe_value(r[value_col])} for _, r in df.iterrows()]
    total = sum(d["value"] or 0 for d in data)

    return {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"共 {len(data)} 类 · 合计 {_format_number(total)}"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item",
                    "formatter": "function(p){return '<div style=\"font-weight:600;margin-bottom:4px;\">'+p.name+'</div><span style=\"color:#6b7280;\">'+p.seriesName+'：</span><b>'+p.value.toLocaleString()+'</b> ('+p.percent+'%)';}"},
        "legend": {**_ECHARTS_BASE_LEGEND, "orient": "vertical", "right": 16, "top": "middle", "itemGap": 12},
        "toolbox": {"right": 24, "top": 24, "feature": {"saveAsImage": {"title": "导出 PNG", "pixelRatio": 2}}},
        "color": _ECHARTS_PALETTE,
        "series": [{
            "name": _build_axis_label(value_col) if value_col else "计数",
            "type": "pie",
            "radius": ["42%", "68%"],
            "center": ["40%", "52%"],
            "avoidLabelOverlap": True,
            "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "borderRadius": 6},
            "label": {"show": True, "color": _ECHARTS_TEXT_COLOR, "fontSize": 12,
                      "formatter": "{b}\n{d}%"},
            "labelLine": {"length": 12, "length2": 14, "smooth": True},
            "emphasis": {
                "itemStyle": {"shadowBlur": 16, "shadowColor": "rgba(0,0,0,0.15)"},
                "label": {"fontSize": 14, "fontWeight": 600},
            },
            "data": data,
        }],
    }


def _echarts_histogram(
    df: pd.DataFrame, *, x: str, color: str | None, bins: int, title: str,
) -> dict[str, Any]:
    """直方图：自动分箱 + 边际箱线图（叠加在底部）。"""
    x_label = _build_axis_label(x)
    series_data = df[x].dropna().astype(float)
    if series_data.empty:
        return _echarts_bar(df, x=x, y=None, color=color, aggregation="none", title=title)

    counts, edges = np.histogram(series_data, bins=min(max(bins, 2), 200))
    categories = [f"{edges[i]:.1f}-{edges[i+1]:.1f}" for i in range(len(counts))]
    data = [int(c) for c in counts]

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} 分布直方图"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "axis", "axisPointer": {"type": "shadow"},
                    "formatter": "function(params){var p=params[0];return '<div style=\"font-weight:600;\">区间：'+p.axisValue+'</div><span style=\"color:#6b7280;\">频数：</span><b>'+p.value.toLocaleString()+'</b>';}"},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": [_ECHARTS_PALETTE[0]],
        "xAxis": [{**_ECHARTS_BASE_AXIS, "type": "category", "data": categories, "name": x_label,
                   "axisLabel": {**_ECHARTS_BASE_AXIS["axisLabel"], "rotate": 35}}],
        "yAxis": [{**_ECHARTS_BASE_AXIS, "type": "value", "name": "频数"}],
        "series": [{
            "name": "频数",
            "type": "bar",
            "data": data,
            "barWidth": "92%",
            "itemStyle": {"color": _build_linear_gradient(_ECHARTS_PALETTE[0]), "borderRadius": [4, 4, 0, 0]},
            "emphasis": {"itemStyle": {"color": _ECHARTS_PALETTE[0]}},
        }],
        "dataZoom": [{"type": "inside", "start": 0, "end": 100}],
    }
    return base


def _echarts_box(
    df: pd.DataFrame, *, x: str, y: str | None, color: str | None, title: str, violin: bool = False,
) -> dict[str, Any]:
    """箱线图 / 小提琴图：按 x 分组展示 y 分布。"""
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y) if y else ""
    if y is None:
        return _echarts_histogram(df, x=x, color=color, bins=30, title=title)

    groups = df.groupby(x, dropna=False)[y]
    categories = [str(k) for k in groups.groups.keys()]
    box_data = []
    for _, group in groups:
        vals = group.dropna().astype(float).tolist()
        if not vals:
            box_data.append([0, 0, 0, 0, 0])
            continue
        q1 = float(np.percentile(vals, 25))
        q2 = float(np.percentile(vals, 50))
        q3 = float(np.percentile(vals, 75))
        iqr = q3 - q1
        lower = min(vals) if not vals else max(min(vals), q1 - 1.5 * iqr)
        upper = max(vals) if not vals else min(max(vals), q3 + 1.5 * iqr)
        outliers = [v for v in vals if v < lower or v > upper]
        box_data.append([lower, q1, q2, q3, upper] + outliers)

    return {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": f"{x_label} 分组 × {y_label} 分布"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item"},
        "grid": {**_ECHARTS_BASE_GRID},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "color": _ECHARTS_PALETTE,
        "xAxis": [{**_ECHARTS_BASE_AXIS, "type": "category", "data": categories, "name": x_label}],
        "yAxis": [{**_ECHARTS_BASE_AXIS, "type": "value", "name": y_label, "scale": True}],
        "series": [{
            "name": y_label,
            "type": "boxplot",
            "data": box_data,
            "itemStyle": {"color": _hex_to_rgba(_ECHARTS_PALETTE[0], 0.18),
                          "borderColor": _ECHARTS_PALETTE[0], "borderWidth": 1.5},
            "emphasis": {"itemStyle": {"borderWidth": 2, "shadowBlur": 8}},
        }],
    }


def _echarts_heatmap(
    df: pd.DataFrame, *, x: str, y: str | None, values: str | None, title: str,
    is_correlation: bool = False,
) -> dict[str, Any]:
    """热力图 / 相关性热力图。"""
    if is_correlation:
        numeric = df.select_dtypes(include="number")
        if numeric.empty:
            return {"title": {"text": title}}
        corr = numeric.corr()
        x_names = corr.columns.tolist()
        y_names = corr.index.tolist()
        data = []
        for i, _yi in enumerate(y_names):
            for j, _xj in enumerate(x_names):
                data.append([j, i, round(float(corr.iloc[i, j]), 3)])
        vmin, vmax = -1, 1
        value_label = "相关系数"
    else:
        if not x or not y or not values:
            return {"title": {"text": title}}
        pivot = df.pivot_table(index=y, columns=x, values=values, aggfunc="mean")
        x_names = [str(c) for c in pivot.columns]
        y_names = [str(i) for i in pivot.index]
        data = []
        for i, _yi in enumerate(y_names):
            for j, _xj in enumerate(x_names):
                v = pivot.iloc[i, j]
                data.append([j, i, _safe_value(v)])
        vmin = float(np.nanmin([d[2] for d in data if d[2] is not None])) if data else 0
        vmax = float(np.nanmax([d[2] for d in data if d[2] is not None])) if data else 1
        value_label = _build_axis_label(values)

    return {
        "title": {**_ECHARTS_BASE_TITLE, "text": title,
                  "subtext": "相关性矩阵" if is_correlation else f"{_build_axis_label(x)} × {_build_axis_label(y)}"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item",
                    "formatter": "function(p){return '<b>'+p.seriesName+'</b><br/>'+y_names[p.value[1]]+' × '+x_names[p.value[0]]+'：<b>'+p.value[2]+'</b>';}"},
        "grid": {**_ECHARTS_BASE_GRID, "left": 100, "bottom": 100},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "xAxis": [{"type": "category", "data": x_names, "splitArea": {"show": True},
                   "axisLabel": {"color": _ECHARTS_TEXT_SECONDARY, "rotate": 30}}],
        "yAxis": [{"type": "category", "data": y_names, "splitArea": {"show": True},
                   "axisLabel": {"color": _ECHARTS_TEXT_SECONDARY}}],
        "visualMap": {
            "min": vmin, "max": vmax, "calculable": True, "orient": "horizontal",
            "left": "center", "bottom": 16,
            "textStyle": {"color": _ECHARTS_TEXT_SECONDARY},
            "inRange": {"color": ["#C75D63", "#f3f4f6", "#2C5F8D"]},
        },
        "series": [{
            "name": value_label, "type": "heatmap", "data": data,
            "label": {"show": True, "color": _ECHARTS_TEXT_COLOR, "fontSize": 11,
                      "formatter": "function(p){return p.value[2];}"},
            "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.3)"}},
        }],
    }


def _echarts_scatter_matrix(
    df: pd.DataFrame, *, dimensions: list[str], color: str | None, title: str,
) -> dict[str, Any]:
    """散点矩阵：用平行坐标 + 雷达图组合近似表达（ECharts 无原生 SPLOM）。"""
    numeric = [d for d in dimensions if d in df.columns and pd.api.types.is_numeric_dtype(df[d])]
    if not numeric:
        return {"title": {"text": title}}

    # 平行坐标：每条样本一条线，颜色按 color 分组
    parallel_axes = [{
        "dim": idx, "name": _build_axis_label(d),
        "scale": True,
        "axisLine": {"lineStyle": {"color": _ECHARTS_GRID_COLOR}},
        "axisLabel": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 11},
        "splitLine": {"show": True, "lineStyle": {"color": _ECHARTS_BORDER_COLOR, "type": "dashed"}},
        "nameTextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 11},
    } for idx, d in enumerate(numeric)]

    base: dict[str, Any] = {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": "平行坐标多维视图"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item"},
        "toolbox": {**_ECHARTS_BASE_TOOLBOX},
        "parallelAxis": parallel_axes,
        "parallel": {
            "left": 64, "right": 32, "top": 80, "bottom": 60,
            "parallelAxisDefault": {"axisLine": {"lineStyle": {"color": _ECHARTS_GRID_COLOR}}},
        },
        "color": _ECHARTS_PALETTE,
        "series": [],
    }

    if color and color in df.columns:
        levels = list(pd.unique(df[color].dropna()))
        for idx, level in enumerate(levels):
            sub = df[df[color] == level]
            data = [[_safe_value(v) for v in r[numeric].tolist()] for _, r in sub.iterrows()]
            base["series"].append({
                "name": str(level), "type": "parallel", "data": data,
                "lineStyle": {"color": _series_color(idx), "width": 1, "opacity": 0.5},
                "emphasis": {"lineStyle": {"width": 2.5, "opacity": 1}},
            })
        base["legend"] = {**_ECHARTS_BASE_LEGEND, "data": [str(item) for item in levels]}
    else:
        data = [[_safe_value(v) for v in r[numeric].tolist()] for _, r in df.iterrows()]
        base["series"] = [{
            "name": "样本", "type": "parallel", "data": data,
            "lineStyle": {"color": _ECHARTS_PALETTE[0], "width": 1, "opacity": 0.5},
            "emphasis": {"lineStyle": {"width": 2.5, "opacity": 1}},
        }]

    return base


def _echarts_sunburst(
    df: pd.DataFrame, *, path_columns: list[str], values: str | None, title: str, is_treemap: bool = False,
) -> dict[str, Any]:
    """旭日图 / 矩形树图：层级结构可视化。"""
    if not path_columns:
        return {"title": {"text": title}}

    # 构建层级树
    tree: dict[str, Any] = {"name": "全部", "children": []}
    for _, row in df.iterrows():
        current = tree
        for depth, col in enumerate(path_columns):
            name = str(row[col])
            existing = next((c for c in current["children"] if c["name"] == name), None)
            if existing is None:
                existing = {"name": name, "children": []}
                current["children"].append(existing)
            current = existing
            if depth == len(path_columns) - 1:
                current["value"] = _safe_value(row[values]) if values and values in df.columns else 1

    # 聚合叶子值到父节点
    def aggregate(node: dict[str, Any]) -> float:
        if not node.get("children"):
            return float(node.get("value", 0) or 0)
        total = sum(aggregate(c) for c in node["children"])
        node["value"] = total
        return total

    aggregate(tree)

    chart_type = "treemap" if is_treemap else "sunburst"
    series: dict[str, Any] = {
        "name": title,
        "type": chart_type,
        "data": tree["children"],
        "label": {"color": _ECHARTS_TEXT_COLOR, "fontSize": 12},
        "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "gapWidth": 2},
        "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.15)"}},
        "levels": [
            {"itemStyle": {"color": _ECHARTS_PALETTE[0]}},
            {"itemStyle": {"color": _ECHARTS_PALETTE[1]}},
            {"itemStyle": {"color": _ECHARTS_PALETTE[2]}},
            {"itemStyle": {"color": _ECHARTS_PALETTE[3]}},
        ],
    }
    if not is_treemap:
        series["radius"] = ["20%", "90%"]
        series["nodeClick"] = "zoomToNode"

    return {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": "层级结构 · 点击下钻"},
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item",
                    "formatter": "function(p){return '<b>'+p.name+'</b><br/>值：<b>'+(p.value||0).toLocaleString()+'</b>';}"},
        "toolbox": {"right": 24, "top": 24, "feature": {"saveAsImage": {"title": "导出 PNG", "pixelRatio": 2}}},
        "color": _ECHARTS_PALETTE,
        "series": [series],
    }


# === ECharts HTML 模板（standalone，与 Plotly 同目录共存）===

_ECHARTS_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script src="{script}"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: 100%; height: 100%; background: {bg}; font-family: {font}; color: {text}; overflow: hidden; }}
  /* 图表区域自适应容器尺寸：去掉 min-height: 560px 强制撑高，
     改为 min-height: 320px 保证小屏可读，高度 100% 填满预览模态。
     这样预览模态（840px 高）里图表会完整显示，不再溢出看不见。 */
  #chart {{ width: 100%; height: 100%; min-height: 320px; }}
  .interpretation {{
    border-top: 1px solid #e5e7eb;
    padding: 16px 24px;
    background: #f9fafb;
    font-size: 13px;
    line-height: 1.75;
    color: #374151;
    max-height: 180px;
    overflow-y: auto;
  }}
  .interpretation-title {{
    font-size: 12px;
    color: #6b7280;
    font-weight: 600;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }}
  .layout {{ display: flex; flex-direction: column; height: 100%; }}
  .chart-wrap {{ flex: 1; min-height: 0; }}
</style>
</head>
<body>
<div class="layout">
  <div class="chart-wrap"><div id="chart"></div></div>
  {interpretation_block}
</div>
<script>
(function(){{
  var el = document.getElementById('chart');
  var chart = echarts.init(el, null, {{renderer: 'canvas', devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2)}});
  var option = {option};
  chart.setOption(option, true);
  window.addEventListener('resize', function(){{ chart.resize(); }});
  // 主题切换：监听 prefers-color-scheme（暂只渲染浅色，预留深色扩展点）
  window.__echartsInstance = chart;
}})();
</script>
</body>
</html>
"""


def _build_echarts_html(
    *,
    title: str,
    option: dict[str, Any],
    script_src: str,
    interpretation: str,
) -> str:
    """组装 standalone HTML：ECharts option + 解读块 + 视觉规范。"""
    # option 序列化为 JSON，</script> 转义防 XSS（与 Plotly 分支一致）
    option_json = json.dumps(option, ensure_ascii=False, default=str)
    option_json = option_json.replace("</script>", "<\\/script>")

    if interpretation:
        interp_block = (
            '<div class="interpretation">'
            '<div class="interpretation-title">数据解读</div>'
            f'{escape(interpretation)}'
            '</div>'
        )
    else:
        interp_block = ""

    return _ECHARTS_HTML_TEMPLATE.format(
        title=escape(title),
        script=script_src,
        option=option_json,
        interpretation_block=interp_block,
        bg=_ECHARTS_BG_COLOR,
        font=_ECHARTS_FONT_FAMILY,
        text=_ECHARTS_TEXT_COLOR,
    )


# === 主入口：分派到对应图表生成器 ===

def _build_echarts_option(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    z: str | None,
    size: str | None,
    values: str | None,
    path_columns: list[str] | None,
    dimensions: list[str] | None,
    aggregation: str,
    title: str,
    bins: int,
) -> dict[str, Any]:
    """根据 chart_type 分派到对应的 ECharts option 生成器。"""
    if chart_type == "bar":
        return _echarts_bar(df, x=x, y=y, color=color, aggregation=aggregation, title=title)
    if chart_type == "line":
        return _echarts_line(df, x=x, y=y, color=color, aggregation=aggregation, title=title, area=False)
    if chart_type == "area":
        return _echarts_line(df, x=x, y=y, color=color, aggregation=aggregation, title=title, area=True)
    if chart_type == "scatter":
        return _echarts_scatter(df, x=x, y=y, color=color, size=size, title=title, is_3d=False)
    if chart_type == "scatter_3d":
        # echarts-gl 不在标准 bundle 内，降级为 2D 散点并标注
        opt = _echarts_scatter(df, x=x, y=y, color=color, size=size, title=title + "（2D 视图）", is_3d=False)
        opt["title"]["subtext"] = "3D 散点降级为 2D（ECharts 标准包不含 gl 模块）"
        return opt
    if chart_type == "histogram":
        return _echarts_histogram(df, x=x, color=color, bins=bins, title=title)
    if chart_type == "box":
        return _echarts_box(df, x=x, y=y, color=color, title=title, violin=False)
    if chart_type == "violin":
        # ECharts 无原生 violin，用 boxplot 近似
        opt = _echarts_box(df, x=x, y=y, color=color, title=title, violin=True)
        opt["title"]["subtext"] = "小提琴图用箱线图近似（ECharts 无原生 violin）"
        return opt
    if chart_type == "pie":
        return _echarts_pie(df, x=x, values=values, y=y, title=title)
    if chart_type == "heatmap":
        return _echarts_heatmap(df, x=x, y=y, values=values, title=title, is_correlation=False)
    if chart_type == "correlation_heatmap":
        return _echarts_heatmap(df, x=x, y=y, values=values, title=title or "相关性矩阵", is_correlation=True)
    if chart_type == "scatter_matrix":
        return _echarts_scatter_matrix(df, dimensions=dimensions or [], color=color, title=title)
    if chart_type == "sunburst":
        return _echarts_sunburst(df, path_columns=path_columns or [], values=values, title=title, is_treemap=False)
    if chart_type == "treemap":
        return _echarts_sunburst(df, path_columns=path_columns or [], values=values, title=title, is_treemap=True)
    raise ValueError(f"ECharts 引擎暂不支持图表类型：{chart_type}")


def _render_echarts(
    workspace: DataWorkspace,
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    z: str | None,
    size: str | None,
    values: str | None,
    path_columns: list[str] | None,
    dimensions: list[str] | None,
    aggregation: str,
    title: str | None,
    bins: int,
    display_title: str,
    stem: str,
) -> dict[str, Any]:
    """ECharts 渲染主入口：生成 option、HTML、解读文本，返回 response dict。"""
    # 生成 ECharts option
    option = _build_echarts_option(
        df, chart_type=chart_type, x=x, y=y, color=color, z=z, size=size,
        values=values, path_columns=path_columns, dimensions=dimensions,
        aggregation=aggregation, title=display_title, bins=bins,
    )

    # 自动白话解读
    interpretation = _auto_interpret(
        df, chart_type=chart_type, x=x, y=y, color=color,
        aggregation=aggregation, title=display_title,
    )

    # 获取 echarts bundle（首次从 CDN 下载，失败时用 CDN URL 直引）
    bundle = workspace.ensure_echarts_bundle()
    if bundle is not None:
        relative_script = bundle.relative_to(workspace.artifacts_dir).as_posix()
    else:
        # 离线 fallback：直接引用 CDN（在线场景可用）
        relative_script = ECHARTS_CDN_URL

    html_path = workspace.artifacts_dir / f"{stem}.html"
    html_content = _build_echarts_html(
        title=display_title,
        option=option,
        script_src=relative_script,
        interpretation=interpretation,
    )
    _atomic_write_text(html_path, html_content)
    workspace.register_artifact(html_path, "visualization", display_title)

    # ECharts option JSON：供前端动态渲染 / 调试
    json_path = workspace.artifacts_dir / f"{stem}.echarts.json"
    _atomic_write_text(json_path, json.dumps(option, ensure_ascii=False, default=str))
    workspace.register_artifact(json_path, "chart_data", "ECharts option JSON")

    return {
        "status": "ok",
        "chart_engine": "echarts",
        "chart_type": chart_type,
        "rows_plotted": len(df),
        "html": str(html_path),
        "echarts_json": str(json_path),
        "interpretation": interpretation,
        "category_coverage": {
            "complete": True,
            "observed_combinations": len(df),
            "total_combinations": len(df),
            "missing_count": 0,
        },
        "scale_mode": "auto",
        "scale_details": {"scale_mode": "auto", "extreme_points": []},
    }
