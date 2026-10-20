"""把活动 DataFrame 暴露成只读 SQL 表 ``data``（内存 SQLite）。

CSV/Excel 会话没有 .db 文件时，``query_database(source=session)`` 走这里，
复用 ``sql_guard`` 与 SQLite 的语句超时，避免模型去沙箱里手写过滤。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

ACTIVE_TABLE = "data"


def _sql_ready_frame(frame: pd.DataFrame) -> pd.DataFrame:
    ready = frame.copy()
    for column in ready.columns:
        series = ready[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            ready[column] = series.astype(str)
        elif pd.api.types.is_bool_dtype(series):
            ready[column] = series.astype(int)
        elif str(series.dtype) == "Int64":
            ready[column] = series.astype(object)
    return ready


def connect_from_frame(frame: pd.DataFrame) -> sqlite3.Connection:
    """把当前表写入内存库的 ``data`` 表，返回可交给 sqlite_source.run_select 的连接。"""
    connection = sqlite3.connect(":memory:")
    _sql_ready_frame(frame).to_sql(ACTIVE_TABLE, connection, index=False, if_exists="replace")
    return connection


def schema_summary(frame: pd.DataFrame) -> dict[str, Any]:
    columns = [
        {
            "name": str(column),
            "type": str(frame[column].dtype),
            "not_null": bool(frame[column].notna().all()),
        }
        for column in frame.columns
    ]
    return {
        "table_count": 1,
        "tables": [{"table": ACTIVE_TABLE, "row_count": int(len(frame)), "columns": columns}],
    }

