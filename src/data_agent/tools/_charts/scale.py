"""极端值尺度控制：检测并处理压缩主体分布的极端值。

从 ``charts.py`` 拆分而来，负责在少数极端值破坏图表可读性时提供
"主体尺度 / 全量视图"切换按钮。原始数据不修改，仅控制默认视口。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .._helpers import _compact_number, _nice_ticks
from .annotate import _append_title_note


def _severe_axis_compression(
    values: list[Any], *, include_zero: bool = False
) -> dict[str, Any] | None:
    """Return a readable axis range when a few values collapse the main distribution.

    The plotted data is never modified. The returned range only controls the default
    viewport; the chart also exposes a full-range toggle.
    """
    numeric = pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce")
    numeric = numeric[np.isfinite(numeric)]
    if len(numeric) < 5 or numeric.nunique() < 3:
        return None
    q1, q3 = numeric.quantile([0.25, 0.75])
    spread = float(q3 - q1)
    if not np.isfinite(spread) or spread <= 0:
        median = float(numeric.median())
        deviations = (numeric - median).abs()
        mad = float(deviations.median())
        if mad <= 0 or not np.isfinite(mad):
            return None
        # Q1==Q3 且 MAD>0 在常见分位插值下不可达（中间 50% 同值时偏差中位数恒为 0），
        # 该分支仅作为数值健壮性兜底。
        lower_fence, upper_fence = (
            median - 8 * mad,
            median + 8 * mad,
        )  # pragma: no cover - 数学上不可达
    else:
        # Three IQRs is deliberately conservative: ordinary high performers stay
        # visible, while only scale-destroying points trigger the alternate view.
        lower_fence, upper_fence = float(q1 - 3 * spread), float(q3 + 3 * spread)
    normal_mask = numeric.between(lower_fence, upper_fence)
    normal = numeric[normal_mask]
    extreme = numeric[~normal_mask]
    if extreme.empty or len(normal) < 3 or len(extreme) > max(3, int(len(numeric) * 0.2)):
        return None
    normal_min, normal_max = float(normal.min()), float(normal.max())
    full_min, full_max = float(numeric.min()), float(numeric.max())
    normal_span = normal_max - normal_min
    reference = max(normal_span, abs(float(normal.median())) * 0.25, 1.0)
    if (full_max - full_min) / reference < 8:
        return None
    # 用 nice ticks 对齐视口边界到圆数（1/2/5/10 倍数），
    # 避免 padding 算出 123.456 这种不圆的边界。
    nice_min, nice_max, _ = _nice_ticks(normal_min, normal_max, n=5)
    lower = nice_min
    upper = nice_max
    if include_zero and normal_min >= 0:
        lower = 0.0
    return {
        "lower": float(lower),
        "upper": float(upper),
        "extreme_count": int(len(extreme)),
    }


def _trace_axis_values(fig: go.Figure, axis: str) -> list[Any]:
    values: list[Any] = []
    for trace in fig.data:
        if getattr(trace, "name", None) == "极端值提示":
            continue
        raw = getattr(trace, axis, None)
        if raw is not None:
            values.extend(list(raw))
    return values


def _apply_outlier_scale_controls(
    fig: go.Figure,
    chart_type: str,
    scale_mode: str,
) -> dict[str, Any]:
    """Add honest robust/full viewport controls when extreme values ruin readability."""
    if scale_mode == "full" or chart_type not in {"bar", "line", "area", "scatter"}:
        fig.update_layout(meta={"scale_mode": "full", "extreme_points": 0})
        return {"scale_mode": "full", "extreme_points": 0, "axis_ranges": {}}

    x_guard = None
    if chart_type == "scatter":
        x_guard = _severe_axis_compression(_trace_axis_values(fig, "x"))
    y_guard = _severe_axis_compression(
        _trace_axis_values(fig, "y"),
        include_zero=chart_type == "bar",
    )
    if not x_guard and not y_guard:
        fig.update_layout(meta={"scale_mode": "full", "extreme_points": 0})
        return {"scale_mode": "full", "extreme_points": 0, "axis_ranges": {}}

    # 性能优化：原实现对每个数据点单独 pd.to_numeric(pd.Series([raw_x]))，
    # 1M 点会创建 2M 个 1 元素 Series 对象，GC 压力极大实测卡死数十秒。
    # 改为每个 trace 批量 to_numeric 一次，然后用 numpy 向量化比较找极端点。
    indicator_text: list[str] = []
    for trace in fig.data:
        xs = list(trace.x) if getattr(trace, "x", None) is not None else []
        ys = list(trace.y) if getattr(trace, "y", None) is not None else []
        if not xs and not ys:
            continue
        # 批量转换，避免逐点创建 Series。
        nx = (
            pd.to_numeric(pd.Series(xs, dtype="object"), errors="coerce").to_numpy()
            if xs
            else np.array([])
        )
        ny = (
            pd.to_numeric(pd.Series(ys, dtype="object"), errors="coerce").to_numpy()
            if ys
            else np.array([])
        )
        # 预计算极端点 mask，向量化避免 Python 层循环。
        if x_guard and len(nx) > 0:
            x_extreme_mask = np.isfinite(nx) & ((nx < x_guard["lower"]) | (nx > x_guard["upper"]))
        else:
            x_extreme_mask = (
                np.zeros(len(nx), dtype=bool) if len(nx) > 0 else np.array([], dtype=bool)
            )
        if y_guard and len(ny) > 0:
            y_extreme_mask = np.isfinite(ny) & ((ny < y_guard["lower"]) | (ny > y_guard["upper"]))
        else:
            y_extreme_mask = (
                np.zeros(len(ny), dtype=bool) if len(ny) > 0 else np.array([], dtype=bool)
            )
        # 取并集；zip 长度按较短者截断，与原 zip 行为一致。
        n = min(len(x_extreme_mask), len(y_extreme_mask))
        extreme_indices = np.where(x_extreme_mask[:n] | y_extreme_mask[:n])[0]
        for i in extreme_indices:
            raw_x = xs[i] if i < len(xs) else None
            numeric_x = nx[i] if i < len(nx) else np.nan
            numeric_y = ny[i] if i < len(ny) else np.nan
            details = []
            if pd.notna(numeric_x) and isinstance(raw_x, (int, float, np.number)):
                details.append(f"x={_compact_number(float(numeric_x))}")
            elif raw_x is not None:
                details.append(str(raw_x))
            if pd.notna(numeric_y):
                details.append(f"真实值={_compact_number(float(numeric_y))}")
            indicator_text.append("<br>".join(details))

    if indicator_text:
        visible_details = indicator_text[:3]
        remaining = len(indicator_text) - len(visible_details)
        callout = "<b>极端值超出主体尺度</b><br>" + "<br>".join(
            value.replace("<br>", " · ") for value in visible_details
        )
        if remaining > 0:
            callout += f"<br>另有 {remaining} 个极端点"
        fig.add_annotation(
            xref="paper",
            yref="paper",
            x=0.012,
            y=0.985,
            xanchor="left",
            yanchor="top",
            text=callout,
            showarrow=False,
            align="left",
            bgcolor="rgba(255,250,245,0.94)",
            bordercolor="#D97745",
            borderwidth=1,
            borderpad=8,
            font={"size": 11, "color": "#7A3728"},
        )

    robust_layout: dict[str, Any] = {}
    full_layout: dict[str, Any] = {}
    axis_ranges: dict[str, list[float]] = {}
    if x_guard:
        robust_layout.update(
            {"xaxis.autorange": False, "xaxis.range": [x_guard["lower"], x_guard["upper"]]}
        )
        full_layout["xaxis.autorange"] = True
        axis_ranges["x"] = [x_guard["lower"], x_guard["upper"]]
        fig.update_xaxes(range=axis_ranges["x"], autorange=False)
    if y_guard:
        robust_layout.update(
            {"yaxis.autorange": False, "yaxis.range": [y_guard["lower"], y_guard["upper"]]}
        )
        full_layout["yaxis.autorange"] = True
        axis_ranges["y"] = [y_guard["lower"], y_guard["upper"]]
        fig.update_yaxes(range=axis_ranges["y"], autorange=False)

    indicator_count = max(
        x_guard["extreme_count"] if x_guard else 0,
        y_guard["extreme_count"] if y_guard else 0,
    )
    fig.update_layout(
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "x": 1,
                "xanchor": "right",
                "y": 1.16,
                "yanchor": "top",
                "showactive": True,
                "active": 0,
                "buttons": [
                    {"label": "主体尺度", "method": "relayout", "args": [robust_layout]},
                    {"label": "全量视图", "method": "relayout", "args": [full_layout]},
                ],
                "bgcolor": "#FFFFFF",
                "bordercolor": "#CBD5D1",
                "font": {"size": 12, "color": "#245C55"},
            }
        ],
        meta={
            "scale_mode": "robust",
            "extreme_points": indicator_count,
            "axis_ranges": axis_ranges,
        },
    )
    _append_title_note(
        fig,
        f"检测到 {indicator_count} 个极端点；默认显示主体尺度，"
        "右上角可切换全量视图。原始数据未修改",
    )
    return {"scale_mode": "robust", "extreme_points": indicator_count, "axis_ranges": axis_ranges}
