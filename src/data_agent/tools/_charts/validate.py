"""图表意义性防护：列校验、标识符检测、高基数拦截、时间维度识别。

从 ``charts.py`` 拆分而来。报错文案面向 ReAct 模型：给出可直接执行的
修正建议（换列/传 top_n/换图型），让模型在下一轮工具调用中自行纠正，
而不是产出无意义图表。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .constants import (
    _CATEGORY_AXIS_MAX,
    _CATEGORY_X_CHART_TYPES,
    _CHART_TYPE_LABELS_ZH,
    _COLOR_MAX_CATEGORIES,
    _ID_CHECK_MIN_ROWS,
    _ID_LIKE_UNIQUE_RATIO,
    _ID_NAME_PATTERN,
    _ID_NAME_SUFFIXES_ZH,
    _PIE_MAX_CATEGORIES,
)


def _checked_columns(df: pd.DataFrame, columns: list[str] | None) -> list[str]:
    """验证并返回列名列表，列不存在时抛出带可用列提示的 ValueError。"""
    result = list(df.columns) if not columns else columns
    missing = [column for column in result if column not in df.columns]
    if missing:
        raise ValueError(f"列不存在：{missing}。可用列：{list(df.columns)}")
    return result


def _numeric_columns(df: pd.DataFrame, columns: list[str] | None = None) -> list[str]:
    """筛选数值类型列，无可用数值列时抛出 ValueError。"""
    candidates = (
        _checked_columns(df, columns) if columns else list(df.select_dtypes(include=np.number))
    )
    result = [column for column in candidates if pd.api.types.is_numeric_dtype(df[column])]
    if not result:
        raise ValueError("没有可用于该操作的数值列。")
    return result


def _name_looks_like_id(column: str) -> bool:
    """判断列名是否是典型的标识符命名（user_id / 订单编号 / UUID 等）。"""
    name = str(column).strip()
    return bool(_ID_NAME_PATTERN.search(name)) or name.endswith(_ID_NAME_SUFFIXES_ZH)


def _looks_like_id_column(df: pd.DataFrame, column: str, *, strict: bool) -> bool:
    """判断列是否是标识符列（对其画分布/分组图没有分析意义）。

    strict=True：命名命中或"近乎逐行唯一且非浮点"即判定（用于类别角色）；
    strict=False：仅命名命中且大部分唯一才判定（用于 scatter/line 等连续轴，
    避免把销售额这类天然近唯一的测量列误报为 ID）。
    """
    if pd.api.types.is_datetime64_any_dtype(df[column]):
        return False
    rows = len(df)
    if rows == 0:
        return False
    unique = int(df[column].nunique(dropna=True))
    ratio = unique / rows
    if _name_looks_like_id(str(column)) and ratio >= 0.5:
        return True
    if not strict:
        return False
    return (
        rows >= _ID_CHECK_MIN_ROWS
        and ratio >= _ID_LIKE_UNIQUE_RATIO
        and not pd.api.types.is_float_dtype(df[column])
    )


def _validate_chart_semantics(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None = None,
    y: str | None = None,
    color: str | None = None,
    values: str | None = None,
    path_columns: list[str] | None = None,
    dimensions: list[str] | None = None,
    top_n: int | None = None,
) -> None:
    """在渲染前拦截无分析意义的图表配置，抛出带修正建议的 ValueError。

    三类拦截：
    1. 常量列 —— 只有一个取值的列画任何图都不携带信息；
    2. 标识符列 —— ID/编号列的分布图、分组图、图例都没有分析价值；
    3. 高基数 —— 类别数超过可读阈值时要求传 top_n 或换图型。
    """
    rows = len(df)
    if rows < 2:
        return

    def _nunique(column: str) -> int:
        return int(df[column].nunique(dropna=True))

    def _is_datetime(column: str) -> bool:
        return pd.api.types.is_datetime64_any_dtype(df[column])

    def _is_continuous(column: str) -> bool:
        return pd.api.types.is_numeric_dtype(df[column]) and not pd.api.types.is_bool_dtype(
            df[column]
        )

    # 1) 常量列：任何角色都拒绝。
    constant_roles = [("x", x), ("y", y), ("color", color), ("values", values)]
    constant_roles += [("path_columns", column) for column in path_columns or []]
    constant_roles += [("dimensions", column) for column in dimensions or []]
    for role, column in constant_roles:
        if column and column in df.columns and _nunique(column) <= 1:
            raise ValueError(
                f"列「{column}」在当前数据中只有一个取值，作为 {role} 画图不携带任何信息；"
                "请换用有区分度的列，或先用 inspect_data 确认数据内容。"
            )

    # 2) 标识符列：类别角色做严格校验，连续轴角色只看命名。
    id_checks: list[tuple[str, str, bool]] = []
    if x:
        id_checks.append(("x", x, chart_type in _CATEGORY_X_CHART_TYPES))
    if y and chart_type in {"heatmap", "box", "violin"}:
        # heatmap 的 y 也是类别轴；box/violin 若把 ID 当数值 y 同样无意义。
        id_checks.append(("y", y, chart_type == "heatmap"))
    if color:
        id_checks.append(("color", color, not _is_continuous(color)))
    id_checks += [("path_columns", column, True) for column in path_columns or []]
    id_checks += [("dimensions", column, False) for column in dimensions or []]
    for role, column, strict in id_checks:
        if _looks_like_id_column(df, column, strict=strict):
            unique = _nunique(column)
            raise ValueError(
                f"列「{column}」疑似标识符列（{unique} 个唯一值 / 共 {rows} 行），"
                f"作为 {role} 画{_CHART_TYPE_LABELS_ZH.get(chart_type, chart_type)}没有分析意义；"
                "请改用业务类别列（如地区/产品/月份）或数值指标列。"
            )

    # 3) 高基数：超过可读阈值时给出可执行的修正方向。
    effective_x = min(_nunique(x), top_n or 10**9) if x else 0
    if chart_type == "pie" and x and effective_x > _PIE_MAX_CATEGORIES:
        raise ValueError(
            f"饼图类别过多（{_nunique(x)} 个，上限 {_PIE_MAX_CATEGORIES}），扇区不可读；"
            "请传 top_n（建议 ≤ 10）只展示头部类别，或改用 bar 图。"
        )
    if (
        chart_type in {"bar", "box", "violin"}
        and x
        and not _is_datetime(x)
        and effective_x > _CATEGORY_AXIS_MAX
    ):
        raise ValueError(
            f"x 轴类别过多（{_nunique(x)} 个，上限 {_CATEGORY_AXIS_MAX}），图表不可读；"
            "请传 top_n（建议 10-20）聚焦头部类别；若 x 是连续数值请改用 histogram。"
        )
    if chart_type == "heatmap":
        if x and not _is_datetime(x) and effective_x > _CATEGORY_AXIS_MAX:
            raise ValueError(
                f"热力图 x 轴类别过多（{_nunique(x)} 个，上限 {_CATEGORY_AXIS_MAX}）；请传 top_n 或改用基数更低的列。"
            )
        if y and not _is_datetime(y) and _nunique(y) > _CATEGORY_AXIS_MAX:
            raise ValueError(
                f"热力图 y 轴类别过多（{_nunique(y)} 个，上限 {_CATEGORY_AXIS_MAX}）；请改用基数更低的列。"
            )
    if color and not _is_continuous(color) and _nunique(color) > _COLOR_MAX_CATEGORIES:
        raise ValueError(
            f"color 列「{color}」有 {_nunique(color)} 个类别（上限 {_COLOR_MAX_CATEGORIES}），图例不可读；"
            "请去掉 color，或改用基数更低的分类列。"
        )
    for column in path_columns or []:
        if _nunique(column) > _CATEGORY_AXIS_MAX:
            raise ValueError(
                f"path_columns 中「{column}」有 {_nunique(column)} 个类别（上限 {_CATEGORY_AXIS_MAX}），"
                "层级图不可读；请改用基数更低的层级列。"
            )


def _looks_like_datetime_series(series: pd.Series) -> bool:
    """判断一个序列是否应被视为时间维度（含被读成字符串的日期）。

    直接是 datetime64 立即返回 True；否则仅对 object/string 列抽样尝试
    解析，命中率 ≥ 80% 才判定为日期，避免把普通字符串列误判成时间。
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if not pd.api.types.is_string_dtype(series):
        return False
    sample = series.dropna().head(20)
    if len(sample) == 0:
        return False
    try:
        parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
    except Exception:
        return False
    return bool(parsed.notna().mean() >= 0.8)
