"""MCP 数据面服务测试：用官方 SDK 的内存客户端做真实协议互操作。

不走 mock：客户端经 MCP 协议（initialize / tools/list / tools/call）与服务器
交互，验证的是外部 Agent 实际看到的行为。工具报错必须以 ``isError=True``
返回并带中文原因，而不是把异常炸断协议流——这是 MCP 客户端的契约。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import anyio
import pytest

from data_agent import postgres_source
from data_agent.config import AgentSettings
from data_agent.mcp_server import create_server

EXPECTED_TOOLS = {"list_sessions", "open_dataset", "inspect_data", "sql_query"}


def _server(tmp_path: Path):
    settings = AgentSettings(
        api_key="not-used",
        runs_dir=tmp_path / "runs",
        max_upload_bytes=10 * 1024 * 1024,
        max_rows=1000,
        max_cells=100_000,
        max_active_sessions=10,
        session_ttl_hours=24.0,
    )
    return create_server(settings)


def _payload(result) -> dict:
    """把成功调用结果解析成 dict；isError 时抛出带原因的断言错误。"""
    text = result.content[0].text
    if result.isError:
        raise AssertionError(f"工具调用意外失败：{text}")
    return json.loads(text)


def _error_text(result) -> str:
    assert result.isError is True, "预期工具报错（isError=True）"
    return result.content[0].text


def _run(scenario) -> None:
    anyio.run(scenario)


def _make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE region (code TEXT, manager TEXT)")
        connection.executemany("INSERT INTO region VALUES (?, ?)", [("east", "张三"), ("west", "李四")])
        connection.execute("CREATE TABLE orders (order_id INTEGER, code TEXT, amount REAL)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(index, "east", float(index) * 10) for index in range(1, 5)],
        )
        connection.commit()
    finally:
        connection.close()


def _pg_available() -> bool:
    if not postgres_source.is_configured():
        return False
    try:
        connection = postgres_source.connect_readonly()
    except Exception:  # noqa: BLE001 —— 连不上即视为不可用
        return False
    connection.close()
    return True


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL 不可用（未设置 DATA_AGENT_DATABASE_URL 或服务未启动）",
)


# ---------------------------------------------------------------------------
# 工具清单与会话生命周期
# ---------------------------------------------------------------------------


def test_lists_expected_tools(tmp_path: Path):
    observed: set[str] = set()

    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            result = await session.list_tools()
            observed.update(tool.name for tool in result.tools)

    _run(scenario)
    assert observed == EXPECTED_TOOLS


def test_open_dataset_then_inspect_roundtrip(tmp_path: Path):
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("region,sales\nEast,100\nWest,200\n", encoding="utf-8")

    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            opened = _payload(await session.call_tool("open_dataset", {"path": str(csv_path)}))
            assert opened["rows"] == 2
            assert opened["columns"] == 2
            assert opened["column_names"] == ["region", "sales"]
            inspected = _payload(
                await session.call_tool("inspect_data", {"session_id": opened["session_id"]})
            )
            assert inspected["filename"] == "sales.csv"
            assert inspected["rows"] == 2
            assert inspected["preview"][0]["region"] == "East"

    _run(scenario)


def test_open_dataset_rejects_unsupported_extension(tmp_path: Path):
    bad = tmp_path / "payload.exe"
    bad.write_bytes(b"whatever")

    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            result = await session.call_tool("open_dataset", {"path": str(bad)})
            assert "不支持的格式" in _error_text(result)

    _run(scenario)


def test_open_dataset_rejects_missing_file(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            result = await session.call_tool("open_dataset", {"path": str(tmp_path / "nope.csv")})
            assert "文件不存在" in _error_text(result)

    _run(scenario)


def test_tool_errors_surface_as_iserror_not_protocol_break(tmp_path: Path):
    """MCP 客户端的契约：报错以 isError 结果返回，协议流不中断。"""
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            broken = await session.call_tool("inspect_data", {"session_id": "no_such_session"})
            assert "不存在" in _error_text(broken)
            # 出错后协议仍然可用：后续调用正常
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

    _run(scenario)


def test_list_sessions_reports_created_sessions(tmp_path: Path):
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("region,sales\nEast,100\n", encoding="utf-8")

    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            opened = _payload(await session.call_tool("open_dataset", {"path": str(csv_path)}))
            listing = _payload(await session.call_tool("list_sessions", {}))
            ids = {item["id"] for item in listing["sessions"]}
            assert opened["session_id"] in ids

    _run(scenario)


# ---------------------------------------------------------------------------
# 只读 SQL
# ---------------------------------------------------------------------------


async def _open_sqlite_session(session, tmp_path: Path) -> dict:
    db_path = tmp_path / f"shop_{uuid4().hex[:6]}.db"
    _make_db(db_path)
    return _payload(await session.call_tool("open_dataset", {"path": str(db_path)}))


def test_sql_query_lists_schema_and_available_sources(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            opened = await _open_sqlite_session(session, tmp_path)
            payload = _payload(
                await session.call_tool("sql_query", {"session_id": opened["session_id"], "sql": ""})
            )
            assert payload["source_kind"] == "session"
            assert "session" in payload["available_sources"]
            tables = {item["table"] for item in payload["tables"]}
            assert {"orders", "region"} <= tables

    _run(scenario)


def test_sql_query_runs_select_and_join(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            opened = await _open_sqlite_session(session, tmp_path)
            payload = _payload(
                await session.call_tool(
                    "sql_query",
                    {
                        "session_id": opened["session_id"],
                        "sql": (
                            "SELECT r.manager, SUM(o.amount) AS total "
                            "FROM orders o JOIN region r ON o.code = r.code "
                            "GROUP BY r.manager ORDER BY total DESC"
                        ),
                    },
                )
            )
            # 本文件的 _make_db 里 4 笔订单全部挂在 east（10+20+30+40=100）
            assert payload["rows"] == [["张三", 100.0]]
            assert payload["truncated"] is False

    _run(scenario)


def test_sql_query_rejects_writing_statement(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            opened = await _open_sqlite_session(session, tmp_path)
            result = await session.call_tool(
                "sql_query",
                {"session_id": opened["session_id"], "sql": "DELETE FROM orders"},
            )
            assert "只允许只读查询" in _error_text(result)

    _run(scenario)


def test_sql_query_requires_session_id_for_session_source(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            result = await session.call_tool("sql_query", {"sql": "SELECT 1"})
            assert "session_id" in _error_text(result)

    _run(scenario)


def test_sql_query_rejects_unknown_source(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            result = await session.call_tool(
                "sql_query", {"session_id": "whatever", "sql": "SELECT 1", "source": "oracle"}
            )
            assert "未知的 source" in _error_text(result)

    _run(scenario)


def test_sql_query_never_mutates_the_session_dataset(tmp_path: Path):
    """数据面只读承诺：查询之后会话的活动数据集必须原封不动。"""
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            opened = await _open_sqlite_session(session, tmp_path)
            await session.call_tool(
                "sql_query",
                {"session_id": opened["session_id"], "sql": "SELECT * FROM orders"},
            )
            inspected = _payload(
                await session.call_tool("inspect_data", {"session_id": opened["session_id"]})
            )
            assert inspected["rows"] == opened["rows"]
            assert inspected["columns"] == opened["columns"]

    _run(scenario)


# ---------------------------------------------------------------------------
# PostgreSQL 数据源（连不上自动跳过）
# ---------------------------------------------------------------------------


@requires_pg
def test_sql_query_postgres_discovery_and_select(tmp_path: Path):
    async def scenario():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(_server(tmp_path)) as session:
            discovery = _payload(
                await session.call_tool("sql_query", {"source": "postgres", "sql": ""})
            )
            assert discovery["source_kind"] == "postgres"
            assert set(discovery["available_sources"]) >= {"session", "postgres"}
            selected = _payload(
                await session.call_tool(
                    "sql_query",
                    {"source": "postgres", "sql": "SELECT 1 AS one"},
                )
            )
            assert selected["rows"] == [[1]]

    _run(scenario)
