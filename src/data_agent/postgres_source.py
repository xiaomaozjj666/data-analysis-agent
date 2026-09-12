"""只读 PostgreSQL 数据源：连接、内省与受限查询。

与 :mod:`data_agent.sqlite_source` 并列的第二种数据源。语句白名单、结果上限、
JSON 安全转换这些驱动无关的规则都在 :mod:`data_agent.sql_guard`，本模块只负责
Postgres 特有的部分。

安全边界（与 SQLite 版同一取向，由常量与配置固定，不提供绕过参数）：

- 只读落在**服务端事务属性**上：连接建立后执行
  ``SET default_transaction_read_only = on``，此后每个事务都拒绝写入，并回读
  ``SHOW transaction_read_only`` 确认生效——配置失败就报错，而不是默默放行。
  语句白名单挡的是"不该跑的语句"，这一层挡的是"驱动之外的一切写入"。
- 超时用服务端 ``statement_timeout``（毫秒），失控查询由数据库自己取消。
- 连接串只从环境变量 ``DATA_AGENT_DATABASE_URL`` 读取，与项目里 S3/R2 凭证的
  既有约定一致；未配置时整个数据源不可用，而不是退化成某个默认连接。
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from data_agent.sql_guard import (
    collect_rows,
    quote_identifier,
    validate_select,
)

#: Postgres 连接串的环境变量名。与 S3/R2 凭证一样走环境变量，而不是另造凭证库。
DATABASE_URL_ENV = "DATA_AGENT_DATABASE_URL"

#: 单条查询的墙钟预算（毫秒），通过服务端 statement_timeout 强制执行。
QUERY_TIME_BUDGET_MS = 5_000

#: 表结构概览里逐表 COUNT(*) 的预算（毫秒），只用于"看一眼结构"。
_ROW_COUNT_BUDGET_MS = 500

#: 可选依赖：未安装 psycopg 时给出可操作的中文提示，而不是裸 ImportError。
try:  # pragma: no cover - 视安装环境而定
    import psycopg
except ImportError:  # pragma: no cover - 可选依赖
    psycopg = None  # type: ignore[assignment]

_INSTALL_HINT = (
    "未安装 PostgreSQL 驱动。请先安装：pip install \".[postgres]\""
    "（或 pip install \"psycopg[binary]>=3.2,<4\"）。"
)


def is_configured() -> bool:
    """判断 Postgres 数据源是否可用：驱动已装且连接串已配置。"""
    return psycopg is not None and bool((os.environ.get(DATABASE_URL_ENV) or "").strip())


def resolve_dsn() -> str:
    """读取连接串。

    Raises:
        ValueError: 未配置连接串，或驱动未安装。
    """
    dsn = (os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not dsn:
        raise ValueError(
            f"未配置 PostgreSQL 连接串。请设置环境变量 {DATABASE_URL_ENV}"
            "（如 postgresql://user:password@host:5432/dbname）后重启服务。"
        )
    if psycopg is None:
        raise ValueError(_INSTALL_HINT)
    return dsn


def connect_readonly(dsn: str | None = None, budget_ms: int = QUERY_TIME_BUDGET_MS) -> Any:
    """建立只读连接并配置查询预算。

    只读通过 ``default_transaction_read_only`` 在服务端生效，并且回读确认——
    如果这个会话属性没设置成功，说明连接已被限制（如云数据库的只读副本策略），
    必须报错而不是带着不确定的权限继续。

    Args:
        dsn: 连接串；缺省时读取 :data:`DATABASE_URL_ENV`。
        budget_ms: 单条查询超时（毫秒）。

    Returns:
        psycopg 连接。
    """
    if psycopg is None:
        raise ValueError(_INSTALL_HINT)
    target = (dsn or resolve_dsn()).strip()
    connection = psycopg.connect(target)
    try:
        # SET 必须在 autocommit 下执行：否则它们落在事务里，事务结束就被回滚，
        # "只读 + 超时"两项保护对一个不知情的调用者来说等于没设。
        connection.autocommit = True
        connection.execute(f"SET statement_timeout = {int(budget_ms)}")
        connection.execute("SET default_transaction_read_only = on")
        confirmed = connection.execute("SHOW transaction_read_only").fetchone()
        if not confirmed or str(confirmed[0]).lower() != "on":
            raise ValueError(
                "无法把本连接设置为只读（transaction_read_only 未生效），已中止连接。"
                "请确认账号具备设置会话参数的权限。"
            )
        connection.execute("SET search_path = public")
        connection.autocommit = False
    except Exception:
        connection.close()
        raise
    return connection


def list_tables(connection: Any, schema: str = "public") -> list[str]:
    """列出指定 schema 下的用户表（不含视图与系统表）。"""
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
        (schema,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def describe_table(connection: Any, table: str, schema: str = "public") -> list[dict[str, Any]]:
    """返回表的列定义。

    表名与 schema 名同样先收敛到真实表集合再拼接，与 SQLite 版同一原则。
    """
    known = list_tables(connection, schema)
    if table not in known:
        raise ValueError(f"表「{table}」不存在。可用表：{'、'.join(known) if known else '（无）'}")
    rows = connection.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()
    return [
        {"name": str(row[0]), "type": str(row[1] or ""), "not_null": row[1] is not None and str(row[2]) == "NO"}
        for row in rows
    ]


def count_rows(connection: Any, table: str, schema: str = "public", budget_ms: int = _ROW_COUNT_BUDGET_MS) -> int:
    """统计单表行数；超时返回 -1，不让"看一眼结构"变成长任务。"""
    try:
        connection.execute(f"SET statement_timeout = {int(budget_ms)}")
        row = connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()
        return int(row[0]) if row else -1
    except psycopg.errors.QueryCanceled:  # type: ignore[union-attr]
        connection.rollback()
        return -1


def schema_summary(connection: Any, schema: str = "public") -> dict[str, Any]:
    """汇总所有表的结构，供工具在发现模式（sql 留空）下返回。"""
    described: list[dict[str, Any]] = []
    for table in list_tables(connection, schema):
        described.append(
            {
                "table": table,
                "row_count": count_rows(connection, table, schema),
                "columns": describe_table(connection, table, schema),
            }
        )
    return {"table_count": len(described), "tables": described}


def run_select(
    connection: Any,
    sql: str,
    *,
    limit: int,
    budget_ms: int = QUERY_TIME_BUDGET_MS,
) -> dict[str, Any]:
    """执行只读查询并返回列名、行与截断标记。

    Raises:
        ValueError: 查询超时、语句非只读、驱动未安装或 SQL 执行失败（已中文化）。
    """
    statement = validate_select(sql)
    try:
        # 超时按调用入参收紧：连接级默认值可能被同会话的其他调用改过。
        connection.execute(f"SET statement_timeout = {int(budget_ms)}")
        cursor = connection.execute(statement)
        return collect_rows(cursor, limit)
    except psycopg.errors.QueryCanceled as exc:  # type: ignore[union-attr]
        connection.rollback()
        raise ValueError(
            f"查询超过 {budget_ms / 1000:g} 秒预算已被数据库取消。"
            "通常是跨表笛卡尔积或缺少过滤条件导致；请补上 WHERE、先用聚合把数据收敛，"
            "或改用带索引的列做连接。"
        ) from exc
    except psycopg.errors.ReadOnlySqlTransaction as exc:  # type: ignore[union-attr]
        connection.rollback()
        raise ValueError("连接处于只读事务，写入语句已被数据库拒绝。") from exc
    except psycopg.Error as exc:  # type: ignore[union-attr]
        connection.rollback()
        raise ValueError(f"SQL 执行失败：{exc}") from exc


def read_schema_frame(connection: Any, table: str, schema: str = "public") -> pd.DataFrame:
    """把指定表读成 DataFrame（默认表载入用；工具路径不走这里）。"""
    known = list_tables(connection, schema)
    if table not in known:
        raise ValueError(f"表「{table}」不存在。可用表：{'、'.join(known) if known else '（无）'}")
    return pd.read_sql_query(f"SELECT * FROM {quote_identifier(table)}", connection)
