"""ECharts 自动白话解读：纯数据驱动，不依赖 LLM。

从 ``echarts_engine.py`` 拆出。所有解读失败都必须在调用方被吞掉（返回空串），
不能影响图表生成——该兜底逻辑保留在门面模块 ``echarts_engine._auto_interpret``。
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from data_agent.density import (
    DensityView,
    density_note,
    format_number,
    hotspot_sentence,
    panels_sentence,
)
from data_agent.echarts_constants import _DENSITY_DETAIL_POINTS
from data_agent.render.primitives import _format_number, _format_time_categories
from data_agent.tools._helpers import _human_column_label as _build_axis_label
from data_agent.tools._helpers import _scatter_structure


def _interpret_impl(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str | None,
    density_view: DensityView | None = None,
    stacked: bool = False,
) -> str:
    title_text = title or f"{_build_axis_label(x) or ''}与{_build_axis_label(y) or ''}分布"
    if chart_type in {"bar", "line", "area"} and x and y and len(df) > 0:
        return _interpret_trend(
            df,
            chart_type=chart_type,
            x=x,
            y=y,
            color=color,
            aggregation=aggregation,
            title=title_text,
            stacked=stacked,
        )
    if chart_type == "pie" and x and len(df) > 0:
        return _interpret_pie(df, x=x, title=title_text)
    if chart_type == "scatter" and density_view is not None and x and y and len(df) > 0:
        return _density_interpretation(
            df, view=density_view, x=x, y=y, color=color, title=title_text
        )
    if chart_type in {"scatter", "scatter_3d"} and x and y and len(df) > 0:
        return _interpret_scatter(df, x=x, y=y, title=title_text, is_3d=chart_type == "scatter_3d")
    if chart_type in {"correlation_heatmap", "heatmap"} and len(df) > 0:
        return _interpret_heatmap(
            df, title=title_text, is_correlation=chart_type == "correlation_heatmap"
        )
    if chart_type in {"box", "violin"} and x and y and len(df) > 0:
        return _interpret_box(df, x=x, y=y, title=title_text)
    if chart_type in {"sunburst", "treemap"} and len(df) > 0:
        return _interpret_hierarchy(df, title=title_text)
    return f"本图展示了「{title_text}」的分布情况，可结合悬浮提示与图例交互深入查看各维度细节。"


def _interpret_trend(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str,
    y: str,
    color: str | None,
    aggregation: str,
    title: str,
    stacked: bool = False,
) -> str:
    agg_label = {
        "mean": "平均",
        "median": "中位",
        "sum": "合计",
        "count": "计数",
        "min": "最小",
        "max": "最大",
    }.get(aggregation, "")
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y)

    if color:
        # 分组场景：对比各系列总量与差异
        pivot = df.groupby(color)[y].sum() if y in df.columns else None
        if pivot is None or pivot.empty:
            grouping = "堆叠" if stacked else "分组"
            return f"「{title}」按{_build_axis_label(color)}{grouping}对比，悬浮可查看每组明细。"
        top_series = pivot.idxmax()
        top_val = float(pivot.max())
        low_series = pivot.idxmin()
        low_val = float(pivot.min())
        ratio = top_val / low_val if low_val > 0 else float("inf")
        grouping = "堆叠" if stacked else "分组"
        return (
            f"「{title}」按{_build_axis_label(color)}{grouping}，{top_series}累计最高"
            f"（{_format_number(top_val)}），{low_series}最低（{_format_number(low_val)}），"
            f"前者约为后者的{ratio:.1f}倍。点击图例可隐藏系列聚焦对比，框选区域可放大查看。"
        )

    # 无分组：找最高最低、识别拐点；时间类目标签与轴一致（去 00:00:00 后缀）
    _time_labels = _format_time_categories(df[x])
    _label_of = (
        dict(zip((str(v) for v in df[x].tolist()), _time_labels, strict=False))
        if _time_labels
        else {}
    )

    def _fmt_x(v: Any) -> Any:
        return _label_of.get(str(v), v)

    if chart_type == "bar":
        sorted_df = df.sort_values(y, ascending=False)
        top_row = sorted_df.iloc[0]
        low_row = sorted_df.iloc[-1]
        mean_val = float(df[y].mean())
        diff_pct = (float(top_row[y]) - float(low_row[y])) / max(abs(float(low_row[y])), 1e-9) * 100
        return (
            f"「{title}」中{x_label}「{_fmt_x(top_row[x])}」的{agg_label}{y_label}最高"
            f"（{_format_number(top_row[y])}），「{_fmt_x(low_row[x])}」最低"
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
            peak_x = _fmt_x(df.iloc[max_diff_idx][x])
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


def _interpret_scatter(df: pd.DataFrame, *, x: str, y: str, title: str, is_3d: bool = False) -> str:
    if not pd.api.types.is_numeric_dtype(df[x]) or not pd.api.types.is_numeric_dtype(df[y]):
        return f"「{title}」展示{_build_axis_label(x)}与{_build_axis_label(y)}的分布关系，悬浮查看每个点明细。"
    corr = float(df[[x, y]].corr().iloc[0, 1])
    # 3D 图无框选功能，交互提示必须区分，避免误导用户找不到操作
    hint = (
        "拖拽旋转视角、滚轮缩放可从不同角度观察分布。"
        if is_3d
        else "滚轮缩放可查看密集区域，框选可隔离离群点。"
    )
    if math.isnan(corr):
        return f"「{title}」展示两变量分布，悬浮查看每个点明细，" + (
            "拖拽旋转视角可从不同角度观察。" if is_3d else "框选可放大区域。"
        )
    direction = "正向" if corr > 0 else "反向"
    strength = "强" if abs(corr) > 0.7 else "中等" if abs(corr) > 0.4 else "弱"
    return f"「{title}」呈现{direction}{strength}相关（r={corr:.2f}），共{len(df)}个点。{hint}"


def _interpret_heatmap(df: pd.DataFrame, *, title: str, is_correlation: bool = False) -> str:
    generic = (
        f"「{title}」以颜色深浅表达数值大小，颜色越深数值越高。"
        f"悬浮单元格查看精确值，适用于矩阵型数据的整体模式识别。"
    )
    if not is_correlation:
        return generic
    # 相关性矩阵：找出最强相关对，给出业务化提示
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        return generic
    corr = numeric.corr()
    cols = list(corr.columns)
    pairs: list[tuple[str, str, float]] = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = float(corr.iloc[i, j])
            if not math.isnan(v):
                pairs.append((str(cols[i]), str(cols[j]), v))
    if not pairs:
        return generic
    strongest = max(pairs, key=lambda p: abs(p[2]))
    a, b, v = strongest
    msg = (
        f"「{title}」中「{_build_axis_label(a)}」与「{_build_axis_label(b)}」的相关性最强"
        f"（r={v:.2f}，{'正' if v > 0 else '负'}相关）。"
    )
    neg = min(pairs, key=lambda p: p[2])
    if neg is not strongest and neg[2] < -0.3:
        msg += (
            f"「{_build_axis_label(neg[0])}」与「{_build_axis_label(neg[1])}」呈明显负相关"
            f"（r={neg[2]:.2f}）。"
        )
    return msg + "红色代表正相关、蓝色代表负相关，颜色越深关系越强，悬浮单元格查看精确值。"


def _interpret_box(df: pd.DataFrame, *, x: str, y: str, title: str) -> str:
    y_label = _build_axis_label(y)
    generic = (
        f"「{title}」对比各组{y_label}的分布，箱体表示中间 50% 数据，"
        f"须线延伸至1.5倍四分位距，超出须线的点为潜在异常值。悬浮查看分位数细节。"
    )
    # 数据驱动：对比各组中位数，统计离群点总数（与图中 scatter 系列口径一致）
    grouped = df.assign(_v=pd.to_numeric(df[y], errors="coerce")).groupby(x)["_v"]
    medians = grouped.median().dropna()
    if len(medians) < 2:
        return generic
    top_key, low_key = medians.idxmax(), medians.idxmin()
    outlier_count = 0
    for _, group in grouped:
        vals = group.dropna().astype(float)
        if len(vals) < 4:
            continue
        q1, q3 = float(vals.quantile(0.25)), float(vals.quantile(0.75))
        iqr = q3 - q1
        outlier_count += int(((vals < q1 - 1.5 * iqr) | (vals > q3 + 1.5 * iqr)).sum())
    outlier_text = (
        f"共识别出 {outlier_count} 个离群点（超出 1.5 倍四分位距，图中以独立圆点标出）"
        if outlier_count
        else "各组未发现明显离群点"
    )
    return (
        f"「{title}」中「{top_key}」的{y_label}中位数最高（{_format_number(medians.max())}），"
        f"「{low_key}」最低（{_format_number(medians.min())}），{outlier_text}。"
        f"箱体为中间 50% 数据，菱形标记为均值，悬浮可查看五数概括与样本数。"
    )


def _interpret_hierarchy(df: pd.DataFrame, *, title: str) -> str:
    return f"「{title}」以层级方式展示数据结构，点击节点可下钻/上卷，面积大小反映对应数值占比。"


def _density_interpretation(
    df: pd.DataFrame,
    *,
    view: DensityView,
    x: str,
    y: str,
    color: str | None,
    title: str,
) -> str:
    """密度视图的白话解读：讲清"颜色读什么"＋把算出来的锚点写成业务句子。

    点云图原本的解读（"滚轮缩放可查看密集区域，框选可隔离离群点"）对密度图
    不成立——没有可框选的离散点，颜色也不表示数值大小。这里改成：相关性一句
    话 + 密度编码说明 + 最密集区域 + 分面差异 + 正确的交互提示，与 Plotly
    分支的 ``density_plotly.density_interpretation`` 同口径。
    """
    x_label = _build_axis_label(x)
    y_label = _build_axis_label(y)
    color_label = _build_axis_label(color) if color else None
    pair = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    structure = (
        _scatter_structure(pair[x].astype(float).tolist(), pair[y].astype(float).tolist())
        if len(pair) >= 3
        else None
    )
    r_value = structure.get("r") if structure else None
    parts: list[str] = []
    if r_value is not None:
        direction = "正向" if r_value > 0 else "反向"
        strength = "强" if abs(r_value) > 0.7 else "中等" if abs(r_value) > 0.4 else "弱"
        parts.append(
            f"「{title}」共 {view.rows_used:,} 条记录，呈{direction}{strength}相关"
            f"（r={r_value:.2f}）。"
        )
    else:
        parts.append(f"「{title}」共 {view.rows_used:,} 条记录")
    parts.append(density_note(view, color_label=color_label))
    hotspot = hotspot_sentence(view, x_label, y_label)
    if hotspot:
        parts.append(hotspot)
    if color_label:
        panels = panels_sentence(view, color_label)
        if panels:
            parts.append(panels)
    parts.append(
        "颜色越深表示该区域记录越密集（色标给出每格记录数区间）；"
        "滚轮可放大局部，悬浮查看每格覆盖范围与记录数。"
    )
    if not view.is_faceted:
        # 与 Plotly 分支同一句话：ECharts 用图例开关（点图例里的名字显示/隐藏），
        # 不说清楚没人会知道那层点是可以打开的。
        parts.append(
            f"放大后若想看具体记录，点图例中的「抽样原始点（"
            f"{format_number(_DENSITY_DETAIL_POINTS)} 条）」即可叠加显示"
            "（完整数据在同名 JSON 产物里）。"
        )
    return "".join(parts)
