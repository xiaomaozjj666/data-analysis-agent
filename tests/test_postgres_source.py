"""PostgreSQL 只读数据源测试。

设计原则：**连不上就跳过，而不是失败**。本地没有 Docker/Postgres 的环境里这些用例
自动跳过；CI 配了 service container 后跑真实集成测试——重点验证"只读在服务端真的
生效"（写入必须被数据库拒绝）与"失控查询会被服务端取消"。

并行注意：xdist 会多 worker 同时连库，每个用例都用唯一表名并在结束时清理。
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from data_agent import postgres_source
from data_agent.postgres_source import (
    DATABASE_URL_ENV,
    describe_table,
    resolve_dsn,
    run_select,
    schema_summary,
)
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _pg_available() -> bool:
    """连接一次判断可用性；驱动未装或连接串未配/连不上都视为不可用。"""
    if not postgres_source.is_configured():
        return False
    try:
        connection = postgres_source.connect_readonly()
    except Exception:  # noqa: BLE001 —— 任何连接失败都等价于"不可用"
        return False
    connection.close()
    return True


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL 不可用（未设置 DATA_AGENT_DATABASE_URL、未装 psycopg 或服务未启动）",
)


def _table() -> str:
    return f"da_test_{uuid4().hex[:8]}"


@pytest.fixture()
def pg():
    connection = postgres_source.connect_readonly()
    try:
        yield connection
    finally:
        connection.close()


def _make_table(connection, table: str, rows: int = 4) -> None:
    """在只读连接里建表——这要求先以可写连接准备，测试里用独立管理连接。"""
    import psycopg

    admin = psycopg.connect(os.environ[DATABASE_URL_ENV])
    try:
        admin.autocommit = True
        admin.execute(f"CREATE TABLE {table} (region TEXT, amount REAL)")
        admin.execute(
            f"INSERT INTO {table} SELECT 'east', g * 10.0 FROM generate_series(1, %s) g",
            (rows,),
        )
    finally:
        admin.close()


def _drop_table(table: str) -> None:
    import psycopg

    admin = psycopg.connect(os.environ[DATABASE_URL_ENV])
    try:
        admin.autocommit = True
        admin.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        admin.close()


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def test_resolve_dsn_requires_env(monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    with pytest.raises(ValueError, match=DATABASE_URL_ENV):
        resolve_dsn()


def test_is_configured_reflects_both_driver_and_dsn(monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    assert postgres_source.is_configured() is False
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://postgres:postgres@localhost:5432/analytics")
    # 驱动已安装（dev 依赖包含 psycopg[binary]）时才为 True
    assert postgres_source.is_configured() == (postgres_source.psycopg is not None)


def test_missing_driver_reports_install_hint(monkeypatch):
    """没装 psycopg 时要给出可操作的安装提示，而不是裸 ImportError。"""
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://postgres:postgres@localhost:5432/analytics")
    monkeypatch.setattr(postgres_source, "psycopg", None)
    with pytest.raises(ValueError, match=r"psycopg"):
        resolve_dsn()


# ---------------------------------------------------------------------------
# 只读边界：必须在服务端真实生效
# ---------------------------------------------------------------------------


@requires_pg
def test_write_is_rejected_by_the_server(pg):
    """只读由 default_transaction_read_only 保证，写入必须被数据库拒绝。"""
    table = _table()
    with pytest.raises(ValueError, match="只读事务"):
        run_select(pg, f"INSERT INTO {table} VALUES ('x', 1)", limit=10)


@requires_pg
def test_pragma_and_attach_are_rejected_by_the_statement_guard(pg):
    with pytest.raises(ValueError, match="只允许只读查询"):
        run_select(pg, "PRAGMA table_info(pg_tables)", limit=10)
    with pytest.raises(ValueError, match="只允许只读查询"):
        run_select(pg, "ATTACH DATABASE 'x' AS y", limit=10)


@requires_pg
def test_schema_summary_lists_created_tables(pg):
    table = _table()
    try:
        _make_table(pg, table)
        summary = schema_summary(pg)
        names = {item["table"] for item in summary["tables"]}
        assert table in names
    finally:
        _drop_table(table)


@requires_pg
def test_describe_table_rejects_unknown_name(pg):
    with pytest.raises(ValueError, match="不存在"):
        describe_table(pg, "no_such_table_xyz")


@requires_pg
def test_run_select_returns_rows(pg):
    table = _table()
    try:
        _make_table(pg, table, rows=4)
        result = run_select(pg, f"SELECT region, SUM(amount) AS total FROM {table} GROUP BY region", limit=10)
        assert result["columns"] == ["region", "total"]
        assert result["row_count"] == 1
        assert result["rows"][0][0] == "east"
    finally:
        _drop_table(table)


@requires_pg
def test_run_select_interrupts_runaway_query(pg):
    """statement_timeout 必须真的取消失控查询，而不是让分析线程占死。"""
    table = _table()
    try:
        _make_table(pg, table)
        runaway = (
            f"SELECT COUNT(*) FROM {table} a, generate_series(1, 10000000) g1, "
            "generate_series(1, 100) g2"
        )
        with pytest.raises(ValueError, match="预算已被数据库取消"):
            run_select(pg, runaway, limit=10, budget_ms=1)
    finally:
        _drop_table(table)


@requires_pg
def test_query_is_reproducible_across_calls(pg):
    table = _table()
    try:
        _make_table(pg, table, rows=3)
        first = run_select(pg, f"SELECT COUNT(*) AS n FROM {table}", limit=10)
        second = run_select(pg, f"SELECT COUNT(*) AS n FROM {table}", limit=10)
        assert first["rows"] == second["rows"] == [[3]]
    finally:
        _drop_table(table)


# ---------------------------------------------------------------------------
# 工具层：source 参数分发
# ---------------------------------------------------------------------------


@requires_pg
def test_query_database_tool_lists_schema_for_postgres(monkeypatch, tmp_path):
    monkeypatch.setenv(DATABASE_URL_ENV, os.environ[DATABASE_URL_ENV])
    workspace = DataWorkspace(tmp_path / "runs", session_id="pg_tool")
    tools = {tool.name: tool for tool in build_tools(workspace)}
    payload = json.loads(tools["query_database"].invoke({"sql": "", "source": "postgres"}))
    assert payload["source_kind"] == "postgres"
    assert payload["table_count"] >= 0
    assert isinstance(payload["tables"], list)


@requires_pg
def test_query_database_tool_adopts_postgres_result(monkeypatch, tmp_path):
    monkeypatch.setenv(DATABASE_URL_ENV, os.environ[DATABASE_URL_ENV])
    workspace = DataWorkspace(tmp_path / "runs", session_id="pg_adopt")
    tools = {tool.name: tool for tool in build_tools(workspace)}
    table = _table()
    try:
        _make_table(postgres_source.connect_readonly(), table)
        payload = json.loads(
            tools["query_database"].invoke(
                {
                    "sql": f"SELECT region, amount FROM {table} ORDER BY amount LIMIT 2",
                    "source": "postgres",
                    "adopt": True,
                }
            )
        )
        assert payload["adopted"] is True
        assert list(workspace.dataframe.columns) == ["region", "amount"]
        assert workspace.source_row_count == 2
    finally:
        _drop_table(table)


@requires_pg
def test_query_database_reports_available_sources_on_discovery(monkeypatch, tmp_path):
    """发现模式要告诉模型有哪些数据源可选，否则它无从得知 postgres 可用。"""
    monkeypatch.setenv(DATABASE_URL_ENV, os.environ[DATABASE_URL_ENV])
    workspace = DataWorkspace(tmp_path / "runs", session_id="pg_src")
    tools = {tool.name: tool for tool in build_tools(workspace)}
    payload = json.loads(tools["query_database"].invoke({"sql": "", "source": "postgres"}))
    assert payload["source_kind"] == "postgres"
    assert payload["status"] == "ok"
