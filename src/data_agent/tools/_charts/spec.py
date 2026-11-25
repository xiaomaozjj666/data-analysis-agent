"""``create_visualization`` 的请求解析与数据准备。

从 ``_tools_charts.create_visualization`` 拆出的前半段（原函数 217~348 行）：
列校验 → 自动选图 / 自动配色 → 按需 copy → 语义校验 → top_n → 聚合 →
布尔值本地化 → 轴标签。全部逻辑逐字搬移，只把局部变量收进 ``ChartSpec``
状态对象，便于逐段测试。

本模块不依赖 workspace，只处理 DataFrame 与调用参数；副作用仅有对
``df`` 的原地布尔值本地化/追加 hover 文本列（与原实现一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .._helpers import _human_column_label
from .aggregate import _aggregate_for_chart, _localize_boolean_categories
from .constants import _AGGREGATION_LABELS, _BOOLEAN_VALUE_LABELS
from .infer import _infer_chart_type
from .validate import _checked_columns, _validate_chart_semantics


def _empty_coverage() -> dict[str, Any]:
    """每次新建一份覆盖率结构，避免多个 spec 共享可变默认值。"""
    return {
        "complete": True,
        "observed_combinations": 0,
        "total_combinations": 0,
        "missing_combinations": [],
    }


@dataclass
class ChartSpec:
    """一次 ``create_visualization`` 调用的解析后状态。

    构造时只填调用参数与原始 DataFrame；``prepare_chart_spec`` 会逐步填充
    ``chart_type`` / ``df`` / ``labels`` / ``coverage`` 等派生字段。图型渲染、
    产物落盘与响应组装都读取同一个 spec，避免长参数列表。
    """

    chart_type: str
    df: pd.DataFrame
    x: str | None = None
    y: str | None = None
    color: str | None = None
    z: str | None = None
    size: str | None = None
    values: str | None = None
    path_columns: list[str] | None = None
    dimensions: list[str] | None = None
    aggregation: str = "none"
    title: str | None = None
    chart_engine: str = "plotly"
    bins: int = 30
    top_n: int | None = None
    scale_mode: str = "auto"
    export_png: bool = False
    stacked: bool = False
    # --- 派生状态 ---
    was_auto: bool = False
    auto_color_applied: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=_empty_coverage)
    stem: str = ""
    display_title: str = ""
    density_view: Any = None
    density_scale: dict[str, Any] | None = None
    stat_agg: dict[str, Any] | None = None


def prepare_chart_spec(raw_df: pd.DataFrame, spec: ChartSpec) -> None:
    """校验列、解析 auto 图型、准备（必要时拷贝/聚合）数据并生成轴标签。

    与拆分前 ``create_visualization`` 的前半段逐行等价：
    - 先校验请求列存在，保证自动选图能安全读取 ``df[x]/df[y]``；
    - 自动选图在"是否需要 copy"之前完成，否则 bar/line 等图型会被漏判；
    - 只有需要修改数据（聚合、布尔值本地化、top_n 筛选）时才 copy，
      大 DataFrame（100K+ 行）避免无谓深拷贝；
    - 语义校验在聚合/top_n 之前执行，报错文案携带修正建议。
    """
    x, y, color = spec.x, spec.y, spec.color
    requested = [value for value in [x, y, color, spec.z, spec.size, spec.values] if value]
    requested.extend(spec.path_columns or [])
    requested.extend(spec.dimensions or [])
    # 先校验列存在性，确保后续自动选图能安全读取 df[x]/df[y]。
    _checked_columns(raw_df, requested)

    # 自动选图：必须在"是否需要 copy"的判断之前解析出具体 chart_type，
    # 否则 needs_mutation 会因 chart_type=='auto' 误判为无需改动（bar/line/
    # area/pie/sunburst/treemap 需要 copy 做聚合/布尔本地化）。
    spec.was_auto = spec.chart_type == "auto"
    if spec.was_auto:
        _resolve_auto_chart_type(raw_df, spec, x=x, y=y, color=color)
    color = _resolve_auto_color(raw_df, spec, x=x, y=y, color=color)
    spec.color = color
    df = _prepared_dataframe(raw_df, spec, x=x, y=y, color=color)
    _validate_and_filter(spec, df, x=x, y=y, color=color)
    _aggregate_and_label(spec, x=x, y=y, color=color)


def _resolve_auto_chart_type(
    raw_df: pd.DataFrame, spec: ChartSpec, *, x: str | None, y: str | None, color: str | None
) -> None:
    """``chart_type="auto"`` 时按列类型/格式推断图型，并补默认聚合。"""
    spec.chart_type = _infer_chart_type(
        raw_df,
        x=x,
        y=y,
        color=color,
        z=spec.z,
        size=spec.size,
        values=spec.values,
        path_columns=spec.path_columns,
        dimensions=spec.dimensions,
        aggregation=spec.aggregation,
        top_n=spec.top_n,
    )
    # 纯分类计数场景（仅给 x、无 y）：自动用 count 聚合，避免空 series。
    if spec.chart_type == "bar" and y is None and spec.aggregation == "none":
        spec.aggregation = "count"
    # 没有 color 分组、且 x 含重复类别（如 product×channel 但没传 color）
    # 时，柱图会出现重复类别导致错乱，自动按 x 求和聚合对齐到每类一行。
    # 注：aggregation 仅对 bar/line/area 生效，饼图的重复聚合在下方单独处理。
    if spec.chart_type == "bar" and y is not None and spec.aggregation == "none" and not color:
        non_null = raw_df[x].dropna() if x else pd.Series(dtype=object)
        if len(non_null) > non_null.nunique():
            spec.aggregation = "sum"


def _resolve_auto_color(
    raw_df: pd.DataFrame, spec: ChartSpec, *, x: str | None, y: str | None, color: str | None
) -> str | None:
    """自动配色（Tableau 式默认行为）：auto 散点且无 color 时挑低基数分类列。"""
    # 自动挑一个低基数分类列（2~8 个取值）作为颜色维度。大点云单一
    # 颜色毫无区分度（实测用户反馈"不能就一种颜色"），数据里有现成
    # 的分组维度时必须利用；显式传 chart_type/color 时不干预。
    if not (
        spec.was_auto and spec.chart_type == "scatter" and not color and not spec.z and x and y
    ):
        return color
    for candidate in raw_df.columns:
        if candidate in (x, y):
            continue
        candidate_col = raw_df[candidate]
        if pd.api.types.is_numeric_dtype(candidate_col) or pd.api.types.is_datetime64_any_dtype(
            candidate_col
        ):
            continue
        try:
            n_unique = int(candidate_col.nunique(dropna=True))
        except Exception:
            continue
        if 2 <= n_unique <= 8:
            spec.auto_color_applied = True
            return candidate
    return color


def _prepared_dataframe(
    raw_df: pd.DataFrame, spec: ChartSpec, *, x: str | None, y: str | None, color: str | None
) -> pd.DataFrame:
    """按需 copy 并预先处理饼图重复类别，返回后续渲染使用的 DataFrame。"""
    _has_bool = any(
        col and col in raw_df.columns and pd.api.types.is_bool_dtype(raw_df[col])
        for col in (x, color)
    )
    needs_mutation = (
        spec.aggregation != "none"
        or spec.chart_type in {"bar", "line", "area", "pie", "sunburst", "treemap"}
        or spec.top_n is not None
        or _has_bool
    )
    df = raw_df.copy() if needs_mutation else raw_df
    # 自动选图推断为饼图、且 x 含重复类别（如 product×channel 但没传 color）
    # 时，饼图生成器按原始行会产生同名扇区错乱；这里预先按 x 聚合求和，
    # 两引擎饼图均按单行绘制（ECharts 生成器还会再 groupby，幂等）。
    if spec.was_auto and spec.chart_type == "pie" and y is not None and not color:
        non_null = df[x].dropna()
        if len(non_null) > non_null.nunique():
            df = df.groupby(x, dropna=False)[y].sum().reset_index()
    spec.df = df
    return df


def _validate_and_filter(
    spec: ChartSpec, df: pd.DataFrame, *, x: str | None, y: str | None, color: str | None
) -> None:
    """意义性防护（ID/常量/高基数）与 top_n 筛选。"""
    # 拦截 ID 列分布图、常量列图、高基数不可读图表，
    # 报错文案带修正建议（换列/传 top_n/换图型）供模型下一轮自行纠正。
    _validate_chart_semantics(
        df,
        chart_type=spec.chart_type,
        x=x,
        y=y,
        color=color,
        values=spec.values,
        path_columns=spec.path_columns,
        dimensions=spec.dimensions,
        top_n=spec.top_n,
    )
    if spec.top_n is not None:
        if not x or not 1 <= spec.top_n <= 500:
            raise ValueError("top_n 需要 x，且必须在 1 到 500 之间。")
        keep = df[x].value_counts(dropna=False).head(spec.top_n).index
        spec.df = df[df[x].isin(keep)].copy()


def _aggregate_and_label(
    spec: ChartSpec, *, x: str | None, y: str | None, color: str | None
) -> None:
    """可选聚合 → 布尔值本地化（含覆盖率标签）→ 生成轴标签。"""
    df = spec.df
    coverage: dict[str, Any] = _empty_coverage()
    if spec.aggregation != "none":
        if spec.chart_type not in {"bar", "line", "area"} or not x:
            raise ValueError("aggregation 仅用于带 x 的 bar/line/area。")
        df, y, coverage = _aggregate_for_chart(
            df,
            x=x,
            y=y,
            color=color,
            aggregation=spec.aggregation,
        )
        spec.df = df
        spec.y = y

    _localize_boolean_categories(df, [x, color])
    for item in coverage.get("missing_combinations", []):
        for column in (x, color):
            if column and item.get(column) in _BOOLEAN_VALUE_LABELS:
                item[column] = _BOOLEAN_VALUE_LABELS[item[column]]
    if coverage.get("color_levels"):
        coverage["color_levels"] = [
            _BOOLEAN_VALUE_LABELS.get(value, value) for value in coverage["color_levels"]
        ]
    spec.coverage = coverage

    labels = {column: _human_column_label(str(column)) for column in df.columns}
    if y and spec.aggregation in _AGGREGATION_LABELS:
        labels[y] = f"{_human_column_label(y)}（{_AGGREGATION_LABELS[spec.aggregation]}）"
    spec.labels = labels
