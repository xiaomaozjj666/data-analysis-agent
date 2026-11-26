"""通用 DB-API 外部源：sqlite3 离线链路、白名单与 Snowflake 会话设置。"""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from data_agent import data_sources, external_source


def _sqlite_db(tmp_path):
    path = tmp_path / "warehouse.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sales (region TEXT, amount REAL)")
    connection.executemany("INSERT INTO sales VALUES (?, ?)", [("East", 10.5), ("West", 20.0)])
    connection.commit()
    connection.close()
    return path


def test_external_sqlite_roundtrip(tmp_path, monkeypatch):
    path = _sqlite_db(tmp_path)
    monkeypatch.setenv(external_source.MODULE_ENV, "sqlite3")
    monkeypatch.setenv(external_source.KWARGS_ENV, json.dumps({"database": path.as_posix()}))
    assert external_source.is_configured() is True

    connection = external_source.connect_readonly()
    try:
        result = external_source.run_select(connection, "SELECT region, amount FROM sales", 10)
        assert result["columns"] == ["region", "amount"]
        assert result["row_count"] == 2
        with pytest.raises(ValueError, match="只允许"):
            external_source.run_select(connection, "DELETE FROM sales", 10)
        summary = external_source.schema_summary(connection)
        assert summary["table_count"] == 0
        assert "cursor.tables" in summary.get("note", "")
    finally:
        connection.close()


def test_external_config_errors(tmp_path, monkeypatch):
    monkeypatch.delenv(external_source.MODULE_ENV, raising=False)
    with pytest.raises(ValueError, match="未配置外部数据源"):
        external_source.connect_readonly()

    monkeypatch.setenv(external_source.MODULE_ENV, "os.system")
    with pytest.raises(ValueError, match="不允许的 DB-API 模块"):
        external_source.connect_readonly()

    monkeypatch.setenv(external_source.MODULE_ENV, "snowflake.connector")
    monkeypatch.setenv(external_source.KWARGS_ENV, "[1, 2]")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        external_source.connect_readonly()
    monkeypatch.setenv(external_source.KWARGS_ENV, "{broken")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        external_source.connect_readonly()

    monkeypatch.setenv(external_source.KWARGS_ENV, "{}")
    with pytest.raises(ValueError, match="未安装驱动"):
        external_source.connect_readonly()


def test_snowflake_connect_sets_statement_timeout(monkeypatch):
    executed: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            executed.append(sql)

    connection = SimpleNamespace(cursor=lambda: _Cursor())
    module = SimpleNamespace(connect=lambda **kwargs: connection)
    monkeypatch.setattr(external_source.importlib, "import_module", lambda name: module)
    monkeypatch.setenv(external_source.MODULE_ENV, "snowflake.connector")
    monkeypatch.setenv(external_source.KWARGS_ENV, '{"account": "demo"}')
    monkeypatch.setenv(external_source.TIMEOUT_ENV, "45")

    assert external_source.connect_readonly() is connection
    assert executed == ["ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 45"]


def test_psycopg_connect_sets_statement_timeout(monkeypatch):
    executed: list[str] = []
    commits: list[bool] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            executed.append(sql)

    connection = SimpleNamespace(
        cursor=lambda: _Cursor(), commit=lambda: commits.append(True)
    )
    module = SimpleNamespace(connect=lambda **kwargs: connection)
    monkeypatch.setattr(external_source.importlib, "import_module", lambda name: module)
    monkeypatch.setenv(external_source.MODULE_ENV, "psycopg")
    monkeypatch.setenv(external_source.KWARGS_ENV, '{"dbname": "analytics"}')
    monkeypatch.setenv(external_source.TIMEOUT_ENV, "30")

    assert external_source.connect_readonly() is connection
    assert executed == ["SET statement_timeout = 30000"]
    assert commits == [True]


def test_psycopg_timeout_failure_is_tolerated(monkeypatch):
    """部分 DB-API 包装不支持会话级 SET，不能因此拒绝连接。"""

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            raise RuntimeError("unsupported")

    connection = SimpleNamespace(cursor=lambda: _Cursor(), commit=lambda: None)
    module = SimpleNamespace(connect=lambda **kwargs: connection)
    monkeypatch.setattr(external_source.importlib, "import_module", lambda name: module)
    monkeypatch.setenv(external_source.MODULE_ENV, "psycopg")
    monkeypatch.setenv(external_source.KWARGS_ENV, "{}")

    assert external_source.connect_readonly() is connection


def test_connect_readonly_reraises_and_wraps_driver_errors(monkeypatch):
    def broken_connect(**kwargs):
        raise TypeError("bad kwargs")

    module = SimpleNamespace(connect=broken_connect)
    monkeypatch.setattr(external_source.importlib, "import_module", lambda name: module)
    monkeypatch.setenv(external_source.MODULE_ENV, "sqlite3")
    monkeypatch.setenv(external_source.KWARGS_ENV, "{}")
    with pytest.raises(TypeError, match="bad kwargs"):
        external_source.connect_readonly()

    def auth_failed(**kwargs):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(
        external_source.importlib, "import_module", lambda name: SimpleNamespace(connect=auth_failed)
    )
    with pytest.raises(ValueError, match="外部数据源连接失败（RuntimeError）"):
        external_source.connect_readonly()


def test_row_value_prefers_attributes_then_index() -> None:
    assert external_source._row_value(SimpleNamespace(name="sales"), "name") == "sales"
    assert external_source._row_value(("orders",)) == "orders"
    # 既没有匹配属性也不支持下标：降级为 None
    assert external_source._row_value(SimpleNamespace(other=1)) is None


def test_run_select_tolerates_progress_handler_failure(monkeypatch):
    class _UnsupportedHandler:
        """模拟不支持会话级进度回调的 DB-API 包装。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def set_progress_handler(self, *_args, **_kwargs):
            raise RuntimeError("not supported")

    inner = sqlite3.connect(":memory:", check_same_thread=False)
    connection = _UnsupportedHandler(inner)
    try:
        inner.execute("CREATE TABLE t (value INTEGER)")
        inner.execute("INSERT INTO t VALUES (7)")
        inner.commit()
        result = external_source.run_select(connection, "SELECT value FROM t", 10)
        assert result["rows"] == [[7]]
    finally:
        inner.close()


def test_data_sources_exposes_external(tmp_path, monkeypatch):
    path = _sqlite_db(tmp_path)
    monkeypatch.setenv(external_source.MODULE_ENV, "sqlite3")
    monkeypatch.setenv(external_source.KWARGS_ENV, json.dumps({"database": path.as_posix()}))
    descriptor = data_sources.resolve("external")
    assert descriptor.configured is True
    assert descriptor.readonly_guarantee == data_sources.READONLY_ROLE_REQUIRED

    driver, connection, label = data_sources.open_external("external")
    try:
        assert driver is external_source
        assert "DB-API" in label
        result = driver.run_select(connection, "SELECT count(*) AS n FROM sales", 5)
        assert result["rows"][0][0] == 2
    finally:
        connection.close()


def test_query_database_tool_uses_external_source(workspace, tmp_path, monkeypatch):
    from data_agent.tools.builder import build_tools

    path = _sqlite_db(tmp_path)
    monkeypatch.setenv(external_source.MODULE_ENV, "sqlite3")
    monkeypatch.setenv(external_source.KWARGS_ENV, json.dumps({"database": path.as_posix()}))
    tool = {item.name: item for item in build_tools(workspace)}["query_database"]
    payload = tool.invoke(
        {"sql": "SELECT region, amount FROM sales ORDER BY amount DESC", "source": "external"}
    )
    assert '"source_kind": "external"' in payload
    assert "West" in payload


def test_external_timeout_and_kwargs_defaults(monkeypatch):
    monkeypatch.delenv(external_source.KWARGS_ENV, raising=False)
    assert external_source._kwargs() == {}
    monkeypatch.setenv(external_source.TIMEOUT_ENV, "abc")
    assert 1 <= external_source._timeout_seconds() <= 120
    monkeypatch.setenv(external_source.TIMEOUT_ENV, "999")
    assert external_source._timeout_seconds() == 120
    monkeypatch.setenv(external_source.TIMEOUT_ENV, "-5")
    assert external_source._timeout_seconds() == 1


def test_schema_summary_introspects_and_degrades():
    class _Cursor:
        def tables(self):
            return [
                SimpleNamespace(table_name="sales"),
                (None, None, "orders"),
                (None, None, None),
            ]

        def columns(self, table):
            if table == "orders":
                raise RuntimeError("columns unsupported")
            return [
                SimpleNamespace(column_name="region", type_name="TEXT"),
                (None, None, None, "amount", None, "REAL"),
            ]

        def close(self):
            raise RuntimeError("close failed")

    summary = external_source.schema_summary(SimpleNamespace(cursor=lambda: _Cursor()))
    assert summary["table_count"] == 2
    assert summary["tables"][0]["columns"] == [
        {"name": "region", "type": "TEXT"},
        {"name": "amount", "type": "REAL"},
    ]
    assert summary["tables"][1]["columns"] == []


def test_schema_summary_cursor_failures():
    class _NoCursor:
        def cursor(self):
            raise RuntimeError("no cursor")

    assert "无法创建游标" in external_source.schema_summary(_NoCursor())["note"]

    class _BrokenTables:
        def tables(self):
            raise RuntimeError("tables failed")

        def close(self):
            pass

    assert (
        "内省失败"
        in external_source.schema_summary(SimpleNamespace(cursor=lambda: _BrokenTables()))["note"]
    )

    class _CloseRaises:
        def tables(self):
            return []

        def close(self):
            raise RuntimeError("close failed")

    assert (
        external_source.schema_summary(SimpleNamespace(cursor=lambda: _CloseRaises()))[
            "table_count"
        ]
        == 0
    )


def test_run_select_closes_cursor_even_if_close_fails():
    class _Cursor:
        description = [("n",)]

        def execute(self, sql):
            self.sql = sql

        def fetchmany(self, size):
            return [(1,)]

        def fetchone(self):
            return None

        def close(self):
            raise RuntimeError("close failed")

    class _Connection:
        def cursor(self):
            return _Cursor()

    result = external_source.run_select(_Connection(), "SELECT 1 AS n", 5)
    assert result["rows"] == [[1]]


def test_external_sqlite_runaway_query_is_interrupted(tmp_path, monkeypatch):
    path = tmp_path / "big.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE big (x INTEGER)")
    connection.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(20000)])
    connection.commit()
    connection.close()
    monkeypatch.setenv(external_source.MODULE_ENV, "sqlite3")
    monkeypatch.setenv(external_source.KWARGS_ENV, json.dumps({"database": path.as_posix()}))
    monkeypatch.setenv(external_source.TIMEOUT_ENV, "1")
    connection = external_source.connect_readonly()
    started = time.perf_counter()
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - sqlite 中止异常类型跨版本
        external_source.run_select(
            connection,
            "SELECT count(*) FROM big a, big b",
            10,
        )
    elapsed = time.perf_counter() - started
    connection.close()
    assert elapsed < 8.0
    assert "interrupted" in str(excinfo.value).lower() or "预算" in str(excinfo.value)


def test_external_thread_cap_bounds_non_interruptible_drivers():
    import threading

    class _SlowCursor:
        def execute(self, sql):
            time.sleep(5)

        def close(self):
            pass

    class _SlowConnection:
        def cursor(self):
            return _SlowCursor()

    started = time.perf_counter()
    with pytest.raises(ValueError, match="墙钟预算"):
        external_source.run_select(_SlowConnection(), "SELECT 1", 10, timeout_seconds=1)
    assert time.perf_counter() - started < 4.0
    # 守护线程被放弃：不能阻塞进程退出
    assert threading.active_count() >= 1


def test_external_driver_connect_error_is_actionable(monkeypatch):
    """驱动鉴权/网络异常必须转成 ValueError（MCP 才能返回 ToolError 而非 500）。"""
    module = SimpleNamespace(
        connect=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("auth failed"))
    )
    monkeypatch.setattr(external_source.importlib, "import_module", lambda name: module)
    monkeypatch.setenv(external_source.MODULE_ENV, "snowflake.connector")
    monkeypatch.setenv(external_source.KWARGS_ENV, '{"account": "demo"}')
    with pytest.raises(ValueError, match="连接失败.*auth failed"):
        external_source.connect_readonly()
