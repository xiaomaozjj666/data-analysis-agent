"""SQLite 只读数据源测试：访问边界、护栏、查询与接管数据集。

重点在两处：一是**只读必须真的只读**（连接层拒绝写入，而不只是语句白名单挡了一层）；
二是**失控查询必须能被中断**——SQLite 没有语句级超时，一张跨表笛卡尔积会一直跑下去，
把分析线程占死。这两条都靠真实行为验证，不看实现细节。
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest

from data_agent.sqlite_source import (
    MAX_RESULT_COLUMNS,
    QUERY_TIME_BUDGET_MS,
    connect_readonly,
    count_rows,
    describe_table,
    find_source,
    list_tables,
    pick_default_table,
    read_default_table,
    resolve_session_source,
    run_select,
    schema_summary,
    to_json_safe,
    validate_select,
)
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _make_db(path, *, big_rows: int = 6, with_second: bool = True) -> None:
    """构造一个测试库：orders 为主表，region 为维表。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE region (code TEXT PRIMARY KEY, manager TEXT)")
        connection.executemany(
            "INSERT INTO region VALUES (?, ?)",
            [("east", "张三"), ("west", "李四")],
        )
        connection.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, code TEXT, amount REAL)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(index, "east" if index % 2 else "west", float(index) * 10) for index in range(1, big_rows + 1)],
        )
        if not with_second:
            connection.execute("DELETE FROM region")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "shop.db"
    _make_db(path)
    return path


def _db_workspace(tmp_path, name: str = "db_ws") -> DataWorkspace:
    """建一个以 SQLite 文件为数据源的会话工作区。"""
    source = tmp_path / f"{name}.db"
    _make_db(source)
    workspace = DataWorkspace(tmp_path / "runs", session_id=name)
    workspace.save_upload(source.name, source.read_bytes())
    workspace.load(workspace.input_dir / source.name)
    return workspace


def _tools(workspace) -> dict:
    return {tool.name: tool for tool in build_tools(workspace)}


# ---------------------------------------------------------------------------
# 连接层：只读必须是真的只读
# ---------------------------------------------------------------------------


def test_connect_readonly_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "nope.db")


def test_connect_readonly_rejects_non_sqlite_content(tmp_path):
    """扩展名是 .db 但内容不是 SQLite 时必须给出可理解的错误。"""
    fake = tmp_path / "fake.db"
    fake.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不是有效的 SQLite 数据库"):
        connect_readonly(fake)


def test_connection_refuses_writes_at_driver_level(db_path):
    """只读由驱动层保证：即使绕过语句白名单，写入也会失败。"""
    connection = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO orders VALUES (99, 'east', 1.0)")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 表发现与结构描述
# ---------------------------------------------------------------------------


def test_list_tables_excludes_internal_tables(db_path):
    connection = connect_readonly(db_path)
    try:
        assert list_tables(connection) == ["orders", "region"]
    finally:
        connection.close()


def test_count_rows_and_describe_table(db_path):
    connection = connect_readonly(db_path)
    try:
        assert count_rows(connection, "orders") == 6
        described = describe_table(connection, "orders")
        assert [item["name"] for item in described] == ["order_id", "code", "amount"]
        assert described[0]["primary_key"] is True
        assert described[0]["type"] == "INTEGER"
    finally:
        connection.close()


def test_describe_table_rejects_unknown_name_and_lists_available(db_path):
    connection = connect_readonly(db_path)
    try:
        with pytest.raises(ValueError, match="不存在") as exc:
            describe_table(connection, "missing")
        assert "orders" in str(exc.value)
    finally:
        connection.close()


def test_describe_table_rejects_injected_table_name(db_path):
    """表名要拼进 PRAGMA，必须先把入参收敛到真实表集合。"""
    connection = connect_readonly(db_path)
    try:
        with pytest.raises(ValueError, match="不存在"):
            describe_table(connection, 'orders"; DROP TABLE orders; --')
        # 注入尝试不能改变库结构
        assert "orders" in list_tables(connection)
    finally:
        connection.close()


def test_pick_default_table_chooses_the_largest(db_path):
    connection = connect_readonly(db_path)
    try:
        assert pick_default_table(connection) == "orders"
    finally:
        connection.close()


def test_pick_default_table_raises_on_empty_database(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    connection = connect_readonly(path)
    try:
        with pytest.raises(ValueError, match="没有任何表"):
            pick_default_table(connection)
    finally:
        connection.close()


def test_schema_summary_reports_every_table(db_path):
    connection = connect_readonly(db_path)
    try:
        summary = schema_summary(connection)
        assert summary["table_count"] == 2
        assert {item["table"] for item in summary["tables"]} == {"orders", "region"}
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 语句白名单
# ---------------------------------------------------------------------------


def test_validate_select_accepts_select_and_with():
    assert validate_select("SELECT 1") == "SELECT 1"
    assert validate_select("  select * from t;  ") == "select * from t"
    assert validate_select("WITH x AS (SELECT 1) SELECT * FROM x").lower().startswith("with")


def test_validate_select_rejects_empty():
    with pytest.raises(ValueError, match="sql 不能为空"):
        validate_select("   ")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN b INT",
        "CREATE TABLE t (a INT)",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE 'other.db' AS other",
        "VACUUM",
        "BEGIN",
    ],
)
def test_validate_select_rejects_writing_statements(statement):
    with pytest.raises(ValueError, match="只允许只读查询"):
        validate_select(statement)


def test_validate_select_rejects_comment_prefixed_statement():
    """注释开头不能让语句绕进白名单。"""
    with pytest.raises(ValueError, match="只允许 SELECT 或 WITH"):
        validate_select("-- drop everything\nSELECT 1")


def test_validate_select_rejects_unknown_leading_keyword():
    with pytest.raises(ValueError, match="只允许 SELECT 或 WITH"):
        validate_select("EXPLAIN SELECT 1")


# ---------------------------------------------------------------------------
# 查询执行：结果、截断与超时
# ---------------------------------------------------------------------------


def test_run_select_returns_rows_and_columns(db_path):
    connection = connect_readonly(db_path)
    try:
        result = run_select(connection, "SELECT code, amount FROM orders ORDER BY order_id", limit=3)
        assert result["columns"] == ["code", "amount"]
        assert result["row_count"] == 3
        assert result["truncated"] is True
        assert result["rows"][0] == ["east", 10.0]
    finally:
        connection.close()


def test_run_select_reports_not_truncated_when_it_fits(db_path):
    connection = connect_readonly(db_path)
    try:
        result = run_select(connection, "SELECT COUNT(*) AS n FROM orders", limit=10)
        assert result["truncated"] is False
        assert result["rows"] == [[6]]
    finally:
        connection.close()


def test_run_select_caps_columns(db_path):
    connection = connect_readonly(db_path)
    try:
        wide = ", ".join(f"1 AS c{index}" for index in range(MAX_RESULT_COLUMNS + 1))
        with pytest.raises(ValueError, match="列上限"):
            run_select(connection, f"SELECT {wide}", limit=1)
    finally:
        connection.close()


def test_run_select_interrupts_runaway_query(db_path):
    """失控查询必须在墙钟预算内被中断，而不是无限跑下去。

    SQLite 没有语句级超时，这里用 progress handler 兜底。构造一张自连接的大
    结果集，并把预算压到 1ms，验证返回的是"超预算已中断"而不是一个长任务。
    """
    connection = connect_readonly(db_path)
    try:
        runaway = (
            "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 200000) "
            "SELECT COUNT(*) FROM seq a, seq b"
        )
        with pytest.raises(ValueError, match="预算已被中断"):
            run_select(connection, runaway, limit=10, budget_ms=1)
    finally:
        connection.close()


def test_query_budget_default_is_bounded():
    """预算必须是有限正数：0 或负数会让每条查询都被立刻打断。"""
    assert QUERY_TIME_BUDGET_MS > 0


def test_to_json_safe_handles_blobs_and_scalars():
    assert to_json_safe(b"\x00\x01") == "<blob:2 bytes>"
    assert to_json_safe(None) is None
    assert to_json_safe(3) == 3
    assert to_json_safe("x") == "x"


# ---------------------------------------------------------------------------
# 载入默认表
# ---------------------------------------------------------------------------


def test_read_default_table_loads_largest_and_warns_which_one(db_path):
    frame, warnings = read_default_table(db_path)
    assert list(frame.columns) == ["order_id", "code", "amount"]
    assert len(frame) == 6
    # 必须说清载入的是哪张表，避免用户以为载入的是别的主表
    assert any("orders" in item for item in warnings)


def test_read_default_table_raises_for_invalid_file(tmp_path):
    fake = tmp_path / "fake.sqlite"
    fake.write_text("not a database", encoding="utf-8")
    with pytest.raises(ValueError, match="不是有效的 SQLite 数据库"):
        read_default_table(fake)


# ---------------------------------------------------------------------------
# 会话内定位数据源
# ---------------------------------------------------------------------------


def test_find_source_detects_sqlite_in_input_dir(tmp_path, db_path):
    import shutil

    shutil.copy(db_path, tmp_path / "copied.db")
    assert find_source(tmp_path) == (tmp_path / "copied.db").resolve()
    assert find_source(tmp_path / "missing") is None


def test_resolve_session_source_prefers_the_session_source(tmp_path, db_path):
    import shutil

    shutil.copy(db_path, tmp_path / "a.db")
    shutil.copy(db_path, tmp_path / "b.db")
    # 原始数据文件就是库时优先用它，语义最明确
    assert resolve_session_source(tmp_path / "b.db", tmp_path).name == "b.db"
    # 原始文件不是库时退回按名查找
    csv_like = tmp_path / "data.csv"
    csv_like.write_text("a\n1\n", encoding="utf-8")
    assert resolve_session_source(csv_like, tmp_path).name == "a.db"


# ---------------------------------------------------------------------------
# 工作区：上传即可用的 SQLite 数据源
# ---------------------------------------------------------------------------


def test_workspace_loads_sqlite_default_table_and_records_warning(tmp_path):
    workspace = _db_workspace(tmp_path)
    assert list(workspace.dataframe.columns) == ["order_id", "code", "amount"]
    assert workspace.source_row_count == 6
    assert any("orders" in item for item in workspace.load_warnings)


def test_upload_accepts_sqlite_extension(tmp_path):
    workspace = DataWorkspace(tmp_path / "runs", session_id="acc")
    saved = workspace.save_upload("x.db", b"SQLite format 3\x00")
    assert saved.suffix == ".db"


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------


def test_query_database_lists_schema_when_sql_is_empty(tmp_path):
    workspace = _db_workspace(tmp_path, "db_list")
    payload = json.loads(_tools(workspace)["query_database"].invoke({"sql": ""}))
    assert payload["table_count"] == 2
    tables = {item["table"]: item for item in payload["tables"]}
    assert tables["orders"]["row_count"] == 6
    assert tables["orders"]["columns"] == ["order_id:INTEGER", "code:TEXT", "amount:REAL"]


def test_query_database_runs_select(tmp_path):
    workspace = _db_workspace(tmp_path, "db_select")
    payload = json.loads(
        _tools(workspace)["query_database"].invoke(
            {"sql": "SELECT code, SUM(amount) AS total FROM orders GROUP BY code ORDER BY code"}
        )
    )
    assert payload["columns"] == ["code", "total"]
    assert payload["row_count"] == 2


def test_query_database_joins_across_tables(tmp_path):
    """跨表 JOIN 是这个数据源相对单表 CSV 的核心价值。

    orders 里 east 三条共 90、west 三条共 120，按总额降序时 west 的李四排第一。
    """
    workspace = _db_workspace(tmp_path, "db_join")
    payload = json.loads(
        _tools(workspace)["query_database"].invoke(
            {
                "sql": (
                    "SELECT r.manager, SUM(o.amount) AS total "
                    "FROM orders o JOIN region r ON o.code = r.code "
                    "GROUP BY r.manager ORDER BY total DESC"
                )
            }
        )
    )
    assert payload["row_count"] == 2
    assert payload["rows"][0] == ["李四", 120.0]
    assert payload["rows"][1] == ["张三", 90.0]


def test_query_database_rejects_writing_statement(tmp_path):
    workspace = _db_workspace(tmp_path, "db_write")
    with pytest.raises(ValueError, match="只允许只读查询"):
        _tools(workspace)["query_database"].invoke({"sql": "DELETE FROM orders"})


def test_query_database_adopts_result_as_dataset(tmp_path):
    workspace = _db_workspace(tmp_path, "db_adopt")
    before_artifacts = len(workspace.artifacts)
    payload = json.loads(
        _tools(workspace)["query_database"].invoke(
            {
                "sql": "SELECT code, SUM(amount) AS total FROM orders GROUP BY code",
                "adopt": True,
            }
        )
    )
    assert payload["adopted"] is True
    assert payload["rows_in_dataset"] == 2
    assert payload["output"].endswith("db_query_result.csv")
    assert list(workspace.dataframe.columns) == ["code", "total"]
    # 基线随新数据集重置，后续清洗才有正确参照
    assert workspace.source_row_count == 2
    assert len(workspace.artifacts) == before_artifacts + 1


def test_query_database_reports_missing_source_on_non_sqlite_session(workspace):
    """CSV 会话里调用本工具要给出可纠正的说明，而不是底层异常。"""
    with pytest.raises(ValueError, match="没有 SQLite 数据源"):
        _tools(workspace)["query_database"].invoke({"sql": "SELECT 1"})


def test_query_database_result_can_feed_chart_tool(tmp_path):
    """接管后的数据集必须能被后续工具正常消费（链路闭环）。

    这里刻意取行级结果而不是 GROUP BY 结果：图表工具会拒绝把"命名像 ID 且近乎
    逐行唯一"的列当类别轴，而聚合后的分组列恰好每行一个取值——那是既有护栏在
    正常工作，不该由本数据源去绕过。
    """
    workspace = _db_workspace(tmp_path, "db_chain")
    tools = _tools(workspace)
    tools["query_database"].invoke(
        {
            "sql": "SELECT r.manager, o.amount FROM orders o JOIN region r ON o.code = r.code",
            "adopt": True,
        }
    )
    chart = json.loads(
        tools["create_visualization"].invoke({"chart_type": "bar", "x": "manager", "y": "amount"})
    )
    assert chart["status"] == "ok"
    assert chart["html"].endswith(".html")


def test_query_database_does_not_corrupt_source_file(tmp_path):
    """查询全程不得改动数据库文件本身。"""
    workspace = _db_workspace(tmp_path, "db_ro")
    source = workspace.input_dir / "db_ro.db"
    before = source.read_bytes()
    _tools(workspace)["query_database"].invoke({"sql": "SELECT * FROM orders"})
    _tools(workspace)["query_database"].invoke(
        {"sql": "SELECT code, SUM(amount) AS total FROM orders GROUP BY code", "adopt": True}
    )
    assert source.read_bytes() == before


def test_dataframe_from_query_is_plain_values(tmp_path):
    """查询结果列名与取值要能直接进 pandas，不需要额外清洗。"""
    workspace = _db_workspace(tmp_path, "db_frame")
    _tools(workspace)["query_database"].invoke(
        {"sql": "SELECT order_id, code FROM orders ORDER BY order_id LIMIT 2", "adopt": True}
    )
    frame = workspace.dataframe
    assert isinstance(frame, pd.DataFrame)
    assert frame["order_id"].tolist() == [1, 2]
