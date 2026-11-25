"""数据集导出工具：``export_data``。

从 ``builder.build_tools`` 按域拆出的工具定义；文件名清洗规则与产物登记
行为保持原实现不变。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool, tool

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace


def build_export_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建导出类工具（export_data）。"""

    @tool
    def export_data(
        format: Literal["csv", "xlsx", "parquet"] = "csv", filename: str = "analysis_result"
    ) -> str:
        """Export the current active dataset as CSV, Excel or Parquet and register the artifact."""
        safe_stem = (
            re.sub(r"[^\w\-\u4e00-\u9fff]", "_", Path(filename).stem)[:60] or "analysis_result"
        )
        path = workspace.save_dataframe(f"{safe_stem}.{format}")
        return json_text({"status": "ok", "rows": len(workspace.dataframe), "output": path})

    return [export_data]
