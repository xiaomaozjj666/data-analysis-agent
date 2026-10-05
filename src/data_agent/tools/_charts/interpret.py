"""Plotly 分支白话解读：基于聚合结果生成业务可读的数据解读文案。

从 ``charts.py`` 拆分而来，与 ECharts ``_auto_interpret`` 语义对齐，
让双引擎都有"数据解读"区块，避免体验分裂。
"""

from __future__ import annotations

import math as _math

import pandas as pd

from .._helpers import _compact_number
from .._helpers import _human_column_label as _hl


def _plotly_auto_interpret(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str | None,
) -> str:
    """基于聚合结果生成一段业务白话解读（Plotly 分支专用）。

    与 ECharts 的 _auto_interpret 语义对齐，避免双引擎体验分裂。
    """
    try:
        title_text = title or f"{_hl(x) or ''}与{_hl(y) or ''}分布"
        if chart_type in {"bar", "line", "area"} and x and y and len(df) > 0:
            return _plotly_interpret_trend(
                df,
                chart_type=chart_type,
                x=x,
                y=y,
                color=color,
                aggregation=aggregation,
                title=title_text,
            )
        if chart_type == "pie" and x and len(df) > 0:
            return _plotly_interpret_pie(df, x=x, title=title_text)
        if chart_type in {"scatter", "scatter_3d"} and x and y and len(df) > 0:
            return _plotly_interpret_scatter(
                df, x=x, y=y, title=title_text, is_3d=chart_type == "scatter_3d"
            )
        if chart_type in {"correlation_heatmap", "heatmap"} and len(df) > 0:
            return _plotly_interpret_heatmap(title=title_text)
        if chart_type in {"box", "violin"} and x and y and len(df) > 0:
            return _plotly_interpret_box(x=x, y=y, title=title_text)
        if chart_type in {"sunburst", "treemap"} and len(df) > 0:
            return _plotly_interpret_hierarchy(title=title_text)
        return f"本图展示了「{title_text}」的分布情况，可结合悬浮提示与图例交互深入查看各维度细节。"
    except Exception:
        return ""


def _plotly_interpret_trend(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str,
    y: str,
    color: str | None,
    aggregation: str,
    title: str,
) -> str:
    agg_label = {
        "mean": "平均",
        "median": "中位",
        "sum": "合计",
        "count": "计数",
        "min": "最小",
        "max": "最大",
    }.get(aggregation, "")
    x_label = _hl(x)
    y_label = _hl(y)

    if color:
        pivot = df.groupby(color)[y].sum() if y in df.columns else None
        if pivot is None or pivot.empty:
            return f"「{title}」按{_hl(color)}分组对比，悬浮可查看每组明细。"
        top_series = pivot.idxmax()
        top_val = float(pivot.max())
        low_series = pivot.idxmin()
        low_val = float(pivot.min())
        ratio = top_val / low_val if low_val > 0 else float("inf")
        return (
            f"「{title}」按{_hl(color)}分组，{top_series}累计最高"
            f"（{_compact_number(top_val)}），{low_series}最低（{_compact_number(low_val)}），"
            f"前者约为后者的{ratio:.1f}倍。点击图例可隐藏系列聚焦对比，框选区域可放大查看。"
        )

    if chart_type == "bar":
        sorted_df = df.sort_values(y, ascending=False)
        top_row = sorted_df.iloc[0]
        low_row = sorted_df.iloc[-1]
        mean_val = float(df[y].mean())
        diff_pct = (float(top_row[y]) - float(low_row[y])) / max(abs(float(low_row[y])), 1e-9) * 100
        return (
            f"「{title}」中{x_label}「{top_row[x]}」的{agg_label}{y_label}最高"
            f"（{_compact_number(float(top_row[y]))}），「{low_row[x]}」最低"
            f"（{_compact_number(float(low_row[y]))}），两者相差{diff_pct:.0f}%，"
            f"整体均值约{_compact_number(mean_val)}。鼠标悬浮查看每项明细。"
        )

    series = df[y].astype(float).reset_index(drop=True)
    if len(series) >= 3:
        diffs = series.diff().abs()
        max_diff_idx = int(diffs.idxmax())
        if 0 < max_diff_idx < len(series):
            before = series.iloc[max_diff_idx - 1]
            after = series.iloc[max_diff_idx]
            direction = "上升" if after > before else "下降"
            peak_x = df.iloc[max_diff_idx][x]
            return (
                f"「{title}」在{x_label}「{peak_x}」处出现明显拐点（{direction}"
                f"{_compact_number(abs(after - before))}），峰值"
                f"{_compact_number(float(series.max()))}，谷值{_compact_number(float(series.min()))}。"
                f"底部滑块可缩放区间细看趋势。"
            )
    return (
        f"「{title}」整体{y_label}在{_compact_number(float(series.min()))}到"
        f"{_compact_number(float(series.max()))}之间波动，均值约{_compact_number(float(series.mean()))}。"
    )


def _plotly_interpret_pie(df: pd.DataFrame, *, x: str, title: str) -> str:
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
    structure = "高度集中" if top_pct > 60 else "相对均衡" if top_pct < 30 else "适度集中"
    return (
        f"「{title}」中「{top[x]}」占比最高（{top_pct:.1f}%），「{low[x]}」最低"
        f"（{low_pct:.1f}%），结构{structure}。点击图例可隐藏某类，重新计算其余占比。"
    )


def _plotly_interpret_scatter(
    df: pd.DataFrame, *, x: str, y: str, title: str, is_3d: bool = False
) -> str:
    if not pd.api.types.is_numeric_dtype(df[x]) or not pd.api.types.is_numeric_dtype(df[y]):
        return f"「{title}」展示{_hl(x)}与{_hl(y)}的分布关系，悬浮查看每个点明细。"
    corr = float(df[[x, y]].corr().iloc[0, 1])
    # 与 ECharts 分支一致：3D 图无框选，交互提示区分维度
    hint = (
        "拖拽旋转视角、滚轮缩放可从不同角度观察分布。"
        if is_3d
        else "滚轮缩放可查看密集区域，框选可隔离离群点。"
    )
    if _math.isnan(corr):
        return f"「{title}」展示两变量分布，悬浮查看每个点明细，" + (
            "拖拽旋转视角可从不同角度观察。" if is_3d else "框选可放大区域。"
        )
    direction = "正向" if corr > 0 else "反向"
    strength = "强" if abs(corr) > 0.7 else "中等" if abs(corr) > 0.4 else "弱"
    return f"「{title}」呈现{direction}{strength}相关（r={corr:.2f}），共{len(df)}个点。{hint}"


def _plotly_interpret_heatmap(*, title: str) -> str:
    return (
        f"「{title}」以颜色深浅表达数值大小，颜色越深数值越高。"
        f"悬浮单元格查看精确值，适用于矩阵型数据的整体模式识别。"
    )


def _plotly_interpret_box(*, x: str, y: str, title: str) -> str:
    return (
        f"「{title}」对比各组{_hl(y)}的分布，箱体表示中间50%数据，"
        f"须线延伸至1.5倍四分位距，超出须线的点为潜在异常值。悬浮查看分位数细节。"
    )


def _plotly_interpret_hierarchy(*, title: str) -> str:
    return f"「{title}」以层级方式展示数据结构，点击节点可下钻/上卷，面积大小反映对应数值占比。"
