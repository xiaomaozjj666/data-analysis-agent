"""通用只读 DB-API 外部源：用配置而不是硬编码接入数仓驱动。

背景：Snowflake / Databricks / BigQuery 都有 DB-API 2.0 接口（BigQuery 走
``google.cloud.bigquery.dbapi``），但项目没有这些服务的真实凭据，无法验证
各家的鉴权、服务端只读与超时取消。按项目既有的「无可验证手段不冒充能力」
原则，这里不写三份"看起来支持"的驱动，而是提供一个**配置式 DB-API 适配器**：

- 管理员在环境变量里指定模块与连接参数，模块从白名单中选择；
- 生成只读连接（能设置只读开关的驱动会设置：SQLite ``PRAGMA query_only``、
  Snowflake statement timeout）；
- 所有查询仍先过 :func:`data_agent.sql_guard.validate_select`（单条 SELECT/WITH、
  危险函数、CTE 写入等）；
- 能力声明为 ``role-required``：服务端只读由账号/角色保证，项目不谎称已确认。

离线可验证路径：用 ``sqlite3`` 作为 DB-API 模块跑完整查询/内省链路；真实
数仓连接属于部署方责任，配置错误会得到可操作的中文报错。
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from typing import Any

from data_agent.sql_guard import (
    collect_rows,
    query_time_budget_ms,
    validate_select,
)

MODULE_ENV = "DATA_AGENT_EXTERNAL_DB_MODULE"
KWARGS_ENV = "DATA_AGENT_EXTERNAL_DB_KWARGS"
LABEL_ENV = "DATA_AGENT_EXTERNAL_DB_LABEL"
TIMEOUT_ENV = "DATA_AGENT_EXTERNAL_DB_TIMEOUT_SECONDS"

#: 白名单：避免把任意模块名当驱动导入（配置被误填时也不会执行奇怪代码）。
_ALLOWED_MODULES = {
    "sqlite3": "sqlite3",
    "snowflake.connector": "snowflake-connector-python",
    "databricks.sql": "databricks-sql-connector",
    "google.cloud.bigquery.dbapi": "google-cloud-bigquery",
    "psycopg": "psycopg",
}


def module_name() -> str:
    return (os.environ.get(MODULE_ENV) or "").strip()


def is_configured() -> bool:
    return bool(module_name())


def _kwargs() -> dict[str, Any]:
    raw = (os.environ.get(KWARGS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{KWARGS_ENV} 必须是 JSON 对象：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{KWARGS_ENV} 必须是 JSON 对象。")
    return payload


def _timeout_seconds() -> int:
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    try:
        value = int(raw) if raw else query_time_budget_ms() // 1000
    except ValueError:
        value = query_time_budget_ms() // 1000
    return max(1, min(value, 120))


def _connect(module: Any, name: str, kwargs: dict[str, Any]) -> Any:
    seconds = _timeout_seconds()
    if name == "sqlite3":
        # 测试/本地文件场景：显式 read-only URI 由调用方在 database 参数里给出。
        # 查询在工作线程执行（墙钟硬顶），必须允许跨线程使用同一连接。
        connect_kwargs = dict(kwargs)
        connect_kwargs.setdefault("check_same_thread", False)
        connection = module.connect(**connect_kwargs)
        connection.execute("PRAGMA query_only = ON")
        return connection
    connection = module.connect(**kwargs)
    if name == "snowflake.connector":
        # Snowflake 没有会话级只读开关；至少把失控查询交给服务端超时取消。
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds}")
    elif name == "psycopg":
        # 与 postgres_source 同一保证：服务端 statement_timeout 取消失控查询。
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {seconds * 1000}")
            connection.commit()
        except Exception:  # noqa: BLE001 - 某些 DB-API 包装不支持会话级 SET，由线程兜底
            pass
    return connection


def connect_readonly() -> Any:
    """按环境变量建立外部 DB-API 连接；配置/依赖问题给出可操作报错。"""
    name = module_name()
    if not name:
        raise ValueError(
            f"未配置外部数据源：请设置 {MODULE_ENV}（如 snowflake.connector）"
            f"与 {KWARGS_ENV}（JSON 连接参数）。"
        )
    if name not in _ALLOWED_MODULES:
        allowed = "、".join(sorted(_ALLOWED_MODULES))
        raise ValueError(f"不允许的 DB-API 模块「{name}」。可用：{allowed}。")
    kwargs = _kwargs()
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        hint = _ALLOWED_MODULES[name]
        raise ValueError(f"未安装驱动 {hint}，请先安装后再启用外部数据源。") from exc
    return _connect(module, name, kwargs)


def _row_value(row: Any, *names: str, index: int = 0) -> Any:
    for attr in names:
        value = getattr(row, attr, None)
        if value is not None:
            return value
    try:
        return row[index]
    except (TypeError, IndexError, KeyError):
        return None


def schema_summary(connection: Any) -> dict[str, Any]:
    """用 DB-API 的可选 ``cursor.tables()/columns()`` 做尽力内省。

    不是所有驱动都实现这两个方法；失败时返回空结构并附带说明，而不是抛错
    阻断查询——用户仍可直接写 SQL 查已知表。
    """
    tables: list[dict[str, Any]] = []
    note = ""
    try:
        cursor = connection.cursor()
    except Exception as exc:  # noqa: BLE001 - 驱动差异大，统一降级
        return {"table_count": 0, "tables": [], "note": f"无法创建游标：{exc}"}
    try:
        if not hasattr(cursor, "tables"):
            note = "该驱动未实现 cursor.tables()，请直接写 SQL 查询已知表。"
        else:
            for row in cursor.tables():
                name = _row_value(row, "table_name", "TABLE_NAME", index=2)
                if not name:
                    continue
                columns: list[dict[str, Any]] = []
                if hasattr(cursor, "columns"):
                    try:
                        for column in cursor.columns(table=str(name)):
                            column_name = _row_value(column, "column_name", "COLUMN_NAME", index=3)
                            column_type = _row_value(column, "type_name", "TYPE_NAME", index=5)
                            if column_name:
                                columns.append(
                                    {
                                        "name": str(column_name),
                                        "type": str(column_type or ""),
                                    }
                                )
                    except Exception:  # noqa: BLE001 - 列内省失败不影响表清单
                        columns = []
                tables.append({"table": str(name), "row_count": 0, "columns": columns})
    except Exception as exc:  # noqa: BLE001
        note = f"内省失败：{str(exc)[:200]}"
    finally:
        try:
            cursor.close()
        except Exception:  # noqa: BLE001
            pass
    payload: dict[str, Any] = {"table_count": len(tables), "tables": tables}
    if note:
        payload["note"] = note
    return payload


def run_select(
    connection: Any, sql: str, limit: int, *, timeout_seconds: int | None = None
) -> dict[str, Any]:
    """执行单条只读查询；语句校验、行/列上限与 JSON 安全转换复用 sql_guard。

    墙钟预算双层兜底：
    - 支持 progress handler 的驱动（sqlite3）在执行中打断；
    - 其余驱动用工作线程 + join(timeout) 硬顶——DB-API 无法中止已在服务端
      跑的语句，这里保证的是**调用方不被无限期阻塞**，并在报错里说明该边界。
    """
    statement = validate_select(sql)
    seconds = _timeout_seconds() if timeout_seconds is None else max(1, int(timeout_seconds))
    if hasattr(connection, "set_progress_handler"):
        deadline = time.monotonic() + seconds

        def _over_budget() -> int:
            return 1 if time.monotonic() > deadline else 0

        try:
            connection.set_progress_handler(_over_budget, 1000)
        except Exception:  # noqa: BLE001 - 非标准 DB-API 包装可能没有该方法
            pass
    outcome: dict[str, Any] = {}

    def _execute() -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(statement)
            outcome["result"] = collect_rows(cursor, limit)
        except BaseException as exc:  # noqa: BLE001 - 驱动异常类型各异，统一透传
            outcome["error"] = exc
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    worker = threading.Thread(target=_execute, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise ValueError(
            f"查询超过 {seconds}s 墙钟预算，已放弃等待；数据库端可能仍在执行，"
            f"请优化查询或在驱动/账号层配置服务端超时"
            f"（DATA_AGENT_EXTERNAL_DB_TIMEOUT_SECONDS）。"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]
