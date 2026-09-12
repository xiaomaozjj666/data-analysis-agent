"""驱动无关的只读 SQL 护栏：语句校验、结果截断与 JSON 安全转换。

SQLite 与 Postgres 两个数据源共用这一层。驱动差异（连接方式、超时机制、异常类型、
内省 SQL）留在各自模块里，但"什么语句能跑、结果能有多大、取值怎么序列化"属于
同一套产品规则，必须只有一份实现——两份就会在某次改护栏时只改一边。
"""

from __future__ import annotations

from typing import Any

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
}


def validate_select(sql: str) -> str:
    """校验语句是只读查询，返回去掉首尾空白与结尾分号的语句。

    Raises:
        ValueError: 语句为空、以被禁止的关键字开头，或不是查询语句。
    """
    statement = (sql or "").strip().rstrip(";").strip()
    if not statement:
        raise ValueError("sql 不能为空；留空调用本工具可先获取库内表结构。")
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


def quote_identifier(name: str) -> str:
    """把表名/列名包成双引号标识符，内部双引号翻倍转义。

    SQLite 与 Postgres 都用双引号引用标识符，因此两个驱动共用这一份实现。
    """
    return '"' + name.replace('"', '""') + '"'
