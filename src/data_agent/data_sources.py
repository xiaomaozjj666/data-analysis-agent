"""数据源注册表：统一"有哪些源、各自的安全保证、如何连接"。

背景（2026-11 竞品复核后的工程结论）：`query_database` 与 MCP 的 `sql_query`
各自维护一份 source 分派，已经出现"工具文档说返回 available_sources、实现里
却没有"的漂移；未来接 Snowflake / BigQuery / Databricks 时若继续复制分支，
每处都要改、且容易把「服务端只读确认」（Postgres 独有）误用到只有角色只读
的数仓上。

本模块只做三件确定性强、无需真实凭据的事：
- 声明每个源的**能力等级**（只读是怎么保证的、超时机制、标识符引号）；
- 提供统一的发现列表（供工具与 MCP 的 discovery 输出）；
- 外部源的连接解析（目前只有 postgres，新增驱动只需在这里登记）。

刻意不做的事：不提供未验证的占位驱动。项目政策是"没有可验证手段就不合入"，
占位模块会被提示词/发现结果当成可用源，制造假能力。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

#: 只读保证等级：driver=驱动层只读；server-confirmed=服务端设置并回读确认；
#: role-required=仅靠账号权限（尚未有驱动实现）。
READONLY_DRIVER = "driver"
READONLY_SERVER_CONFIRMED = "server-confirmed"
READONLY_ROLE_REQUIRED = "role-required"


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """一个可选数据源的能力描述（面向模型与用户，不参与执行）。"""

    name: str
    label: str
    readonly_guarantee: str
    identifier_quote: str
    timeout_mechanism: str
    configured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _postgres_configured() -> bool:
    from data_agent import postgres_source

    return postgres_source.is_configured()


def _external_configured() -> bool:
    from data_agent import external_source

    return external_source.is_configured()


def describe_sources() -> list[SourceDescriptor]:
    """列出全部已实现的数据源及其能力；``configured`` 反映当前环境。"""
    return [
        SourceDescriptor(
            name="session",
            label="会话内 SQLite 文件 / 活动数据集（表名 data）",
            readonly_guarantee=READONLY_DRIVER,
            identifier_quote="double",
            timeout_mechanism="sqlite progress handler",
            configured=True,
        ),
        SourceDescriptor(
            name="postgres",
            label="DATA_AGENT_DATABASE_URL 配置的 PostgreSQL",
            readonly_guarantee=READONLY_SERVER_CONFIRMED,
            identifier_quote="double",
            timeout_mechanism="statement_timeout",
            configured=_postgres_configured(),
        ),
        SourceDescriptor(
            name="external",
            label=(
                "DATA_AGENT_EXTERNAL_DB_MODULE 配置的 DB-API 数据源"
                "（Snowflake / Databricks / BigQuery 等）"
            ),
            readonly_guarantee=READONLY_ROLE_REQUIRED,
            identifier_quote="double",
            timeout_mechanism="driver/account-level; SQL guard enforces single SELECT",
            configured=_external_configured(),
        ),
    ]


def source_names() -> list[str]:
    return [item.name for item in describe_sources()]


def resolve(name: str) -> SourceDescriptor:
    """按名称取源描述；未知源抛 ValueError（文案列出全部可用值）。"""
    for item in describe_sources():
        if item.name == name:
            return item
    raise ValueError(
        f"未知的 source「{name}」。可用值：{'、'.join(source_names())}"
        "（session=会话内 SQLite 文件，postgres=环境变量配置的连接，"
        "external=配置式 DB-API 数仓）。"
    )


def open_external(name: str) -> tuple[Any, Any, str]:
    """打开一个外部数据源的只读连接，返回 ``(driver_module, connection, label)``。

    只处理已实现的网络型数据源；``session`` 由调用方按工作区上下文处理。
    """
    descriptor = resolve(name)
    if name == "session":
        raise ValueError("session 源需要工作区上下文，请由查询工具自行处理。")
    if name == "postgres":
        from data_agent import postgres_source

        if not postgres_source.is_configured():
            raise ValueError("未配置 PostgreSQL：请设置 DATA_AGENT_DATABASE_URL 后重试。")
        return postgres_source, postgres_source.connect_readonly(), descriptor.label
    if name == "external":
        from data_agent import external_source

        if not external_source.is_configured():
            raise ValueError(
                "未配置外部数据源：请设置 DATA_AGENT_EXTERNAL_DB_MODULE"
                " 与 DATA_AGENT_EXTERNAL_DB_KWARGS 后重试。"
            )
        return external_source, external_source.connect_readonly(), descriptor.label
    # 走到这里说明 README/registry 已声明但执行分支未实现——显式报错，
    # 不静默回退到其他源。
    raise ValueError(f"数据源「{name}」尚未实现执行分支。")
