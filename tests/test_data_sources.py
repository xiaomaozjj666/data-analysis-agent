"""数据源注册表：能力声明、发现列表与外部源解析。"""

from __future__ import annotations

import pytest

from data_agent import data_sources, postgres_source


def test_describe_sources_declares_capabilities(monkeypatch):
    monkeypatch.setattr(postgres_source, "is_configured", lambda: False)
    monkeypatch.delenv("DATA_AGENT_EXTERNAL_DB_MODULE", raising=False)
    by_name = {item.name: item for item in data_sources.describe_sources()}
    assert set(by_name) == {"session", "postgres", "external"}
    assert by_name["session"].readonly_guarantee == data_sources.READONLY_DRIVER
    assert by_name["session"].timeout_mechanism == "sqlite progress handler"
    assert by_name["postgres"].readonly_guarantee == data_sources.READONLY_SERVER_CONFIRMED
    assert by_name["postgres"].timeout_mechanism == "statement_timeout"
    assert by_name["external"].readonly_guarantee == data_sources.READONLY_ROLE_REQUIRED
    assert by_name["postgres"].configured is False
    assert by_name["external"].configured is False
    assert by_name["session"].configured is True


def test_configured_flag_follows_environment(monkeypatch):
    monkeypatch.setattr(postgres_source, "is_configured", lambda: True)
    by_name = {item.name: item for item in data_sources.describe_sources()}
    assert by_name["postgres"].configured is True


def test_resolve_unknown_lists_available_names():
    with pytest.raises(ValueError, match="未知的 source") as excinfo:
        data_sources.resolve("snowflake")
    assert "session" in str(excinfo.value) and "postgres" in str(excinfo.value)


def test_open_external_rejects_session_context():
    with pytest.raises(ValueError, match="工作区上下文"):
        data_sources.open_external("session")


def test_open_external_requires_postgres_configuration(monkeypatch):
    monkeypatch.setattr(postgres_source, "is_configured", lambda: False)
    with pytest.raises(ValueError, match="DATA_AGENT_DATABASE_URL"):
        data_sources.open_external("postgres")


def test_open_external_returns_driver_connection_and_label(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(postgres_source, "is_configured", lambda: True)
    monkeypatch.setattr(postgres_source, "connect_readonly", lambda: sentinel)
    driver, connection, label = data_sources.open_external("postgres")
    assert driver is postgres_source
    assert connection is sentinel
    assert "PostgreSQL" in label


def test_open_external_unimplemented_source_reports_clearly(monkeypatch):
    fake = data_sources.SourceDescriptor(
        name="snowflake",
        label="Snowflake",
        readonly_guarantee=data_sources.READONLY_ROLE_REQUIRED,
        identifier_quote="double",
        timeout_mechanism="statement_timeout",
    )
    monkeypatch.setattr(data_sources, "describe_sources", lambda: [fake])
    with pytest.raises(ValueError, match="尚未实现执行分支"):
        data_sources.open_external("snowflake")
