"""图表聚合辅助：分组聚合、缺失组合标记、布尔值本地化。

从 ``charts.py`` 拆分而来，负责把原始 DataFrame 转换为图表可用的聚合结果，
并标注缺失的类别组合（避免空白被误读为渲染失败）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .annotate import _append_title_note
from .constants import (
    _BOOLEAN_VALUE_LABELS,
    _HAS_RECORDS_COLUMN,
    _MAX_REINDEX_COMBINATIONS,
    _SAMPLE_COUNT_COLUMN,
)


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

    # x 层级顺序必须取自 groupby 聚合结果（已按 x 排序，时间列即时间顺序），
    # 若用原始行的出现顺序，下方 from_product reindex 会把月份轴打乱成
    # 01→02→04→06→05→03 这种乱序，趋势图完全不可读。
    x_levels = list(pd.unique(result[x].dropna()))
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
            font={
                "size": 10 if detailed_label else 15,
                "color": trace_colors.get(str(item[color]), "#7B8783"),
            },
            bgcolor="rgba(251,250,245,0.78)" if detailed_label else "rgba(0,0,0,0)",
            borderpad=2,
        )
    missing_label = "无样本" if aggregation in {"mean", "median", "min", "max"} else "无记录"
    _append_title_note(
        fig,
        f"组合覆盖 {coverage['observed_combinations']}/{coverage['total_combinations']}；"
        f"基线标记表示{missing_label}，不是数值为 0，也不是漏画",
    )
