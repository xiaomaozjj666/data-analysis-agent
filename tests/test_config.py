from __future__ import annotations

import pytest

from data_agent.config import DEEPSEEK_BASE_URL, AgentSettings


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
    assert settings.model == "deepseek-v4-pro"
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
