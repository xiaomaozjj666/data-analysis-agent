from __future__ import annotations

import os

import pytest

from data_agent.config import DEEPSEEK_BASE_URL, AgentSettings, sanitize_no_proxy_env


def test_deepseek_is_default_provider(monkeypatch):
    for name in (
        "MODEL_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_API_BASE",
        "DEEPSEEK_THINKING",
        "DEEPSEEK_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = AgentSettings.from_env()
    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-flash"
    assert settings.base_url == DEEPSEEK_BASE_URL
    assert settings.thinking_enabled is True


def test_provider_specific_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    settings = AgentSettings.from_env(provider="openai")
    assert settings.provider == "openai"
    assert settings.api_key == "openai-test"
    assert settings.model == "custom-model"
    assert settings.thinking_enabled is False


def test_rejects_invalid_deepseek_effort():
    settings = AgentSettings(api_key="test", reasoning_effort="medium")
    with pytest.raises(ValueError, match="high 或 max"):
        settings.validate_for_model()


def test_sanitize_no_proxy_env_drops_bracketed_entries(monkeypatch):
    """带方括号的 IPv6 条目会让 httpx 构造客户端就抛 InvalidURL，必须剔除。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    removed = sanitize_no_proxy_env()
    assert removed == ["[::1]"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    # 幂等：再跑一次不动任何东西
    assert sanitize_no_proxy_env() == []


def test_sanitize_no_proxy_env_handles_lowercase_and_missing(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("no_proxy", "[::1],*.internal")
    assert sanitize_no_proxy_env() == ["[::1]"]
    assert os.environ["no_proxy"] == "*.internal"
    monkeypatch.delenv("no_proxy", raising=False)
    assert sanitize_no_proxy_env() == []


def test_sanitize_no_proxy_env_can_empty_the_value(monkeypatch):
    """全是非法条目时清空而不是留下空逗号（空串等于未设置）。"""
    monkeypatch.setenv("NO_PROXY", "[::1],[::2]")
    assert sanitize_no_proxy_env() == ["[::1]", "[::2]"]
    assert os.environ["NO_PROXY"] == ""


def test_from_env_sanitizes_before_loading_model_settings(monkeypatch):
    """from_env 必须在配置阶段就清理，否则模型客户端一构造就失败。"""
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    AgentSettings.from_env()
    assert os.environ["NO_PROXY"] == "localhost"
