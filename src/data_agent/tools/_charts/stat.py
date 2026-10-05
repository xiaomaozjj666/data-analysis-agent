"""大数据统计图服务端聚合：直方图分箱、箱线图五数概括。

从 ``charts.py`` 拆分而来。问题：直方图与箱线图的"形状"完全由数据的分布
决定，但 px 会把**全部原始数值**塞进 HTML 交给 plotly.js 现算——30 万行时
直方图 HTML 实测 3.2MB、箱线图 5.2MB，iframe 解析慢，产物卡缩略图甚至
渲染不出来（空白卡片）。

做法：超过阈值后改为服务端聚合，视觉结果与全量完全一致，载荷从 MB 降到 KB：
  - 直方图：numpy 分箱 → 每箱计数（坐标轴仍然是真实记录数，不是抽样估计）；
  - 箱线图：五数概括 + 1.5×IQR 须端点（plotly 支持直接传 q1/median/q3/
    lowerfence/upperfence，画出来的箱体与全量逐点绘制逐像素一致）；
  - 小提琴图：核密度只关心形状，取分层抽样（完整数据仍在 .plotly.json 里）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .._helpers import _compact_number, _plotly_axis_tickformat
from .._helpers import _human_column_label as _hl


def _stat_tick_format(low: float, high: float) -> str:
    return _plotly_axis_tickformat((float(low), float(high)))


def _plotly_binned_histogram(
    df: pd.DataFrame,
    *,
    x: str,
    bins: int,
    colors: list[str],
    color: str | None = None,
    labels: dict[str, str] | None = None,
    title: str | None = None,
) -> tuple[go.Figure, dict[str, Any]] | None:
    """服务端分箱的直方图；``x`` 非数值或无法分箱时返回 ``None``（回退 px）。"""
    values = pd.to_numeric(df[x], errors="coerce")
    finite = values.to_numpy(dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size < 2:
        return None
    bin_count = int(max(2, min(bins, 200)))
    edges = np.histogram_bin_edges(finite, bins=bin_count)
    if len(edges) < 2 or not np.isfinite(edges).all() or edges[-1] <= edges[0]:
        # pragma: no cover - 两个以上有限值时 histogram_bin_edges 必给出递增边界
        return None
    centers = (edges[:-1] + edges[1:]) / 2.0
    width = float(edges[1] - edges[0])
    number_format = _stat_tick_format(edges[0], edges[-1])
    x_label = (labels or {}).get(x, _hl(x))
    total = int(finite.size)

    groups: list[tuple[str, np.ndarray]] = []
    if color and color in df.columns:
        text = df[color].astype(str)
        seen: list[str] = []
        for level in text.tolist():
            if level not in seen:
                seen.append(level)
        for level in seen:
            chunk = values[text == level].to_numpy(dtype=float)
            chunk = chunk[np.isfinite(chunk)]
            if chunk.size:
                groups.append((level, chunk))
    if not groups:
        groups = [(x_label, finite)]

    fig = go.Figure()
    for index, (name, chunk) in enumerate(groups):
        counts, _ = np.histogram(chunk, bins=edges)
        custom = np.stack(
            [
                np.round(edges[:-1], 6),
                np.round(edges[1:], 6),
                counts.astype(float),
                np.round(counts / total * 100, 3),
            ],
            axis=-1,
        )
        fig.add_trace(
            go.Bar(
                x=centers,
                y=counts,
                width=width,
                name=name,
                marker={"color": colors[index % len(colors)], "line": {"width": 0}},
                customdata=custom,
                hovertemplate=(
                    f"{x_label} %{{customdata[0]:{number_format}}} ~ %{{customdata[1]:{number_format}}}"
                    "<br>记录数 %{customdata[2]:,.0f}（占 %{customdata[3]:.2f}%）"
                    f"<extra>{name}</extra>"
                ),
            )
        )
    fig.update_layout(
        barmode="relative",
        bargap=0.02,
        title={"text": title, "x": 0.01, "xanchor": "left"} if title else None,
    )
    # 中位数参考线替代原来的边缘箱线图：分位数信息保留，载荷几乎为零。
    median = float(np.median(finite))
    fig.add_vline(
        x=median,
        line_dash="dash",
        line_color="#9aa0a6",
        line_width=1.2,
        annotation_text=f"中位数 {_compact_number(median)}",
        annotation_position="top right",
        annotation_font={"size": 10, "color": "#6b7280"},
    )
    return fig, {"total": total, "median": median, "bins": len(centers)}


def _box_stats(values: np.ndarray) -> dict[str, float] | None:
    """一组数值的五数概括与 1.5×IQR 须端点（与 plotly 逐点绘制口径一致）。"""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    q1, median, q3 = (float(value) for value in np.percentile(finite, [25, 50, 75]))
    iqr = q3 - q1
    lower_fence, upper_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = finite[(finite >= lower_fence) & (finite <= upper_fence)]
    whisker_low = float(inside.min()) if inside.size else q1
    whisker_high = float(inside.max()) if inside.size else q3
    outliers = int(finite.size - inside.size)
    return {
        "q1": q1,
        "median": median,
        "q3": q3,
        "lowerfence": whisker_low,
        "upperfence": whisker_high,
        "mean": float(finite.mean()),
        "n": int(finite.size),
        "outliers": outliers,
    }


def _plotly_box_from_stats(
    df: pd.DataFrame,
    *,
    x: str | None,
    y: str,
    colors: list[str],
    color: str | None = None,
    labels: dict[str, str] | None = None,
    title: str | None = None,
) -> tuple[go.Figure, dict[str, Any]] | None:
    """服务端五数概括的箱线图；``y`` 非数值时返回 ``None``（回退 px）。"""
    values = pd.to_numeric(df[y], errors="coerce")
    numeric = values.to_numpy(dtype=float)
    if np.isfinite(numeric).sum() < 2:
        return None
    y_label = (labels or {}).get(y, _hl(y))

    keys: list[str] = [column for column in (x, color) if column and column in df.columns]
    if not keys:
        stats = _box_stats(numeric)
        if stats is None:
            return None
        groups: list[tuple[str, dict[str, float]]] = [(y_label, stats)]
    else:
        text = df[keys[0]].astype(str)
        if len(keys) == 2:
            text = text + " / " + df[keys[1]].astype(str)
        seen: list[str] = []
        for level in text.tolist():
            if level not in seen:
                seen.append(level)
        groups = []
        for level in seen:
            stats = _box_stats(values[text == level].to_numpy(dtype=float))
            if stats is not None:
                groups.append((level, stats))
    if not groups:  # pragma: no cover - y 有有限值即至少归入一个分组
        return None

    fig = go.Figure()
    total_outliers = 0
    lows = [stats["lowerfence"] for _, stats in groups]
    highs = [stats["upperfence"] for _, stats in groups]
    value_format = _stat_tick_format(min(lows), max(highs))
    for index, (name, stats) in enumerate(groups):
        total_outliers += stats["outliers"]
        fig.add_trace(
            go.Box(
                # x 必须显式给出（一条轨迹一个类别位置）：预计算统计量时每条轨迹
                # 只有 1 个"样本"，不给 x 的话所有箱体会叠在同一个刻度上
                # （实测四组箱体全部挤在 x=0）。
                x=[name],
                name=name,
                q1=[stats["q1"]],
                median=[stats["median"]],
                q3=[stats["q3"]],
                lowerfence=[stats["lowerfence"]],
                upperfence=[stats["upperfence"]],
                mean=[stats["mean"]],
                boxpoints=False,
                marker={"color": colors[index % len(colors)]},
                customdata=[
                    [stats["n"], stats["outliers"], stats["q1"], stats["median"], stats["q3"]]
                ],
                hovertemplate=(
                    "样本 %{customdata[0]:,.0f} 条（须线外 %{customdata[1]:,.0f}）"
                    f"<br>{y_label} 下四分位 %{{customdata[2]:{value_format}}}"
                    f"<br>中位数 %{{customdata[3]:{value_format}}}"
                    f"<br>上四分位 %{{customdata[4]:{value_format}}}"
                    f"<extra>{name}</extra>"
                ),
            )
        )
    fig.update_layout(
        boxmode="group", title={"text": title, "x": 0.01, "xanchor": "left"} if title else None
    )
    total = sum(stats["n"] for _, stats in groups)
    return fig, {"total": total, "outliers": total_outliers, "groups": len(groups)}
