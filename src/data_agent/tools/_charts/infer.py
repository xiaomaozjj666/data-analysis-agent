"""自动选图：根据数据"类型 + 格式"推断最合适的图表类型。

从 ``charts.py`` 拆分而来。chart_type="auto" 时由本函数决策，
让工具随分析的数据自动选图，而不是依赖调用方每次都显式指定。
"""

from __future__ import annotations

import pandas as pd

from .validate import _looks_like_datetime_series


def _infer_chart_type(
    df: pd.DataFrame,
    *,
    x: str | None,
    y: str | None,
    color: str | None,
    z: str | None,
    size: str | None,
    values: str | None,
    path_columns: list[str] | None,
    dimensions: list[str] | None,
    aggregation: str,
    top_n: int | None = None,
) -> str:
    """根据数据列的类型与格式自动推断最合适的图表类型。

    推断优先级（与"数据类型 + 格式"对应）：
    1. 层级结构（path_columns）→ sunburst（多层级时仍用旭日，treemap 需显式指定）
    2. 三维（z 且 x/y 都在）→ scatter_3d
    3. 多维数值（dimensions）→ scatter_matrix（≥3 维）或 scatter（2 维）
    4. 无 x/y 但数值列 ≥3 → correlation_heatmap（探索数值间相关性）
    5. 单数值列分布（无 x，仅 y 为数值）→ histogram
    6. x 为时间（datetime/可解析日期字符串）→ line（时间序列，有 y 时）
    7. x 连续数值 + y 数值 → scatter（两数值关系）
    8. x 连续数值（无 y）→ histogram（分布）；唯一值很少时退化为 bar 计数
    9. x 分类 + y 数值：有 color → bar（分组对比）；无 color 且类别少（≤8）→ pie（构成占比）；否则 bar
    10. x 分类（无 y）→ bar（计数/频次）

    无法推断时抛出带建议的 ValueError，交由调用方（ReAct 模型）下一轮纠正。
    """
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]

    # 1) 层级结构
    if path_columns:
        return "sunburst"
    # 2) 三维
    if z and x and y:
        return "scatter_3d"
    # 3) 多维数值（仅在未显式给 x/y 时据此推断，避免覆盖明确的 x/y 意图）
    if dimensions and not x and not y:
        dims = [d for d in dimensions if d in df.columns and pd.api.types.is_numeric_dtype(df[d])]
        if len(dims) >= 3:
            return "scatter_matrix"
        if len(dims) == 2:
            return "scatter"
    # 4) 无 x/y 但多数值列 → 相关性热力图
    if not x and not y and len(numeric_cols) >= 3:
        return "correlation_heatmap"
    # 5) 单数值分布
    if x is None and y is not None and y in df.columns and pd.api.types.is_numeric_dtype(df[y]):
        return "histogram"

    # 实在没有可用维度：若有数值列就做其分布，否则报错引导
    if x is None and y is None:
        if numeric_cols:
            return "histogram"
        raise ValueError(
            "auto 模式下无法确定图表类型：请提供 x 或 y，或显式指定 chart_type"
            "（如 bar/line/scatter/pie/histogram）。"
        )

    # 至此 x 必然非 None
    x_col = x  # type: ignore[assignment]
    x_is_datetime = _looks_like_datetime_series(df[x_col])
    x_is_numeric = pd.api.types.is_numeric_dtype(df[x_col])
    y_numeric = y is not None and y in df.columns and pd.api.types.is_numeric_dtype(df[y])

    # 6) 时间序列
    if x_is_datetime:
        return "line" if y_numeric else "bar"
    # 7) 两数值关系
    if x_is_numeric and y_numeric:
        return "scatter"
    # 8) 连续数值分布
    if x_is_numeric and not y_numeric:
        if df[x_col].nunique(dropna=True) <= 12:
            return "bar"
        return "histogram"
    # 9) 分类 + 数值
    if (not x_is_numeric) and y_numeric:
        if color:
            return "bar"
        n_unique = int(df[x_col].nunique(dropna=True))
        if n_unique <= 8:
            return "pie"
        return "bar"
    # 10) 分类计数
    if (not x_is_numeric) and not y_numeric:
        return "bar"
    # 兜底（上述分支已覆盖全部 x/y 类型组合，此处仅防御性保留）
    return "bar"  # pragma: no cover - 防御性兜底
