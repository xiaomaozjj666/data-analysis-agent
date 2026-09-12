"""只读 SQLite 数据源：表发现、结构描述与受限查询。

SQLite 文件在本项目里是一种**一等可上传数据源**：上传后默认载入最大的表作为活动
数据集，其余表交给 ``query_database`` 工具查询。这样既复用了既有的上传/校验/前端
链路（不需要新的接口或界面），又让跨表 JOIN 这类只有数据库才能表达的操作用得上
——那正是数据库相对单表 CSV 的价值所在。

安全边界（与 ``clean_data``、``join_datasets`` 同一取向：由常量固定，不提供
绕过参数）：

- 连接以 ``mode=ro`` 只读打开，写入在驱动层即被拒绝；
- 语句只允许 ``SELECT`` / ``WITH`` 开头，``PRAGMA`` / ``ATTACH`` / DDL / DML
  一律拒绝（``PRAGMA`` 可以写库，``ATTACH`` 可以打开任意文件，都不能放行）；
- 查询超时用 ``sqlite3`` 的 progress handler 实现：SQLite 本身没有语句级超时，
  而一张失控的跨表笛卡尔积会一直跑下去，把分析线程占死。progress handler 每
  N 个虚拟指令回调一次，超预算即中断查询；
- 结果行数与列数都有上限，避免单条查询把内存吃光。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from data_agent.sql_guard import (
    MAX_RESULT_COLUMNS as MAX_RESULT_COLUMNS,
)
from data_agent.sql_guard import (
    MAX_RESULT_ROWS as MAX_RESULT_ROWS,
)
from data_agent.sql_guard import (
    collect_rows,
)
from data_agent.sql_guard import (
    quote_identifier as _quote_identifier,
)
from data_agent.sql_guard import (
    to_json_safe as to_json_safe,
)
from data_agent.sql_guard import (
    validate_select as validate_select,
)

#: 视为 SQLite 数据源的扩展名。
SQLITE_EXTENSIONS: frozenset[str] = frozenset({".db", ".sqlite", ".sqlite3"})

#: 单条查询的墙钟预算（毫秒）。SQLite 无语句级超时，靠 progress handler 兜底。
QUERY_TIME_BUDGET_MS = 5_000

#: progress handler 的回调间隔（虚拟指令数）。太小会拖慢查询，太大则超时不准。
_PROGRESS_CHECK_INTERVAL = 1_000

#: 默认表选举时最多统计多少张表的行数。逐表 COUNT(*) 在表很多时开销可观，
#: 超出部分不参与选举（会在警告里说明）。
_DEFAULT_TABLE_SCAN_LIMIT = 20

#: 表选举与结构概览里逐表 COUNT(*) 的预算。这些只是"看一眼结构"，不该拖慢
#: 上传：最坏 20 张表 × 500ms = 10s 封顶，而不是按查询预算的 5s/表 放大到 100s。
_ROW_COUNT_BUDGET_MS = 500


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """以只读方式打开 SQLite 文件。

    用 URI 模式的 ``mode=ro`` 而不是打开后再靠语句白名单兜底：只读在驱动层生效，
    任何写入尝试都会直接失败，不依赖上层检查是否写全。

    Args:
        path: SQLite 文件路径。

    Returns:
        只读连接。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件不是可识别的 SQLite 数据库。
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"数据文件不存在：{resolved}")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - 路径已确认存在
        raise ValueError(f"无法打开 SQLite 数据库：{exc}") from exc
    try:
        # 触碰一次 schema：非 SQLite 文件（如改了扩展名的 CSV）会在此失败，
        # 而不是等到查询时才报一个难以理解的错。
        connection.execute("SELECT name FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise ValueError(
            f"文件不是有效的 SQLite 数据库：{resolved.name}（{exc}）。"
            "请确认上传的是 .db/.sqlite 文件，而不是改了扩展名的其他格式。"
        ) from exc
    return connection


def list_tables(connection: sqlite3.Connection) -> list[str]:
    """列出数据库中的用户表（排除 SQLite 内部表）。"""
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


@contextmanager
def _query_deadline(connection: sqlite3.Connection, budget_ms: int) -> Iterator[None]:
    """在块内为连接装载墙钟超时中断器，退出时复原。

    progress handler 每 ``_PROGRESS_CHECK_INTERVAL`` 个虚拟指令回调一次，超预算
    返回非零值使 SQLite 中断当前查询（抛出 ``OperationalError: interrupted``）。
    这是 SQLite 上唯一可靠的语句级超时手段——``connect(timeout=)`` 只管锁等待，
    管不了查询耗时。
    """
    started = time.monotonic()

    def handler() -> int:
        return 1 if (time.monotonic() - started) * 1000 > budget_ms else 0

    connection.set_progress_handler(handler, _PROGRESS_CHECK_INTERVAL)
    try:
        yield
    finally:
        connection.set_progress_handler(None, 0)


def count_rows(connection: sqlite3.Connection, table: str, budget_ms: int = QUERY_TIME_BUDGET_MS) -> int:
    """统计单表行数；超预算返回 -1（用于默认表选举与结构概览）。

    COUNT(*) 在 SQLite 上是一次 B-tree 扫描，对超大表可能很慢。这里允许它在超时后
    放弃并返回 -1，而不是让"看一眼结构"变成一次长任务。
    """
    with _query_deadline(connection, budget_ms):
        try:
            return int(connection.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0])
        except sqlite3.OperationalError:
            return -1


def describe_table(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """返回表的列定义。

    表名不直接拼接进 SQL，而是先与真实表列表比对——``PRAGMA table_info`` 的表名
    参数无法用占位符绑定，只能拼字符串，所以必须先把入参收敛到已知的安全集合。
    """
    known = list_tables(connection)
    if table not in known:
        raise ValueError(f"表「{table}」不存在。可用表：{'、'.join(known) if known else '（无）'}")
    rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [
        {"name": str(row[1]), "type": str(row[2] or ""), "not_null": bool(row[3]), "primary_key": bool(row[5])}
        for row in rows
    ]


def pick_default_table(connection: sqlite3.Connection) -> str:
    """选出行数最多的表作为默认活动数据集。

    Raises:
        ValueError: 库中没有任何表——返回类型保持为 str，调用方不必再处理 None
            这个实际上不可达的分支。
    """
    tables = list_tables(connection)
    if not tables:
        raise ValueError("SQLite 数据库中没有任何表。")
    best_table, best_rows = tables[0], -1
    for table in tables[:_DEFAULT_TABLE_SCAN_LIMIT]:
        rows = count_rows(connection, table, budget_ms=_ROW_COUNT_BUDGET_MS)
        if rows > best_rows:
            best_table, best_rows = table, rows
    return best_table


def run_select(
    connection: sqlite3.Connection,
    sql: str,
    *,
    limit: int,
    budget_ms: int = QUERY_TIME_BUDGET_MS,
) -> dict[str, Any]:
    """执行只读查询并返回列名、行与截断标记。

    Args:
        connection: 只读连接。
        sql: 查询语句，内部先做只读校验。
        limit: 返回行数上限，内部再夹到 ``MAX_RESULT_ROWS``。
        budget_ms: 墙钟预算，超时中断查询。

    Returns:
        ``{"columns": [...], "rows": [[...]], "row_count": n, "truncated": bool}``。

    Raises:
        ValueError: 查询超时、列数超限、语句非只读或 SQL 执行失败（已中文化）。
    """
    statement = validate_select(sql)
    with _query_deadline(connection, budget_ms):
        try:
            cursor = connection.execute(statement)
            result = collect_rows(cursor, limit)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise ValueError(
                    f"查询超过 {budget_ms / 1000:g} 秒预算已被中断。"
                    "通常是跨表笛卡尔积或缺少过滤条件导致；请补上 WHERE、先用聚合把数据收敛，"
                    "或改用带索引的列做连接。"
                ) from exc
            raise ValueError(f"SQL 执行失败：{exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"SQL 执行失败：{exc}") from exc
    return result


def schema_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    """汇总所有表的结构，供工具在只读取模式（sql 留空）下返回。"""
    tables = list_tables(connection)
    described: list[dict[str, Any]] = []
    for table in tables:
        described.append(
            {
                "table": table,
                "row_count": count_rows(connection, table),
                "columns": describe_table(connection, table),
            }
        )
    return {"table_count": len(tables), "tables": described}


def find_source(input_dir: str | Path) -> Path | None:
    """在会话工作区内找出可查询的 SQLite 文件。

    一个会话通常只上传一个文件，但导入的会话归档或本地手工放置都可能带来多个
    输入文件，因此按文件名排序取第一个，保证同一会话内选择稳定可复现。

    Returns:
        找到的数据库路径；没有则返回 None。
    """
    base = Path(input_dir)
    try:
        children = sorted(base.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    for path in children:
        if path.is_file() and path.suffix.lower() in SQLITE_EXTENSIONS:
            return path.resolve()
    return None


def resolve_session_source(source_path: str | Path | None, input_dir: str | Path) -> Path | None:
    """定位本会话可查询的 SQLite 文件。

    优先用会话的原始数据文件——正常上传路径下它就是用户传进来的那个库，语义最明确；
    只有当原始文件不是 SQLite（例如会话由 CSV 建立、库里另有 .db 输入文件）时，才退回
    按文件名在输入目录里查找。
    """
    if source_path is not None:
        candidate = Path(source_path)
        if candidate.suffix.lower() in SQLITE_EXTENSIONS and candidate.is_file():
            return candidate.resolve()
    return find_source(input_dir)


def read_default_table(path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """把默认表读成 DataFrame，并给出加载警告。

    默认表取行数最多的一张：用户上传一个数据库通常是想分析主表，而"行数最多"是
    一个不需要额外元数据就能算出来的稳定判据。实际选中的表会写进警告，避免用户
    以为载入的是别的表。

    Args:
        path: SQLite 文件路径。

    Returns:
        ``(DataFrame, 警告列表)``。

    Raises:
        ValueError: 文件非法、库内没有表，或默认表读取失败。
    """
    warnings: list[str] = []
    connection = connect_readonly(path)
    try:
        tables = list_tables(connection)
        if not tables:
            raise ValueError("SQLite 数据库中没有任何表。")
        default_table = pick_default_table(connection)
        if len(tables) > _DEFAULT_TABLE_SCAN_LIMIT:
            warnings.append(
                f"库中共 {len(tables)} 张表，默认表仅在前 {_DEFAULT_TABLE_SCAN_LIMIT} 张中选举；"
                "如需分析其他表，请用 query_database 查询后 adopt 为活动数据集。"
            )
        frame = pd.read_sql_query(f"SELECT * FROM {_quote_identifier(default_table)}", connection)
        warnings.append(
            f"数据来自 SQLite 的「{default_table}」表（库中共 {len(tables)} 张表，已按行数选取主表）；"
            "其余表可用 query_database 查询。"
        )
        return frame, warnings
    finally:
        connection.close()
