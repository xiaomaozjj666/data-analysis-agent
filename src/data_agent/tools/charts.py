"""图表生成辅助函数：聚合、标注、尺度控制、标题清理与文件名生成。

这些函数被 ``builder.build_tools`` 中的 ``create_visualization`` 工具调用，
本身不绑定 workspace，可独立测试。
"""

from __future__ import annotations

import math as _math
import re
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from ._helpers import _compact_number
from ._helpers import _human_column_label as _hl

# ---------------------------------------------------------------------------
# 命名常量（图表相关）
# ---------------------------------------------------------------------------

#: 分组图表 reindex 笛卡尔积上限。50×50 = 2500 单元格已足够，
#: 超过此值图表不可读且 reindex 主导内存。调用方仅看到实际观测组合。
_MAX_REINDEX_COMBINATIONS = 2_500

#: 图表标题清理后的最大字符数，避免前端溢出。
_CHART_TITLE_MAX_CHARS = 30

_BOOLEAN_VALUE_LABELS = {False: "未退货", True: "已退货"}
_SAMPLE_COUNT_COLUMN = "__sample_count__"
_HAS_RECORDS_COLUMN = "__has_records__"
#: 预计算的 hover 文本列，避免无记录 bar 在 hover 中显示 nan。
_HOVER_TEXT_COLUMN = "__hover_text__"

# 图表类型的中文短名，用于生成"柱状图_01"这种简洁文件名。
# 用户在前端看到的产物名仍以 LLM 给的 title 为准（经 _humanize_chart_title 清理），
# 文件名 stem 仅作为磁盘上的稳定标识，不再把整段标题塞进去。
_CHART_TYPE_LABELS_ZH: dict[str, str] = {
    "bar": "柱状图",
    "line": "折线图",
    "area": "面积图",
    "scatter": "散点图",
    "scatter_3d": "三维散点",
    "histogram": "直方图",
    "box": "箱线图",
    "violin": "小提琴图",
    "pie": "饼图",
    "heatmap": "热力图",
    "correlation_heatmap": "相关性热力图",
    "scatter_matrix": "散点矩阵",
    "sunburst": "旭日图",
    "treemap": "矩形树图",
}

# LLM 常在 title 后面追加的内部技术标记，例如：
#   "客户评分按产品分布_ANOVA_p_0_0012_η²_0_546"
#   "销量_vs_营收散点图_极端离群值主导"
#   "区域销售_n_2"
# 这些前缀在 UI 上展示给用户会造成"看不懂"。下面这些正则用来把第一段
# 人类可读部分取出来，并去掉所有 _n_N、_样本_N、ANOVA、p=、η² 等标记。
# 注意：之前用 _ANOVA.*$ 贪婪匹配到字符串末尾，会误删 LLM 可能给的合法
# 副标题（如 "客户评分分布_ANOVA_用户洞察" → "客户评分分布" 丢了"用户洞察"）。
# 现在的非贪婪模式只匹配技术标记本身（数字、p 值、η² 等已知后缀），不
# 会吞掉后面的可读内容。
_CHART_TITLE_TECHNICAL_PATTERNS = [
    re.compile(r"_n_\d+", re.IGNORECASE),
    re.compile(r"_样本_\d+", re.IGNORECASE),
    # _ANOVA 后面跟可选的 p 值/η²/effect_size/F 值等数字串，但不吞掉中文。
    re.compile(r"_ANOVA(?:_[a-zA-Z0-9_]+)?(?=_|$)", re.IGNORECASE),
    re.compile(r"_(?:p|p_value|pvalue)\s*[=:]?\s*[\d._-]+", re.IGNORECASE),
    re.compile(r"_η²\s*[\d._-]+", re.IGNORECASE),
    re.compile(r"_effect_size\s*[\d._-]+", re.IGNORECASE),
    re.compile(r"_F\s*[\d._-]+", re.IGNORECASE),
    # 离群值标记只删标记本身（"极端离群值主导" / "含离群值"），不吞后面内容。
    re.compile(r"_极端离群值(?:主导)?(?=_|$)", re.IGNORECASE),
    re.compile(r"_离群值(?:主导)?(?=_|$)", re.IGNORECASE),
    re.compile(r"_主导$", re.IGNORECASE),
    re.compile(r"_含异常值(?=_|$)", re.IGNORECASE),
    re.compile(r"_含离群值(?=_|$)", re.IGNORECASE),
]


def _append_title_note(fig: go.Figure, note: str) -> None:
    """Append a compact factual note without nesting Plotly title markup."""
    current = fig.layout.title.text or "数据图表"
    if current.endswith("</sup>") and "<br><sup>" in current:
        fig.update_layout(title_text=f"{current[:-6]}；{note}</sup>")
    else:
        fig.update_layout(title_text=f"{current}<br><sup>{note}</sup>")


def _localize_boolean_categories(df: pd.DataFrame, columns: list[str | None]) -> None:
    """Turn raw True/False category labels into unambiguous Chinese labels."""
    for column in (value for value in columns if value and value in df.columns):
        non_null = set(df[column].dropna().unique().tolist())
        if non_null and non_null.issubset({True, False, np.bool_(True), np.bool_(False)}):
            df[column] = df[column].map(_BOOLEAN_VALUE_LABELS)


def _aggregate_for_chart(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
    aggregation: str,
) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    """Aggregate while retaining sample counts and absent category combinations."""
    group_columns = [x] + ([color] if color else [])
    grouped = df.groupby(group_columns, dropna=False)
    counts = grouped.size().rename(_SAMPLE_COUNT_COLUMN)
    if aggregation == "count":
        result = counts.rename("count").reset_index()
        result[_SAMPLE_COUNT_COLUMN] = result["count"]
        y = "count"
    else:
        if not y:
            raise ValueError(f"{aggregation} 聚合需要 y。")
        result = grouped[y].agg(aggregation).to_frame().join(counts).reset_index()

    coverage: dict[str, Any] = {
        "complete": True,
        "observed_combinations": len(result),
        "total_combinations": len(result),
        "missing_combinations": [],
    }
    result[_HAS_RECORDS_COLUMN] = True
    if not color:
        return result, y, coverage

    x_levels = list(pd.unique(df[x].dropna()))
    color_levels = list(pd.unique(df[color].dropna()))
    if not x_levels or not color_levels:
        return result, y, coverage
    # Cap the cross-product: a 100×100 grid would produce 10K rows of mostly
    # NaN, dominate memory, and render as visual noise. Skip reindex and let
    # the chart render only observed combinations; the coverage note below
    # already discloses the missing-count when reindex is feasible.
    if len(x_levels) * len(color_levels) > _MAX_REINDEX_COMBINATIONS:
        coverage = {
            "complete": True,
            "observed_combinations": len(result),
            "total_combinations": len(result),
            "missing_combinations": [],
            "color_levels": color_levels,
            "skipped_reindex": True,
        }
        return result, y, coverage
    combinations = pd.MultiIndex.from_product([x_levels, color_levels], names=[x, color])
    result = result.set_index([x, color]).reindex(combinations).reset_index()
    result[_HAS_RECORDS_COLUMN] = result[_SAMPLE_COUNT_COLUMN].notna()
    result[_SAMPLE_COUNT_COLUMN] = result[_SAMPLE_COUNT_COLUMN].fillna(0).astype(int)
    missing_rows = result.loc[~result[_HAS_RECORDS_COLUMN], [x, color]].to_dict("records")
    coverage = {
        "complete": not missing_rows,
        "observed_combinations": len(result) - len(missing_rows),
        "total_combinations": len(result),
        "missing_combinations": missing_rows,
        "color_levels": color_levels,
    }
    return result, y, coverage


def _add_missing_combination_markers(
    fig: go.Figure,
    coverage: dict[str, Any],
    *,
    x: str | None,
    color: str | None,
    aggregation: str,
) -> None:
    """Mark absent grouped categories so blank space cannot look like a render failure."""
    missing = coverage.get("missing_combinations") or []
    if not missing or not x or not color:
        return
    color_levels = coverage.get("color_levels") or []
    trace_colors = {
        str(trace.name): getattr(getattr(trace, "marker", None), "color", "#7B8783")
        for trace in fig.data
        if getattr(trace, "name", None) is not None
    }
    detailed_label = len(color_levels) <= 2
    for item in missing:
        color_index = color_levels.index(item[color]) if item[color] in color_levels else 0
        xshift = int((color_index - (len(color_levels) - 1) / 2) * 36)
        label = "无样本" if aggregation in {"mean", "median", "min", "max"} else "○"
        fig.add_annotation(
            x=item[x],
            y=0,
            xref="x",
            yref="y",
            xshift=xshift,
            yshift=12,
            text=label if detailed_label else "○",
            showarrow=False,
            font={"size": 10 if detailed_label else 15, "color": trace_colors.get(str(item[color]), "#7B8783")},
            bgcolor="rgba(251,250,245,0.78)" if detailed_label else "rgba(0,0,0,0)",
            borderpad=2,
        )
    missing_label = "无样本" if aggregation in {"mean", "median", "min", "max"} else "无记录"
    _append_title_note(
        fig,
        f"组合覆盖 {coverage['observed_combinations']}/{coverage['total_combinations']}；"
        f"基线标记表示{missing_label}，不是数值为 0，也不是漏画",
    )


def _annotate_extreme_values(fig: go.Figure) -> None:
    """在柱状图和折线图上自动标注最大值和最小值（best-effort）。

    仅标注第一个匹配的 trace，避免多 trace 分组图表标注过多影响可读性。
    通过 trace 类型（``bar`` 或 ``scatter`` 且 mode 含 ``lines``）判定是否
    适用，跳过饼图、热力图、散点矩阵等不适用场景。

    标注是 best-effort：任何异常都被吞掉，不能影响图表正常生成。
    """
    try:
        for trace in fig.data:
            trace_type = getattr(trace, "type", "bar")
            trace_mode = getattr(trace, "mode", "") or ""
            # 仅对柱状图和折线图标注，跳过饼图、热力图、散点矩阵等
            is_bar = trace_type == "bar"
            is_line = trace_type == "scatter" and trace_mode in {"lines", "lines+markers"}
            if not (is_bar or is_line):
                continue
            y_values = list(trace.y) if getattr(trace, "y", None) is not None else []
            x_values = list(trace.x) if getattr(trace, "x", None) is not None else []
            if not y_values or not x_values or len(y_values) != len(x_values):
                continue
            # 找到最大值和最小值的索引
            max_idx = max(range(len(y_values)), key=lambda i: y_values[i])
            min_idx = min(range(len(y_values)), key=lambda i: y_values[i])
            # 最大值标注
            y_max = y_values[max_idx]
            max_text = (
                f"最大: {y_max:.1f}"
                if isinstance(y_max, (int, float, np.number))
                else f"最大: {y_max}"
            )
            fig.add_annotation(
                x=x_values[max_idx],
                y=y_max,
                text=max_text,
                showarrow=True,
                arrowhead=2,
                arrowsize=0.8,
                arrowwidth=1,
                arrowcolor="#D97745",
                font={"size": 10, "color": "#D97745"},
                ax=0,
                ay=-30,
            )
            # 最小值标注
            y_min = y_values[min_idx]
            min_text = (
                f"最小: {y_min:.1f}"
                if isinstance(y_min, (int, float, np.number))
                else f"最小: {y_min}"
            )
            fig.add_annotation(
                x=x_values[min_idx],
                y=y_min,
                text=min_text,
                showarrow=True,
                arrowhead=2,
                arrowsize=0.8,
                arrowwidth=1,
                arrowcolor="#7A6FB0",
                font={"size": 10, "color": "#7A6FB0"},
                ax=0,
                ay=30,
            )
            break  # 只标注第一个 trace，避免多 trace 时标注过多
    except Exception:
        pass  # 标注是 best-effort，不能影响图表生成


def _severe_axis_compression(values: list[Any], *, include_zero: bool = False) -> dict[str, Any] | None:
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
        lower_fence, upper_fence = median - 8 * mad, median + 8 * mad
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
    from ._helpers import _nice_ticks
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
        nx = pd.to_numeric(pd.Series(xs, dtype="object"), errors="coerce").to_numpy() if xs else np.array([])
        ny = pd.to_numeric(pd.Series(ys, dtype="object"), errors="coerce").to_numpy() if ys else np.array([])
        # 预计算极端点 mask，向量化避免 Python 层循环。
        if x_guard and len(nx) > 0:
            x_extreme_mask = np.isfinite(nx) & ((nx < x_guard["lower"]) | (nx > x_guard["upper"]))
        else:
            x_extreme_mask = np.zeros(len(nx), dtype=bool) if len(nx) > 0 else np.array([], dtype=bool)
        if y_guard and len(ny) > 0:
            y_extreme_mask = np.isfinite(ny) & ((ny < y_guard["lower"]) | (ny > y_guard["upper"]))
        else:
            y_extreme_mask = np.zeros(len(ny), dtype=bool) if len(ny) > 0 else np.array([], dtype=bool)
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
        robust_layout.update({"xaxis.autorange": False, "xaxis.range": [x_guard["lower"], x_guard["upper"]]})
        full_layout["xaxis.autorange"] = True
        axis_ranges["x"] = [x_guard["lower"], x_guard["upper"]]
        fig.update_xaxes(range=axis_ranges["x"], autorange=False)
    if y_guard:
        robust_layout.update({"yaxis.autorange": False, "yaxis.range": [y_guard["lower"], y_guard["upper"]]})
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


def _checked_columns(df: pd.DataFrame, columns: list[str] | None) -> list[str]:
    """验证并返回列名列表，列不存在时抛出带可用列提示的 ValueError。"""
    result = list(df.columns) if not columns else columns
    missing = [column for column in result if column not in df.columns]
    if missing:
        raise ValueError(f"列不存在：{missing}。可用列：{list(df.columns)}")
    return result


def _numeric_columns(df: pd.DataFrame, columns: list[str] | None = None) -> list[str]:
    """筛选数值类型列，无可用数值列时抛出 ValueError。"""
    candidates = _checked_columns(df, columns) if columns else list(df.select_dtypes(include=np.number))
    result = [column for column in candidates if pd.api.types.is_numeric_dtype(df[column])]
    if not result:
        raise ValueError("没有可用于该操作的数值列。")
    return result


def _humanize_chart_title(title: str | None, chart_type: str) -> str:
    """Strip technical noise from LLM-provided chart titles.

    LLM 经常把 ANOVA / p 值 / η² / _n_2 / 极端离群值主导 等内部标记塞进
    title。这些在 UI 上展示给用户会变成"看不懂的乱码"。本函数取 title
    第一段可读部分，去掉技术标记并截短，文件名 stem 单独用 chart_type
    中文短名 + 序号生成（见 ``_chart_filename_stem``），磁盘文件名不再
    嵌入 LLM 自由发挥的标题。
    """
    raw = (title or "").strip()
    if not raw:
        return _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "数据图表")
    cleaned = raw
    for pattern in _CHART_TITLE_TECHNICAL_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_ ").strip()
    # 如果清理后为空（title 全是技术标记），回退到类型中文短名，
    # 而不是返回原始乱码 title —— 那正是用户"看不懂"的根源。
    if not cleaned:
        return _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "数据图表")
    # 截短到 30 个字符（按 Unicode 字符计数）以避免前端溢出。
    if len(cleaned) > _CHART_TITLE_MAX_CHARS:
        cleaned = cleaned[:_CHART_TITLE_MAX_CHARS].rstrip("_ ,，;；-—")
    # 截短后可能再次为空（前 30 字符全是分隔符），二次回退到类型短名。
    if not cleaned:
        return _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "数据图表")
    return cleaned


def _chart_filename_stem(chart_type: str, existing_count: int) -> str:
    """Build a short, stable filename stem like ``柱状图_1``.

    ``existing_count`` is the number of charts already registered in this
    workspace so each new chart of the same type gets a unique incrementing
    suffix instead of a uuid hash. 用自然数字 1/2/3 而非 01/02/03：前者
    读起来更像人话（"柱状图 1"），与 Observable / Plot 等业界惯例一致。
    """
    label = _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "图表")
    return f"{label}_{existing_count + 1}"


# === Plotly 分支白话解读（与 ECharts _auto_interpret 语义对齐）===
# 移植自 echarts_engine._auto_interpret，让双引擎都有"数据解读"区块。


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
            return _plotly_interpret_trend(df, chart_type=chart_type, x=x, y=y,
                                           color=color, aggregation=aggregation, title=title_text)
        if chart_type == "pie" and x and len(df) > 0:
            return _plotly_interpret_pie(df, x=x, title=title_text)
        if chart_type in {"scatter", "scatter_3d"} and x and y and len(df) > 0:
            return _plotly_interpret_scatter(df, x=x, y=y, title=title_text)
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
    df: pd.DataFrame, *, chart_type: str, x: str, y: str,
    color: str | None, aggregation: str, title: str,
) -> str:
    agg_label = {"mean": "平均", "median": "中位", "sum": "合计", "count": "计数",
                 "min": "最小", "max": "最大"}.get(aggregation, "")
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


def _plotly_interpret_scatter(df: pd.DataFrame, *, x: str, y: str, title: str) -> str:
    if not pd.api.types.is_numeric_dtype(df[x]) or not pd.api.types.is_numeric_dtype(df[y]):
        return f"「{title}」展示{_hl(x)}与{_hl(y)}的分布关系，悬浮查看每个点明细。"
    corr = float(df[[x, y]].corr().iloc[0, 1])
    if _math.isnan(corr):
        return f"「{title}」展示两变量分布，悬浮查看每个点明细，框选可放大区域。"
    direction = "正向" if corr > 0 else "反向"
    strength = "强" if abs(corr) > 0.7 else "中等" if abs(corr) > 0.4 else "弱"
    return (
        f"「{title}」呈现{direction}{strength}相关（r={corr:.2f}），"
        f"共{len(df)}个点。滚轮缩放可查看密集区域，框选可隔离离群点。"
    )


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
    return (
        f"「{title}」以层级方式展示数据结构，点击节点可下钻/上卷，"
        f"面积大小反映对应数值占比。"
    )

