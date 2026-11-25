"""概况检查与格式修复工具：``inspect_data`` / ``repair_data_format``。

从 ``builder.build_tools`` 按域拆出的工具定义；闭包绑定同一个
``DataWorkspace`` 实例。工具名、参数 schema 与描述文案与原实现逐字一致。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace


def build_check_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建概况检查类工具（inspect_data、repair_data_format）。"""

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

    return [inspect_data, repair_data_format]
