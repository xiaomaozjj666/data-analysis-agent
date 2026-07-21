"""数据分析工具集：为 ReAct 执行器提供会话绑定的数据操作能力。

本模块通过 ``build_tools(workspace)`` 工厂函数生成一组 LangChain BaseTool，
包括：
- inspect_data: 数据集概况检查（形状、类型、缺失、重复、样例）
- repair_data_format: 保守的格式修复（不触碰业务数据）
- clean_data: 带安全护栏的数据清洗
- transform_data: 非破坏性筛选/排序视图
- statistical_analysis: 描述统计、相关、分组、假设检验、回归
- create_visualization: Plotly 交互式图表生成
- export_data: 数据导出

设计原则：
- 所有工具通过闭包绑定同一个 DataWorkspace 实例，确保状态一致性。
- 清洗和变换操作带有严格的安全护栏（最小行数、最大删除比例）。
- 可视化自动检测极端值并提供主体尺度/全量视图切换，不修改原始数据。
- 工具返回 JSON 字符串，由 json_text() 统一序列化，确保 NaN/Infinity
  等非法 JSON 值被转为 null。
"""

from __future__ import annotations

import logging
import operator
import re
from html import escape
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from langchain_core.tools import BaseTool, tool
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace, _atomic_write_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 命名常量
# ---------------------------------------------------------------------------

#: 分组图表 reindex 笛卡尔积上限。50×50 = 2500 单元格已足够，
#: 超过此值图表不可读且 reindex 主导内存。调用方仅看到实际观测组合。
_MAX_REINDEX_COMBINATIONS = 2_500

#: 散点矩阵支持的最大维度数，超过后图表不可读且渲染性能急剧下降。
_SCATTER_MATRIX_MAX_DIMENSIONS = 8

#: 卡方检验拒绝的最大基数，超过此值应建议用户合并类别。
_CHI_SQUARE_MAX_CARDINALITY = 100

#: groupby 结果返回的最大行数，防止高基数分组擑爆 context window。
_GROUPBY_MAX_ROWS = 500

#: 图表标题清理后的最大字符数，避免前端溢出。
_CHART_TITLE_MAX_CHARS = 30

#: transform_data 的 limit 参数上限。
_TRANSFORM_LIMIT_MAX = 1_000_000

_CHART_COLORS = [
    "#245C55",
    "#D97745",
    "#4F6BED",
    "#C75D63",
    "#7A6FB0",
    "#D2A63C",
    "#4B8FA8",
    "#8A9A5B",
]

_COLUMN_LABELS = {
    "units": "销量",
    "revenue": "收入",
    "sales": "销售额",
    "profit": "利润",
    "product": "产品",
    "region": "区域",
    "channel": "渠道",
    "category": "类别",
    "customer_rating": "客户评分",
    "unit_price": "单价",
    "discount_rate": "折扣率",
    "order_date": "订单日期",
    "date": "日期",
    "count": "记录数",
    "is_returned": "是否退货",
}

_AGGREGATION_LABELS = {
    "sum": "合计",
    "mean": "平均值",
    "median": "中位数",
    "count": "计数",
    "min": "最小值",
    "max": "最大值",
}

_BOOLEAN_VALUE_LABELS = {False: "未退货", True: "已退货"}
_SAMPLE_COUNT_COLUMN = "__sample_count__"
_HAS_RECORDS_COLUMN = "__has_records__"
#: 预计算的 hover 文本列，避免无记录 bar 在 hover 中显示 nan。
_HOVER_TEXT_COLUMN = "__hover_text__"


def _human_column_label(column: str | None) -> str:
    """将英文列名映射为中文可读标签，未知列名用下划线转空格回退。"""
    if not column:
        return ""
    return _COLUMN_LABELS.get(column, str(column).replace("_", " ").strip())


def _compact_number(value: float) -> str:
    """将数值格式化为紧凑显示（如 1.2M、35.6K），用于图表标注。"""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    if absolute >= 10:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


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
    padding = max(normal_span * 0.08, max(abs(normal_min), abs(normal_max), 1.0) * 0.04)
    lower = normal_min - padding
    upper = normal_max + padding
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


def _normalize_column_names(
    df: pd.DataFrame,
    columns: list[str] | None,
    datetime_columns: list[str] | None,
) -> tuple[pd.DataFrame, list[str] | None, list[str] | None, bool]:
    """Lowercase and sanitize column names; remap caller-provided selections."""
    original = list(df.columns)
    normalized: list[str] = []
    seen: dict[str, int] = {}
    for value in original:
        base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(value).strip().lower()).strip("_") or "column"
        seen[base] = seen.get(base, 0) + 1
        normalized.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    if normalized == original:
        return df, columns, datetime_columns, False
    df = df.copy()
    df.columns = normalized
    mapping = dict(zip(original, normalized, strict=True))
    if columns:
        columns = [mapping.get(value, value) for value in columns]
    if datetime_columns:
        datetime_columns = [mapping.get(value, value) for value in datetime_columns]
    return df, columns, datetime_columns, True


def _trim_string_columns(df: pd.DataFrame) -> int:
    """Trim leading/trailing whitespace on object/string columns in place."""
    string_columns = list(df.select_dtypes(include=["object", "string"]).columns)
    for column in string_columns:
        df[column] = df[column].map(lambda value: value.strip() if isinstance(value, str) else value)
    return len(string_columns)


def _parse_numeric_columns(df: pd.DataFrame, threshold: float = 0.8) -> list[str]:
    """Coerce object/string columns whose numeric ratio meets ``threshold``."""
    converted: list[str] = []
    for column in df.select_dtypes(include=["object", "string"]).columns:
        non_empty = df[column].dropna()
        if non_empty.empty:
            continue
        candidate = pd.to_numeric(df[column], errors="coerce")
        if candidate.notna().sum() / len(non_empty) >= threshold:
            df[column] = candidate
            converted.append(str(column))
    return converted


def _apply_missing_strategy(
    df: pd.DataFrame,
    selected: list[str],
    strategy: str,
) -> None:
    """Apply the requested missing-value strategy in place."""
    if strategy == "drop":
        old_len = len(df)
        df.dropna(subset=selected, inplace=True)
        df.reset_index(drop=True, inplace=True)
        dropped = old_len - len(df)
        if dropped > 0 and dropped / max(old_len, 1) > 0.5:
            raise ValueError(
                f"拒绝高比例删行：将删除 {dropped}/{old_len} 行。"
                "请改用填充策略，或先让用户确认后分批处理。"
            )
        return
    if strategy in {"forward_fill", "backward_fill"}:
        method = "ffill" if strategy == "forward_fill" else "bfill"
        df[selected] = getattr(df[selected], method)()
        return
    for column in selected:
        series = df[column]
        if not series.isna().any():
            continue
        if strategy in {"mean", "median"}:
            if not pd.api.types.is_numeric_dtype(series):
                raise ValueError(f"列 {column} 不是数值列，不能使用 {strategy} 填充。")
            fill_value = getattr(series, strategy)()
        else:
            modes = series.mode(dropna=True)
            if modes.empty:
                continue
            fill_value = modes.iloc[0]
        df[column] = series.fillna(fill_value)


def _handle_outliers(
    df: pd.DataFrame,
    selected: list[str],
    method: str,
    action: str,
) -> tuple[int, dict[str, tuple[float, float]]]:
    """Detect and optionally cap/remove outliers on numeric selected columns."""
    numeric = _numeric_columns(df, selected)
    masks: dict[str, pd.Series] = {}
    bounds: dict[str, tuple[float, float]] = {}
    for column in numeric:
        series = df[column]
        if method == "iqr":
            q1, q3 = series.quantile([0.25, 0.75])
            spread = q3 - q1
            lower, upper = float(q1 - 1.5 * spread), float(q3 + 1.5 * spread)
        else:
            mean, std = float(series.mean()), float(series.std(ddof=0))
            if std == 0 or not np.isfinite(std):
                continue
            lower, upper = mean - 3 * std, mean + 3 * std
        bounds[column] = (lower, upper)
        masks[column] = series.lt(lower) | series.gt(upper)
    count = int(pd.DataFrame(masks).any(axis=1).sum()) if masks else 0
    if action == "remove" and masks:
        if count / max(len(df), 1) > 0.3:
            raise ValueError(
                f"拒绝一次删除过多离群记录：将删除 {count}/{len(df)} 行。"
                "请改用 cap，或缩小需要检查的列。"
            )
        df.drop(pd.DataFrame(masks).any(axis=1).loc[lambda s: s].index, inplace=True)
        df.reset_index(drop=True, inplace=True)
    elif action == "cap":
        for column, (lower, upper) in bounds.items():
            df[column] = df[column].clip(lower, upper)
    return count, bounds


def build_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建绑定到指定工作区的工具集，供 ReAct Agent 使用。

    所有工具通过闭包捕获同一个 workspace 实例，确保数据状态一致性。
    工具列表的顺序即为 Agent 看到的工具顺序。

    Args:
        workspace: 当前分析会话的数据工作区。

    Returns:
        7 个 BaseTool 实例的列表。
    """

    @tool
    def inspect_data(sample_rows: int = 5) -> str:
        """Inspect the active dataset: shape, types, missing values, duplicates and sample rows.

        Always call this before cleaning, statistics or visualization. sample_rows must be 1-20.
        """
        if not 1 <= sample_rows <= 20:
            raise ValueError("sample_rows 必须在 1 到 20 之间。")
        return json_text(workspace.profile(sample_rows=sample_rows))

    @tool
    def repair_data_format(
        normalize_missing: bool = True,
        trim_strings: bool = True,
        parse_numeric: bool = True,
        parse_dates: bool = True,
        normalize_column_names: bool = False,
    ) -> str:
        """Repair only unambiguous formatting issues and return an audit record.

        Safe repairs include trimming text, standardizing explicit missing markers, converting
        fully numeric text columns, and converting fully parseable date/time columns. It never
        removes duplicates, fills missing business values, clips outliers, or changes negatives.
        Call this after a type/format error before retrying the failed analysis tool.
        """
        return json_text(
            workspace.repair_format(
                normalize_missing=normalize_missing,
                trim_strings=trim_strings,
                parse_numeric=parse_numeric,
                parse_dates=parse_dates,
                normalize_column_names=normalize_column_names,
            )
        )

    @tool
    def clean_data(
        columns: list[str] | None = None,
        drop_duplicates: bool = True,
        trim_strings: bool = True,
        normalize_column_names: bool = False,
        missing_strategy: Literal["none", "drop", "mean", "median", "mode", "forward_fill", "backward_fill"] = "none",
        parse_numeric: bool = False,
        datetime_columns: list[str] | None = None,
        outlier_method: Literal["none", "iqr", "zscore"] = "none",
        outlier_action: Literal["remove", "cap"] = "cap",
    ) -> str:
        """Clean the active dataset and save cleaned_data.csv.

        columns limits missing/outlier handling; other operations still apply globally.
        parse_numeric converts an object/string column when at least 80% of its non-empty
        values are numeric. This is intentionally more permissive than repair_data_format,
        which only converts columns where 100% of values are unambiguously numeric after
        trimming currency symbols and thousands separators. Use repair_data_format for safe
        formatting fixes and clean_data when the analysis needs broader numeric coercion.
        datetime_columns explicitly selects columns to parse as dates.
        IQR/z-score outlier handling only applies to numeric selected columns.
        Safety guards (always enforced regardless of parameters):
          - Missing-value deletion over 50% of the current rows is always refused;
            narrowing ``columns`` does not bypass this guard.
          - The cumulative row count of the main dataset must stay at or above 20%
            of the original source row count (computed as max(1, ceil(source/5))).
            Anything that would drop below that floor is refused so the active
            dataset never collapses to a meaningless sample.
        """
        df = workspace.dataframe.copy()
        before = {"rows": len(df), "columns": len(df.columns), "missing": int(df.isna().sum().sum())}
        changes: list[str] = []

        if normalize_column_names:
            df, columns, datetime_columns, changed = _normalize_column_names(df, columns, datetime_columns)
            if changed:
                changes.append("normalized column names")

        selected = _checked_columns(df, columns)
        if trim_strings:
            trimmed = _trim_string_columns(df)
            if trimmed:
                changes.append(f"trimmed {trimmed} text columns")

        if parse_numeric:
            converted = _parse_numeric_columns(df)
            changes.append(f"parsed numeric columns: {converted}")

        if datetime_columns:
            _checked_columns(df, datetime_columns)
            for column in datetime_columns:
                df[column] = pd.to_datetime(df[column], errors="coerce")
            changes.append(f"parsed datetime columns: {datetime_columns}")

        if drop_duplicates:
            count = int(df.duplicated().sum())
            df = df.drop_duplicates().copy()
            changes.append(f"removed {count} duplicate rows")

        if missing_strategy != "none":
            _apply_missing_strategy(df, selected, missing_strategy)
            changes.append(f"applied missing strategy: {missing_strategy}")

        if outlier_method != "none":
            count, _ = _handle_outliers(df, selected, outlier_method, outlier_action)
            changes.append(f"{outlier_action}ped {count} {outlier_method} outlier rows")

        minimum_rows = max(1, (workspace.source_row_count + 4) // 5)
        if len(df) < minimum_rows:
            raise ValueError(
                f"拒绝累计删除过多记录：当前操作会使主数据从原始 {workspace.source_row_count} 行"
                f"缩减为 {len(df)} 行，安全下限为 {minimum_rows} 行。"
                "请保留主数据，并使用非破坏性的筛选视图检查子集。"
            )

        workspace.dataframe = df.reset_index(drop=True)
        output = workspace.save_dataframe("cleaned_data.csv")
        after = {"rows": len(df), "columns": len(df.columns), "missing": int(df.isna().sum().sum())}
        return json_text({"status": "ok", "before": before, "after": after, "changes": changes, "output": output})

    @tool
    def transform_data(
        select_columns: list[str] | None = None,
        filter_column: str | None = None,
        filter_operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "in"] = "eq",
        filter_value: str | float | int | list[str] | list[float] | None = None,
        sort_by: list[str] | None = None,
        ascending: bool = True,
        limit: int | None = None,
    ) -> str:
        """Create a non-destructive filtered or sorted view and export it.

        Use 'in' with a list and 'contains' for text. The result is exported as
        transformed_data.csv but never replaces the active dataset. Use clean_data for deliberate,
        guarded changes to the main data.
        """
        df = workspace.dataframe.copy()
        if filter_column:
            _checked_columns(df, [filter_column])
            if filter_value is None:
                raise ValueError("使用筛选时 filter_value 不能为空。")
            series = df[filter_column]
            if filter_operator == "contains":
                mask = series.astype("string").str.contains(str(filter_value), case=False, na=False, regex=False)
            elif filter_operator == "in":
                if not isinstance(filter_value, list):
                    raise ValueError("in 操作需要列表类型 filter_value。")
                # Adapt list element types so that numeric columns accept numeric
                # lists and string columns accept string lists; mixed types
                # would silently produce an empty result via pandas isin.
                values: list[Any] = list(filter_value)
                if pd.api.types.is_numeric_dtype(series):
                    coerced: list[Any] = []
                    for value in values:
                        try:
                            coerced.append(float(value))
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                f"列 {filter_column} 是数值列，filter_value 中的 {value!r} 无法转为数值。"
                            ) from exc
                    values = coerced
                else:
                    values = [str(value) for value in values]
                mask = series.isin(values)
            else:
                operations = {
                    "eq": operator.eq,
                    "ne": operator.ne,
                    "gt": operator.gt,
                    "ge": operator.ge,
                    "lt": operator.lt,
                    "le": operator.le,
                }
                comparison: Any = filter_value
                if pd.api.types.is_numeric_dtype(series):
                    try:
                        comparison = float(filter_value)  # type: ignore[arg-type]
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"列 {filter_column} 是数值列，filter_value {filter_value!r} 无法转为数值。"
                        ) from exc
                mask = operations[filter_operator](series, comparison)
            df = df.loc[mask].copy()
        if sort_by:
            _checked_columns(df, sort_by)
            df = df.sort_values(sort_by, ascending=ascending)
        if select_columns:
            _checked_columns(df, select_columns)
            df = df[select_columns].copy()
        if limit is not None:
            if not 1 <= limit <= _TRANSFORM_LIMIT_MAX:
                raise ValueError(f"limit 必须在 1 到 {_TRANSFORM_LIMIT_MAX:,} 之间。")
            df = df.head(limit).copy()
        df = df.reset_index(drop=True)
        output = workspace.save_dataframe(
            "transformed_data.csv",
            dataframe=df,
            description="筛选或排序后的数据视图（未改变主数据）",
        )
        return json_text(
            {
                "status": "ok",
                "rows": len(df),
                "columns": list(df.columns),
                "output": output,
                "view_only": True,
                "active_rows": len(workspace.dataframe),
            }
        )

    @tool
    def statistical_analysis(
        method: Literal["descriptive", "correlation", "groupby", "ttest_ind", "ttest_paired", "anova", "chi_square", "linear_regression"],
        columns: list[str] | None = None,
        group_by: str | None = None,
        target: str | None = None,
        aggregation: Literal["mean", "median", "sum", "count", "min", "max", "std"] = "mean",
        alpha: float = 0.05,
    ) -> str:
        """Run rigorous statistical analysis on the active dataset.

        descriptive: optional columns. correlation: numeric columns. groupby: group_by plus value
        columns. t-tests: exactly two numeric columns. anova: numeric target grouped by group_by.
        chi_square: exactly two categorical columns. linear_regression: target plus numeric feature
        columns. Returns statistics, p-values, effect/model metrics and plain decision fields.

        只读契约：此工具不 copy DataFrame，所有下游操作（describe / corr /
        groupby / dropna / crosstab）都返回新对象，不修改 workspace.dataframe。
        若未来需要在此工具中修改数据，必须先 ``df = workspace.dataframe.copy()``
        以免污染主数据。
        """
        # 只读引用：所有下游操作都返回新 DataFrame，不修改 workspace.dataframe。
        df = workspace.dataframe
        if not 0 < alpha < 1:
            raise ValueError("alpha 必须在 0 到 1 之间。")
        result: dict[str, Any] = {"method": method, "alpha": alpha}

        if method == "descriptive":
            selected = _checked_columns(df, columns)
            desc = df[selected].describe(include="all").transpose().reset_index(names="column")
            # 补充分布形态指标（偏度/峰度），帮助判断数据是否正态、是否有重尾。
            numeric_selected = [c for c in selected if pd.api.types.is_numeric_dtype(df[c])]
            if numeric_selected:
                skew = df[numeric_selected].skew().rename("skewness")
                kurt = df[numeric_selected].kurtosis().rename("kurtosis")
                shape_df = pd.DataFrame({"column": numeric_selected, "skewness": skew.values, "kurtosis": kurt.values})
                desc = desc.merge(shape_df, on="column", how="left")
            result["result"] = desc
        elif method == "correlation":
            numeric = _numeric_columns(df, columns)
            result["columns"] = numeric
            result["result"] = df[numeric].corr().round(6).to_dict()
            # 性能优化：预先提取数值子集，避免每对列重复索引和 dropna。
            # 对 20+ 数值列，原实现每对都 df[[left,right]].dropna()，
            # 产生大量中间 DataFrame 对象；改为一次性提取子集后按列对操作。
            numeric_df = df[numeric]
            p_values: dict[str, dict[str, float | None]] = {column: {} for column in numeric}
            sample_sizes: dict[str, dict[str, int]] = {column: {} for column in numeric}
            for i, left in enumerate(numeric):
                p_values[left][left] = 0.0
                sample_sizes[left][left] = int(numeric_df[left].notna().sum())
                left_series = numeric_df[left]
                for right in numeric[i + 1:]:
                    right_series = numeric_df[right]
                    # 只对两列同时非空的行计算，避免全表 dropna。
                    pair_mask = left_series.notna() & right_series.notna()
                    n_valid = int(pair_mask.sum())
                    sample_sizes[left][right] = n_valid
                    sample_sizes[right][left] = n_valid
                    if n_valid < 3:
                        p_values[left][right] = None
                        p_values[right][left] = None
                    else:
                        lv = left_series[pair_mask]
                        rv = right_series[pair_mask]
                        if lv.nunique() < 2 or rv.nunique() < 2:
                            p_values[left][right] = None
                            p_values[right][left] = None
                        else:
                            p_val = float(stats.pearsonr(lv, rv).pvalue)
                            p_values[left][right] = p_val
                            p_values[right][left] = p_val
            result["p_values"] = p_values
            result["sample_sizes"] = sample_sizes
        elif method == "groupby":
            if not group_by:
                raise ValueError("groupby 方法需要 group_by。")
            _checked_columns(df, [group_by])
            values = _numeric_columns(df, columns) if aggregation != "count" else _checked_columns(df, columns)
            grouped = df.groupby(group_by, dropna=False)[values].agg(aggregation).reset_index()
            # 补充每组样本量，帮助用户判断分组结果是否可靠。
            group_sizes = df.groupby(group_by, dropna=False).size().rename("_group_n_").reset_index()
            grouped = grouped.merge(group_sizes, on=group_by, how="left")
            result["result"] = grouped.head(_GROUPBY_MAX_ROWS)
            result["truncated"] = len(grouped) > _GROUPBY_MAX_ROWS
            result["group_count"] = int(df[group_by].nunique(dropna=False))
        elif method in {"ttest_ind", "ttest_paired"}:
            numeric = _numeric_columns(df, columns)
            if len(numeric) != 2:
                raise ValueError("t 检验需要恰好两个数值列。")
            pair = df[numeric].dropna()
            first, second = pair[numeric[0]], pair[numeric[1]]
            test = stats.ttest_ind(first, second, equal_var=False) if method == "ttest_ind" else stats.ttest_rel(first, second)
            difference = first - second
            if method == "ttest_ind":
                pooled = np.sqrt(((first.var(ddof=1) * (len(first) - 1)) + (second.var(ddof=1) * (len(second) - 1))) / max(len(pair) - 2, 1))
                effect_size = float((first.mean() - second.mean()) / pooled) if pooled else 0.0
            else:
                effect_size = float(difference.mean() / difference.std(ddof=1)) if difference.std(ddof=1) else 0.0
            confidence = test.confidence_interval(confidence_level=1 - alpha)
            result.update(
                statistic=float(test.statistic),
                p_value=float(test.pvalue),
                significant=bool(test.pvalue < alpha),
                sample_size=len(pair),
                means={column: float(pair[column].mean()) for column in numeric},
                mean_difference=float(difference.mean()),
                confidence_interval={"low": float(confidence.low), "high": float(confidence.high)},
                effect_size=float(effect_size),
                effect_size_name="cohens_d",
            )
        elif method == "anova":
            if not group_by or not target:
                raise ValueError("anova 需要 group_by 和 target。")
            _checked_columns(df, [group_by, target])
            if not pd.api.types.is_numeric_dtype(df[target]):
                raise ValueError("anova target 必须是数值列。")
            groups = [part[target].dropna().to_numpy() for _, part in df.groupby(group_by) if part[target].notna().sum() >= 2]
            if len(groups) < 2:
                raise ValueError("anova 至少需要两个各含 2 个有效观测值的组。")
            test = stats.f_oneway(*groups)
            valid = np.concatenate(groups)
            grand_mean = float(valid.mean())
            between = sum(len(group) * (float(group.mean()) - grand_mean) ** 2 for group in groups)
            total = float(((valid - grand_mean) ** 2).sum())
            result.update(
                statistic=float(test.statistic),
                p_value=float(test.pvalue),
                significant=bool(test.pvalue < alpha),
                groups=len(groups),
                total_n=int(len(valid)),
                eta_squared=float(between / total) if total else 0.0,
                group_means={str(name): float(part[target].mean()) for name, part in df.groupby(group_by) if part[target].notna().sum() >= 2},
            )
        elif method == "chi_square":
            selected = _checked_columns(df, columns)
            if len(selected) != 2:
                raise ValueError("卡方检验需要恰好两个分类列。")
            cardinality = {column: int(df[column].nunique(dropna=True)) for column in selected}
            max_cardinality = max(cardinality.values())
            if max_cardinality > _CHI_SQUARE_MAX_CARDINALITY:
                raise ValueError(
                    f"卡方检验拒绝高基数列：{cardinality}。"
                    "请对类别做合并、分组或改用其他统计方法。"
                )
            table = pd.crosstab(df[selected[0]], df[selected[1]])
            chi2, p_value, dof, expected = stats.chi2_contingency(table)
            result.update(statistic=float(chi2), p_value=float(p_value), significant=bool(p_value < alpha), degrees_of_freedom=int(dof), contingency_table=table.to_dict(), min_expected=float(np.min(expected)))
        else:
            if not target:
                raise ValueError("linear_regression 需要 target。")
            features = _numeric_columns(df, columns)
            _checked_columns(df, [target])
            if target in features:
                features.remove(target)
            if not features or not pd.api.types.is_numeric_dtype(df[target]):
                raise ValueError("线性回归需要数值 target 和至少一个数值特征列。")
            model_data = df[[*features, target]].dropna()
            if len(model_data) <= len(features) + 1:
                raise ValueError("有效样本量不足以拟合线性回归。")
            model = LinearRegression().fit(model_data[features], model_data[target])
            prediction = model.predict(model_data[features])
            result.update(
                target=target,
                features=features,
                sample_size=len(model_data),
                intercept=float(model.intercept_),
                coefficients=dict(zip(features, map(float, model.coef_), strict=True)),
                r2=float(r2_score(model_data[target], prediction)),
                rmse=float(mean_squared_error(model_data[target], prediction) ** 0.5),
                mae=float(mean_absolute_error(model_data[target], prediction)),
                adjusted_r2=float(1 - (1 - r2_score(model_data[target], prediction)) * (len(model_data) - 1) / max(len(model_data) - len(features) - 1, 1)),
            )

        return json_text(result)

    @tool
    def create_visualization(
        chart_type: Literal["bar", "line", "area", "scatter", "scatter_3d", "histogram", "box", "violin", "pie", "heatmap", "correlation_heatmap", "scatter_matrix", "sunburst", "treemap"],
        x: str | None = None,
        y: str | None = None,
        color: str | None = None,
        z: str | None = None,
        size: str | None = None,
        values: str | None = None,
        path_columns: list[str] | None = None,
        dimensions: list[str] | None = None,
        aggregation: Literal["none", "mean", "median", "sum", "count", "min", "max"] = "none",
        title: str | None = None,
        bins: int = 30,
        top_n: int | None = None,
        scale_mode: Literal["auto", "full"] = "auto",
        export_png: bool = False,
    ) -> str:
        """Create an interactive Plotly chart and save a standalone HTML artifact.

        Supports standard and complex plots including 3D scatter, correlation heatmap, scatter
        matrix, sunburst and treemap. For bar/line/area, aggregation groups by x and optional
        color. heatmap expects x, y and numeric values. scatter_matrix uses dimensions.
        sunburst/treemap use path_columns and values. scale_mode="auto" detects scale-destroying
        extreme values and adds readable main/full viewport controls without changing the data;
        use scale_mode="full" only when the user explicitly wants the uncompressed raw scale.
        export_png is best-effort and needs Chrome.
        """
        # 性能优化：只在确实需要修改数据时才 copy（聚合、布尔值本地化、
        # top_n 筛选）。对大 DataFrame（100K+ 行）避免无谓的深拷贝。
        # correlation_heatmap / scatter_matrix 等只读图表不需要 copy。
        _raw_df = workspace.dataframe
        _has_bool = any(
            col and col in _raw_df.columns and pd.api.types.is_bool_dtype(_raw_df[col])
            for col in (x, color)
        )
        needs_mutation = (
            aggregation != "none"
            or chart_type in {"bar", "line", "area", "pie", "sunburst", "treemap"}
            or top_n is not None
            or _has_bool
        )
        df = _raw_df.copy() if needs_mutation else _raw_df
        requested = [value for value in [x, y, color, z, size, values] if value]
        requested.extend(path_columns or [])
        requested.extend(dimensions or [])
        _checked_columns(df, requested)
        if top_n is not None:
            if not x or not 1 <= top_n <= 500:
                raise ValueError("top_n 需要 x，且必须在 1 到 500 之间。")
            keep = df[x].value_counts(dropna=False).head(top_n).index
            df = df[df[x].isin(keep)].copy()

        groupable = chart_type in {"bar", "line", "area"}
        coverage: dict[str, Any] = {
            "complete": True,
            "observed_combinations": 0,
            "total_combinations": 0,
            "missing_combinations": [],
        }
        if aggregation != "none":
            if not groupable or not x:
                raise ValueError("aggregation 仅用于带 x 的 bar/line/area。")
            df, y, coverage = _aggregate_for_chart(
                df,
                x=x,
                y=y,
                color=color,
                aggregation=aggregation,
            )

        _localize_boolean_categories(df, [x, color])
        for item in coverage.get("missing_combinations", []):
            for column in (x, color):
                if column and item.get(column) in _BOOLEAN_VALUE_LABELS:
                    item[column] = _BOOLEAN_VALUE_LABELS[item[column]]
        if coverage.get("color_levels"):
            coverage["color_levels"] = [
                _BOOLEAN_VALUE_LABELS.get(value, value) for value in coverage["color_levels"]
            ]

        labels = {column: _human_column_label(str(column)) for column in df.columns}
        if y and aggregation in _AGGREGATION_LABELS:
            labels[y] = f"{_human_column_label(y)}（{_AGGREGATION_LABELS[aggregation]}）"
        common = {
            "data_frame": df,
            "title": title,
            "template": "plotly_white",
            "labels": labels,
            "color_discrete_sequence": _CHART_COLORS,
        }
        if chart_type == "bar":
            # 预计算 hover 文本：对无记录 bar（reindex 产生的空白组合）显示
            # "无样本/无记录"，而非让 Plotly 把 y=NaN 格式化成 "nan"。
            # 聚合后行数通常很少（x_levels × color_levels），iterrows 开销可忽略。
            if aggregation != "none" and _SAMPLE_COUNT_COLUMN in df.columns:
                x_label_hover = _human_column_label(x)
                y_label_hover = _human_column_label(y) if y else ""
                hover_lines: list[str] = []
                for _, row in df.iterrows():
                    if row[_HAS_RECORDS_COLUMN]:
                        lines = [f"{x_label_hover}：{row[x]}"]
                        if y:
                            y_val = row[y]
                            y_text = f"{float(y_val):,.2f}" if pd.notna(y_val) else "—"
                            lines.append(f"{y_label_hover}：{y_text}")
                        lines.append(f"样本数：{int(row[_SAMPLE_COUNT_COLUMN])}")
                        hover_lines.append("<br>".join(lines))
                    else:
                        missing_label = "无样本" if aggregation in {"mean", "median", "min", "max"} else "无记录"
                        hover_lines.append(f"{x_label_hover}：{row[x]}<br>{missing_label}")
                df[_HOVER_TEXT_COLUMN] = hover_lines
                custom_data = [_SAMPLE_COUNT_COLUMN, _HAS_RECORDS_COLUMN, _HOVER_TEXT_COLUMN]
            else:
                custom_data = None
            fig = px.bar(**common, x=x, y=y, color=color, barmode="group", custom_data=custom_data)
        elif chart_type == "line":
            fig = px.line(**common, x=x, y=y, color=color, markers=True)
        elif chart_type == "area":
            fig = px.area(**common, x=x, y=y, color=color)
        elif chart_type == "scatter":
            fig = px.scatter(**common, x=x, y=y, color=color, size=size, trendline=None)
        elif chart_type == "scatter_3d":
            if not x or not y or not z:
                raise ValueError("scatter_3d 需要 x、y、z。")
            fig = px.scatter_3d(**common, x=x, y=y, z=z, color=color, size=size)
        elif chart_type == "histogram":
            fig = px.histogram(**common, x=x, color=color, nbins=max(2, min(bins, 200)), marginal="box")
        elif chart_type == "box":
            fig = px.box(**common, x=x, y=y, color=color, points="outliers")
        elif chart_type == "violin":
            fig = px.violin(**common, x=x, y=y, color=color, box=True, points="outliers")
        elif chart_type == "pie":
            if not x:
                raise ValueError("pie 需要 x 作为名称列。")
            fig = px.pie(**common, names=x, values=values or y, hole=0.42)
        elif chart_type == "heatmap":
            if not x or not y or not values:
                raise ValueError("heatmap 需要 x、y、values。")
            pivot = df.pivot_table(index=y, columns=x, values=values, aggfunc="mean")
            fig = px.imshow(pivot, text_auto=True, aspect="auto", title=title, color_continuous_scale="RdBu_r")
        elif chart_type == "correlation_heatmap":
            numeric = _numeric_columns(df, dimensions)
            corr = df[numeric].corr()
            fig = px.imshow(corr, text_auto=".2f", aspect="auto", title=title or "Correlation heatmap", color_continuous_scale="RdBu_r", zmin=-1, zmax=1)
        elif chart_type == "scatter_matrix":
            numeric = _numeric_columns(df, dimensions)
            if len(numeric) > _SCATTER_MATRIX_MAX_DIMENSIONS:
                raise ValueError(f"scatter_matrix 最多支持 {_SCATTER_MATRIX_MAX_DIMENSIONS} 个维度，请缩小 dimensions。")
            fig = px.scatter_matrix(**common, dimensions=numeric, color=color)
            fig.update_traces(diagonal_visible=False)
        elif chart_type == "sunburst":
            if not path_columns:
                raise ValueError("sunburst 需要 path_columns。")
            fig = px.sunburst(**common, path=path_columns, values=values)
        else:
            if not path_columns:
                raise ValueError("treemap 需要 path_columns。")
            fig = px.treemap(**common, path=path_columns, values=values, color=color)

        scale_details = _apply_outlier_scale_controls(fig, chart_type, scale_mode)
        if chart_type == "bar" and aggregation != "none":
            _add_missing_combination_markers(
                fig,
                coverage,
                x=x,
                color=color,
                aggregation=aggregation,
            )
        fig.update_layout(
            font={"family": "IBM Plex Sans, Noto Sans SC, sans-serif", "color": "#102a2a"},
            paper_bgcolor="#fbfaf5",
            plot_bgcolor="#fbfaf5",
            colorway=_CHART_COLORS,
            margin={
                "l": 64,
                "r": 36,
                "t": 108
                if scale_details["scale_mode"] == "robust" or coverage.get("missing_combinations")
                else 76,
                "b": 64,
            },
            hoverlabel={"bgcolor": "#102a2a", "font_color": "white"},
            title={"x": 0.01, "xanchor": "left", "font": {"size": 22, "color": "#102a2a"}},
            legend={
                "bgcolor": "rgba(255,255,255,0.82)",
                "bordercolor": "#D9E1DE",
                "borderwidth": 1,
                "font": {"size": 12},
            },
            autosize=True,
        )
        fig.update_xaxes(
            showgrid=True,
            gridcolor="#E5ECE9",
            zerolinecolor="#C9D5D1",
            linecolor="#BFCBC7",
            tickfont={"size": 12},
            title_font={"size": 14},
            automargin=True,
        )
        fig.update_yaxes(
            showgrid=True,
            gridcolor="#E5ECE9",
            zerolinecolor="#C9D5D1",
            linecolor="#BFCBC7",
            tickfont={"size": 12},
            title_font={"size": 14},
            automargin=True,
            separatethousands=True,
        )
        if chart_type == "bar":
            fig.update_traces(marker_line_color="rgba(16,42,42,0.16)", marker_line_width=0.8, selector={"type": "bar"})
            if aggregation != "none" and _HOVER_TEXT_COLUMN in df.columns:
                # 用预计算的 hover 文本（customdata[2]），避免无记录 bar 显示 nan。
                # customdata 结构：[sample_count, has_records, hover_text]
                fig.update_traces(
                    hovertemplate="%{customdata[2]}<extra>%{fullData.name}</extra>",
                    selector={"type": "bar"},
                )
            if x and not pd.api.types.is_numeric_dtype(df[x]):
                fig.update_xaxes(categoryorder="total descending")
            fig.update_layout(bargap=0.26, bargroupgap=0.08)
        elif chart_type == "scatter":
            fig.update_traces(marker={"size": 9, "opacity": 0.82, "line": {"width": 0.7, "color": "white"}}, selector={"type": "scatter"})
        if color:
            fig.update_layout(legend_title_text=_human_column_label(color))
        # 文件名用"类型_序号"格式（如 柱状图_01），不再把 LLM 给的整段标题
        # 塞进文件名——以前会出现"客户评分按产品分布_ANOVA_p_0_0012_η²_..."
        # 这种看不懂的乱码文件名。display_title 才是 UI 上展示的人话标题。
        existing_chart_count = workspace.count_artifacts("visualization")
        stem = _chart_filename_stem(chart_type, existing_chart_count)
        display_title = _humanize_chart_title(title, chart_type)
        html_path = workspace.artifacts_dir / f"{stem}.html"
        shared_plotly = workspace.ensure_plotly_bundle()
        relative_script = shared_plotly.relative_to(workspace.artifacts_dir).as_posix() if shared_plotly else None
        html_template = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>{title}</title><script src='{script}'></script>"
            "<style>html,body{{width:100%;height:100%;margin:0;background:#fbfaf5;overflow:hidden}}"
            ".plotly-graph-div{{width:100% !important;height:100% !important;min-height:560px}}</style>"
            "</head><body>{div}</body></html>"
            if relative_script
            else None
        )
        if html_template is not None:
            div = fig.to_html(
                full_html=False,
                include_plotlyjs=False,
                default_width="100%",
                default_height="100%",
                config={
                    "responsive": True,
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                },
            )
            # XSS 防护：Plotly to_html 把 figure 数据序列化进 <script> 标签，
            # json.dumps 默认不转义 </script>。若用户 CSV 某列含
            # "</script><script>alert(1)</script>"，LLM 用该列作 x/color，
            # 浏览器解析时第一个 </script> 会提前关闭 Plotly 的 script 块，
            # 剩余 JS 在 iframe 上下文执行。统一转义 <\/script> 避免注入。
            div = div.replace("</script>", "<\\/script>")
            # 原子写：进程被 kill / 磁盘满时不会留下半截 HTML 让预览 iframe
            # 加载到损坏页面。tmp + os.replace 对同目录文件是原子的。
            _atomic_write_text(
                html_path,
                html_template.format(
                    title=escape(display_title),
                    script=relative_script,
                    div=div,
                ),
            )
        else:
            fig.write_html(html_path, include_plotlyjs=True, full_html=True)
        workspace.register_artifact(html_path, "visualization", display_title)
        json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
        fig.write_json(json_path)
        workspace.register_artifact(json_path, "chart_data", "Plotly figure JSON")

        response: dict[str, Any] = {
            "status": "ok",
            "chart_type": chart_type,
            "rows_plotted": len(df),
            **scale_details,
            "category_coverage": {
                "complete": coverage["complete"],
                "observed_combinations": coverage["observed_combinations"],
                "total_combinations": coverage["total_combinations"],
                "missing_count": len(coverage.get("missing_combinations", [])),
            },
            "html": html_path,
            "plotly_json": json_path,
        }
        if export_png:
            png_path = workspace.artifacts_dir / f"{stem}.png"
            try:
                fig.write_image(png_path, width=1400, height=850, scale=1.5)
                workspace.register_artifact(png_path, "image", title or chart_type)
                response["png"] = png_path
            except Exception as exc:  # Kaleido may need a local Chrome installation.
                response["png_warning"] = f"PNG 导出失败，但 HTML 图表已生成：{exc}"
        return json_text(response)

    @tool
    def export_data(format: Literal["csv", "xlsx", "parquet"] = "csv", filename: str = "analysis_result") -> str:
        """Export the current active dataset as CSV, Excel or Parquet and register the artifact."""
        safe_stem = re.sub(r"[^\w\-\u4e00-\u9fff]", "_", Path(filename).stem)[:60] or "analysis_result"
        path = workspace.save_dataframe(f"{safe_stem}.{format}")
        return json_text({"status": "ok", "rows": len(workspace.dataframe), "output": path})

    return [
        inspect_data,
        repair_data_format,
        clean_data,
        transform_data,
        statistical_analysis,
        create_visualization,
        export_data,
    ]
