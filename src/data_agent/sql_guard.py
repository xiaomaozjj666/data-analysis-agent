"""驱动无关的只读 SQL 护栏：语句校验、结果截断与 JSON 安全转换。

SQLite 与 Postgres 两个数据源共用这一层。驱动差异（连接方式、超时机制、异常类型、
内省 SQL）留在各自模块里，但"什么语句能跑、结果能有多大、取值怎么序列化"属于
同一套产品规则，必须只有一份实现——两份就会在某次改护栏时只改一边。
"""

from __future__ import annotations

import os
import re
from typing import Any

#: SQL 墙钟预算环境变量。默认 30 秒：5 秒会把合法的百成千行 JOIN/GROUP BY
#: 掐死，120 秒封顶避免笛卡尔积把分析线程占满。
SQL_TIMEOUT_SECONDS_ENV = "DATA_AGENT_SQL_TIMEOUT_SECONDS"
_DEFAULT_SQL_TIMEOUT_SECONDS = 30.0
_MIN_SQL_TIMEOUT_SECONDS = 1.0
_MAX_SQL_TIMEOUT_SECONDS = 120.0


def query_time_budget_ms() -> int:
    """当前进程的单条查询墙钟预算（毫秒）。"""
    raw = os.getenv(SQL_TIMEOUT_SECONDS_ENV, "").strip()
    try:
        seconds = float(raw) if raw else _DEFAULT_SQL_TIMEOUT_SECONDS
    except ValueError:
        seconds = _DEFAULT_SQL_TIMEOUT_SECONDS
    seconds = min(max(seconds, _MIN_SQL_TIMEOUT_SECONDS), _MAX_SQL_TIMEOUT_SECONDS)
    return int(seconds * 1000)

#: 单条查询返回的最大行数（硬上限，工具的 limit 参数只能更小）。
MAX_RESULT_ROWS = 5_000

#: 单条查询返回的最大列数。
MAX_RESULT_COLUMNS = 500

#: 允许作为查询起点的语句前缀（小写比较）。只读意图的显式白名单。
ALLOWED_STATEMENT_PREFIXES: tuple[str, ...] = ("select", "with")

#: 明确拒绝的语句前缀，命中时给出针对性的错误说明而不是笼统的"不支持"。
FORBIDDEN_STATEMENTS: dict[str, str] = {
    "insert": "INSERT 会写入数据库",
    "update": "UPDATE 会修改数据库内容",
    "delete": "DELETE 会删除数据",
    "drop": "DROP 会删除表或索引",
    "alter": "ALTER 会修改表结构",
    "create": "CREATE 会新建对象",
    "replace": "REPLACE 会覆盖数据",
    "truncate": "TRUNCATE 会清空表",
    "attach": "ATTACH 可以打开数据库之外的任意文件",
    "detach": "DETACH 会改变连接状态",
    "pragma": "PRAGMA 的部分选项会写入数据库",
    "vacuum": "VACUUM 会重写数据库文件",
    "reindex": "REINDEX 会重建索引",
    "begin": "事务控制语句不允许使用",
    "commit": "事务控制语句不允许使用",
    "rollback": "事务控制语句不允许使用",
    # 数仓常见但同样不属于只读查询的语句（BigQuery / Snowflake / Databricks）。
    "merge": "MERGE 会写入或更新数仓表",
    "call": "CALL 会执行存储过程",
    "use": "USE 会改变会话上下文",
    "set": "SET 会改变会话或系统配置",
    "grant": "GRANT 会修改权限",
    "revoke": "REVOKE 会修改权限",
    "export": "EXPORT DATA 会写出数据",
    "copy": "COPY 会在数据库与外部存储之间搬运数据",
    "unload": "UNLOAD 会把数据导出到外部存储",
    "declare": "DECLARE 会创建游标或变量",
    "execute": "EXECUTE 会执行动态语句",
    "prepare": "PREPARE 会创建可执行语句",
}


_DOLLAR_QUOTE_RE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")


def _semicolon_outside_strings(statement: str) -> bool:
    """字符串/注释/引用标识符之外是否还有分号（多语句）。

    支持单引号、双引号、反引号标识符（BigQuery / Databricks / MySQL）、
    美元引号字符串（Postgres ``$$...$$`` / ``$tag$...$tag$``）、行注释与块注释。
    旧实现只跟踪单双引号，会把注释里的分号误判成多语句，也会漏掉反引号
    与美元引号里的分号——两种都是真实的误放/误杀。
    """
    index = 0
    length = len(statement)
    in_single = False
    in_double = False
    in_backtick = False
    dollar_tag = ""
    while index < length:
        char = statement[index]
        if dollar_tag:
            if statement.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = ""
            else:
                index += 1
            continue
        if in_single or in_double or in_backtick:
            if char == "\\" and in_single:
                index += 2
                continue
            if char == "'" and in_single:
                in_single = False
            elif char == '"' and in_double:
                in_double = False
            elif char == "`" and in_backtick:
                in_backtick = False
            index += 1
            continue
        if char == "-" and statement.startswith("--", index):
            newline = statement.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if char == "/" and statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char == "$":
            match = _DOLLAR_QUOTE_RE.match(statement, index)
            if match:
                dollar_tag = match.group(0)
                index = match.end()
                continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "`":
            in_backtick = True
        elif char == ";":
            return True
        index += 1
    return False


def validate_select(sql: str) -> str:
    """校验语句是只读查询，返回去掉首尾空白与结尾分号的语句。

    Raises:
        ValueError: 语句为空、以被禁止的关键字开头，或不是查询语句。
    """
    statement = (sql or "").strip().rstrip(";").strip()
    if not statement:
        raise ValueError("sql 不能为空；留空调用本工具可先获取库内表结构。")
    if _semicolon_outside_strings(statement):
        raise ValueError("只允许单条 SELECT / WITH 语句，禁止用分号拼接多条 SQL。")
    first_word = statement.lower().split(None, 1)[0]
    if first_word in FORBIDDEN_STATEMENTS:
        raise ValueError(
            f"只允许只读查询：{FORBIDDEN_STATEMENTS[first_word]}，本工具不能执行 {first_word.upper()}。"
            "如需把查询结果变成新的活动数据集，请改用 adopt=true。"
        )
    if first_word not in ALLOWED_STATEMENT_PREFIXES:
        # 注释开头的语句也走这里：首词会是 "--" 或 "/*"，同样不在白名单内。
        raise ValueError(
            f"只允许 SELECT 或 WITH 开头的查询语句，收到的是「{first_word.upper()}」。"
        )
    return statement


def to_json_safe(value: Any) -> Any:
    """把数据库返回值转成可 JSON 序列化的形式。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        # BLOB/BYTEA 直接塞进 JSON 会变成不可读的乱码；给出长度占位更便于判断。
        return f"<blob:{len(bytes(value))} bytes>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # datetime / Decimal / UUID 等驱动自定义类型统一转字符串，
    # 保留可读性，也避免 json_text 抛 TypeError。
    return str(value)


def collect_rows(cursor: Any, limit: int) -> dict[str, Any]:
    """从游标收取结果并施加行数/列数上限。

    在 ``cursor.execute`` 成功之后调用。假设调用方已经为连接配置了查询超时。

    Returns:
        ``{"columns": [...], "rows": [[...]], "row_count": n, "truncated": bool}``。

    Raises:
        ValueError: 结果列数超过上限。
    """
    capped = max(1, min(int(limit), MAX_RESULT_ROWS))
    description = cursor.description or []
    if len(description) > MAX_RESULT_COLUMNS:
        raise ValueError(
            f"查询返回 {len(description)} 列，超过 {MAX_RESULT_COLUMNS} 列上限。"
            "请显式选择需要的列，避免 SELECT * 带来用不上的宽表。"
        )
    columns = [str(item[0]) for item in description]
    fetched = cursor.fetchmany(capped)
    truncated = cursor.fetchone() is not None
    rows: list[list[Any]] = [[to_json_safe(value) for value in row] for row in fetched]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def quote_identifier(name: str, dialect: str = "default") -> str:
    """把表名/列名包成标识符；按方言选择引号并转义。

    SQLite / Postgres 用双引号；BigQuery / Databricks / MySQL 用反引号。
    默认行为保持不变，新增方言只影响显式传参的调用方。
    """
    dialect_key = (dialect or "default").strip().lower()
    if dialect_key in {"bigquery", "databricks", "mysql", "hive", "spark"}:
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'
