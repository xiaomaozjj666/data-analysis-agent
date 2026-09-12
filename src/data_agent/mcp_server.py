"""MCP 数据面服务：把受控数据分析能力开放给其他 Agent。

只暴露**数据面**：会话发现、数据集载入、结构探查、只读 SQL。不暴露
``run_python_code`` 这类长尾计算，也不暴露需要 LLM 与分钟级时长的完整分析
流程——后者已由 HTTP API 承载，塞进 MCP 工具调用会多出一个有状态的
长任务入口，两条状态机没有收益。

开放面刻意做粗：MCP 客户端拿到的是"安全的数据访问"，而不是内部工具原样
透出。护栏与工作区内工具**完全同源**——语句白名单、只读连接、行列上限、
上传大小与数据规模限制都复用同一份实现，这里不重新发明。

传输用 stdio：进程由受信任的本地客户端拉起，天然没有网络暴露面。**不要**
把这个服务挂到网络上对外提供服务；如需远程访问，走项目自身的 HTTP API
（带 APP_ACCESS_TOKEN 与限流）。

依赖：``pip install ".[mcp]"``。未安装 SDK 时 ``create_server`` 给出可操作的
中文提示。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from data_agent import postgres_source, sqlite_source
from data_agent.config import AgentSettings
from data_agent.registry import SessionRegistry
from data_agent.serialization import json_text, to_jsonable
from data_agent.workspace import SUPPORTED_EXTENSIONS, DataWorkspace

try:  # pragma: no cover - 视安装环境而定
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - 可选依赖
    FastMCP = None  # type: ignore[assignment]

_INSTALL_HINT = '未安装 MCP SDK。请先安装：pip install ".[mcp]"。'

#: 支持的会话来源。
_VALID_SOURCES: tuple[str, ...] = ("session", "postgres")


def _registry(settings: AgentSettings) -> SessionRegistry:
    """构建独立的会话注册表。

    MCP 服务是独立进程，不与 FastAPI 实例共享内存状态；会话以
    ``runs/<id>/session.json`` 落盘自描述，因此按同一 runs_dir 实例化注册表
    即可读到对方创建的会话。存储后端用本地实现：对象存储同步由 HTTP 服务
    负责，这里只做数据面读取与本地载入。
    """
    return SessionRegistry(
        settings.runs_dir,
        settings.max_active_sessions,
        settings.session_ttl_hours,
    )


def create_server(settings: AgentSettings | None = None) -> Any:
    """构建 MCP 服务实例（stdio 传输）。

    Args:
        settings: 运行配置；缺省时从环境变量读取（不需要 LLM API Key，
            数据面用不到模型）。

    Returns:
        FastMCP 服务实例；调用 ``.run()`` 即以 stdio 启动。

    Raises:
        ValueError: 未安装 MCP SDK。
    """
    if FastMCP is None:
        raise ValueError(_INSTALL_HINT)
    resolved = settings or AgentSettings.from_env()
    registry = _registry(resolved)

    def _session(session_id: str) -> Any:
        """按 id 取会话；把注册表的 HTTPException 转成面向 MCP 客户端的错误。"""
        try:
            return registry.get(session_id)
        except HTTPException as exc:  # 404：会话不存在或已被 TTL 清理
            raise ValueError(
                f"会话「{session_id}」不存在或已过期。可先用 list_sessions 查看可用会话。"
            ) from exc

    mcp = FastMCP(
        "data-analysis-agent",
        instructions=(
            "受控数据分析工作台的数据面。先 list_sessions 或 open_dataset 取得会话，"
            "再用 inspect_data 看结构与预览，需要跨表/过滤/聚合时用 sql_query 写 "
            "SELECT。所有访问都是只读且带护栏的：只接受 SELECT/WITH，写入在驱动或"
            "服务端被拒绝；完整分析流程（LLM 规划-执行-报告）走本项目的 HTTP API。"
        ),
    )

    @mcp.tool()
    def list_sessions(limit: int = 20) -> str:
        """List recent analysis sessions with id, filename, title and status.

        Call this first when you do not know which sessions exist. The id returned
        here is what the other tools take as session_id. Row counts are not part
        of this listing; use inspect_data for a session's shape.
        """
        capped = max(1, min(int(limit), 100))
        sessions = registry.list_recent(limit=capped)
        return json_text({"sessions": sessions, "count": len(sessions)})

    @mcp.tool()
    def open_dataset(path: str) -> str:
        """Load a local data file into a new session and return its profile.

        Supported: CSV/TSV, Excel, JSON/JSONL, Parquet, SQLite (.db/.sqlite,
        the largest table becomes the active dataset), PDF/TXT/DOCX.
        The same size and shape limits as the HTTP upload apply.
        """
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"文件不存在：{source}")
        if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"不支持的格式「{source.suffix}」。支持：{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
        size = source.stat().st_size
        if size == 0:
            raise ValueError("文件为空。")
        if size > resolved.max_upload_bytes:
            raise ValueError(
                f"文件过大（{size / 1024 / 1024:.1f}MB，上限 "
                f"{resolved.max_upload_bytes / 1024 / 1024:.0f}MB）。"
            )
        workspace = DataWorkspace(resolved.runs_dir, session_id=f"mcp_{uuid.uuid4().hex[:12]}")
        try:
            workspace.load(source, copy_into_workspace=True)
            rows, columns = len(workspace.dataframe), len(workspace.dataframe.columns)
            if rows > resolved.max_rows or rows * columns > resolved.max_cells:
                raise ValueError(
                    f"数据规模超过限制：最多 {resolved.max_rows:,} 行或 "
                    f"{resolved.max_cells:,} 个单元格。"
                )
        except Exception:
            workspace.cleanup()
            raise
        session_id, _record = registry.create(workspace)
        return json_text(
            {
                "session_id": session_id,
                "filename": source.name,
                "rows": rows,
                "columns": columns,
                "column_names": [str(name) for name in workspace.dataframe.columns],
                "load_warnings": list(workspace.load_warnings),
                "next": "用 inspect_data 看预览与统计，或直接 sql_query 查询。",
            }
        )

    @mcp.tool()
    def inspect_data(session_id: str) -> str:
        """Inspect a session's active dataset: profile, preview and load warnings.

        Read-only. Use it to learn the columns before writing SQL.
        """
        record = _session(session_id)
        workspace = record.workspace
        return json_text(
            {
                "session_id": session_id,
                "filename": workspace.source_path.name if workspace.source_path else "dataset",
                "rows": len(workspace.dataframe),
                "columns": len(workspace.dataframe.columns),
                "profile": to_jsonable(workspace.profile(sample_rows=8)),
                "preview": to_jsonable(workspace.dataframe.head(20)),
                "load_warnings": list(workspace.load_warnings),
            }
        )

    @mcp.tool()
    def sql_query(
        session_id: str = "",
        sql: str = "",
        source: str = "session",
        limit: int = 200,
    ) -> str:
        """Run a read-only SQL query against a relational data source.

        source selects the target: "session" is the uploaded .db/.sqlite file of
        a session (session_id required); "postgres" is the warehouse configured
        through the DATA_AGENT_DATABASE_URL environment variable (session_id not
        needed).

        Call with an empty sql to get every table's schema and the list of
        available sources. Only SELECT / WITH statements are accepted and the
        connection is read-only, so nothing can be modified. Results are capped
        at 5000 rows; this tool never mutates any session's active dataset.
        """
        if source not in _VALID_SOURCES:
            raise ValueError(
                f"未知的 source「{source}」。可用值：{'、'.join(_VALID_SOURCES)}"
                "（session=会话内 SQLite 文件，postgres=环境变量配置的连接）。"
            )
        if source == "postgres":
            driver = postgres_source
            connection = driver.connect_readonly()
            source_label = "postgres（环境变量配置的连接）"
        else:
            if not session_id.strip():
                raise ValueError(
                    'source="session" 需要提供 session_id；可先用 list_sessions 或 open_dataset 取得。'
                )
            record = _session(session_id)
            workspace = record.workspace
            database_path = sqlite_source.resolve_session_source(workspace.source_path, workspace.input_dir)
            if database_path is None:
                available = ["postgres"] if postgres_source.is_configured() else []
                raise ValueError(
                    "该会话没有 SQLite 数据源（source=\"session\" 需要 .db/.sqlite 文件）。"
                    + (
                        "环境变量已配置 PostgreSQL，请改用 source=\"postgres\"。"
                        if available
                        else "也可改用 inspect_data 查看当前数据集。"
                    )
                )
            driver = sqlite_source
            connection = driver.connect_readonly(database_path)
            source_label = database_path.name
        try:
            if not (sql or "").strip():
                summary = driver.schema_summary(connection)
                available_sources = ["session"]
                if postgres_source.is_configured():
                    available_sources.append("postgres")
                return json_text(
                    {
                        "source": source_label,
                        "source_kind": source,
                        "available_sources": available_sources,
                        "table_count": summary["table_count"],
                        "tables": [
                            {
                                "table": item["table"],
                                "row_count": item["row_count"],
                                "columns": [
                                    f"{column['name']}:{column['type'] or 'TEXT'}"
                                    for column in item["columns"]
                                ],
                            }
                            for item in summary["tables"]
                        ],
                    }
                )
            result = driver.run_select(connection, sql, limit=limit)
            payload: dict[str, Any] = {
                "source": source_label,
                "source_kind": source,
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result["truncated"],
            }
            if result["truncated"]:
                payload["note"] = (
                    f"结果超过 {limit} 行已截断，请先用聚合或过滤收敛结果。"
                )
            return json_text(payload)
        finally:
            connection.close()

    return mcp


def main() -> None:
    """以 stdio 传输启动 MCP 服务（供 ``data-agent-mcp`` 命令使用）。"""
    create_server().run()
