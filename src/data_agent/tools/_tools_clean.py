"""清洗与视图变换工具：``clean_data`` / ``transform_data``。

从 ``builder.build_tools`` 按域拆出的工具定义；清洗护栏（最小行数、最大
删除比例）与错误文案保持原实现不变。
"""

from __future__ import annotations

import operator
from typing import Any, Literal

import pandas as pd
from langchain_core.tools import BaseTool, tool

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace

from ._cleaning import (
    _apply_missing_strategy,
    _handle_outliers,
    _normalize_column_names,
    _parse_numeric_columns,
    _trim_string_columns,
)
from ._limits import _TRANSFORM_LIMIT_MAX
from .charts import _checked_columns


def build_clean_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建清洗与视图变换类工具（clean_data、transform_data）。"""

    @tool
    def clean_data(
        columns: list[str] | None = None,
        drop_duplicates: bool = True,
        trim_strings: bool = True,
        normalize_column_names: bool = False,
        missing_strategy: Literal[
            "none", "drop", "mean", "median", "mode", "forward_fill", "backward_fill"
        ] = "none",
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
        before = {
            "rows": len(df),
            "columns": len(df.columns),
            "missing": int(df.isna().sum().sum()),
        }
        changes: list[str] = []

        if normalize_column_names:
            df, columns, datetime_columns, changed = _normalize_column_names(
                df, columns, datetime_columns
            )
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
        return json_text(
            {"status": "ok", "before": before, "after": after, "changes": changes, "output": output}
        )

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
                mask = series.astype("string").str.contains(
                    str(filter_value), case=False, na=False, regex=False
                )
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

    return [clean_data, transform_data]
