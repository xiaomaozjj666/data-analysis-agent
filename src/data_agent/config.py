from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
SUPPORTED_PROVIDERS = {"deepseek", "openai"}


@dataclass(slots=True)
class AgentSettings:
    """Runtime configuration for the model and graph."""

    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-v4-pro"
    base_url: str | None = DEEPSEEK_BASE_URL
    thinking_enabled: bool = True
    reasoning_effort: str = "high"
    temperature: float = 0.0
    max_iterations: int = 25
    max_plan_steps: int = 8
    timeout_seconds: float = 120.0
    runs_dir: Path = Path("runs")

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        provider: str | None = None,
    ) -> AgentSettings:
        load_dotenv(env_file)
        selected_provider = (provider or os.getenv("MODEL_PROVIDER", "deepseek")).strip().lower()
        if selected_provider == "deepseek":
            api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
            model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
            base_url = os.getenv("DEEPSEEK_API_BASE", DEEPSEEK_BASE_URL).strip() or DEEPSEEK_BASE_URL
            thinking_enabled = os.getenv("DEEPSEEK_THINKING", "true").strip().lower() not in {"0", "false", "no", "off"}
            reasoning_effort = os.getenv("DEEPSEEK_REASONING_EFFORT", "high").strip().lower()
        else:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
            base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
            thinking_enabled = False
            reasoning_effort = "high"
        return cls(
            provider=selected_provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            temperature=float(os.getenv("AGENT_TEMPERATURE", "0")),
            max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "25")),
            max_plan_steps=int(os.getenv("AGENT_MAX_PLAN_STEPS", "8")),
            timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "120")),
            runs_dir=Path(os.getenv("DATA_AGENT_RUNS_DIR", "runs")),
        )

    def validate_for_model(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"不支持的模型提供商：{self.provider}。可用值：deepseek、openai。")
        if not self.api_key:
            key_name = "DEEPSEEK_API_KEY" if self.provider == "deepseek" else "OPENAI_API_KEY"
            raise ValueError(
                "未配置 API Key。请在页面侧栏填写，或复制 .env.example 为 .env 后设置 "
                f"{key_name}。使用本地 Ollama 时可填写任意非空值。"
            )
        if not self.model:
            raise ValueError("模型名称不能为空。")
        if self.provider == "deepseek" and self.reasoning_effort not in {"high", "max"}:
            raise ValueError("DeepSeek reasoning_effort 仅支持 high 或 max。")
        if not 1 <= self.max_iterations <= 100:
            raise ValueError("AGENT_MAX_ITERATIONS 必须在 1 到 100 之间。")
        if not 2 <= self.max_plan_steps <= 12:
            raise ValueError("AGENT_MAX_PLAN_STEPS 必须在 2 到 12 之间。")
