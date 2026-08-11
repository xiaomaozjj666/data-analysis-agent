"""环境变量驱动的 Agent 运行时配置。

所有配置项均可通过环境变量覆盖，优先级：
    代码显式赋值 > 环境变量 > dataclass 默认值

支持的模型提供商：
- deepseek: 使用 langchain-deepseek SDK，支持 thinking mode。
- openai: 使用 langchain-openai SDK，兼容任何 OpenAI API 格式的服务。

典型用法::

    settings = AgentSettings.from_env()  # 从 .env 和环境变量加载
    settings.validate_for_model()        # 创建模型前验证必填项
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: DeepSeek 官方 API 地址。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: 支持的模型提供商集合。
SUPPORTED_PROVIDERS = {"deepseek", "openai"}


@dataclass(slots=True)
class AgentSettings:
    """模型和图运行时配置。

    所有字段均可通过环境变量覆盖，见 ``from_env()`` 中的映射关系。
    资源限制类字段（max_rows、max_cells、max_upload_bytes 等）用于
    API 层的输入验证，防止意外资源耗尽。

    Attributes:
        provider: 模型提供商（deepseek / openai）。
        api_key: 模型 API 密钥。
        model: 模型名称。
        base_url: API 基础地址（None 表示使用提供商默认）。
        thinking_enabled: 是否启用 DeepSeek thinking mode。
        reasoning_effort: 推理努力程度（high / max）。
        temperature: 采样温度（thinking mode 下忽略）。
        max_iterations: ReAct 执行器单步最大迭代数。
        max_plan_steps: 计划最大步骤数。
        timeout_seconds: 单次 LLM 调用超时。
        runs_dir: 工作区根目录。
        max_upload_bytes: 上传文件大小上限。
        max_rows: 数据最大行数。
        max_cells: 数据最大单元格数。
        max_active_sessions: 最大活跃会话数。
        session_ttl_hours: 会话过期时间。
        rate_limit_per_minute: 每客户端每分钟请求上限。
        max_concurrent_analyses: 全局并发分析上限。
        language: Agent 提示词与报告语言（zh / en）。
    """

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
    max_upload_bytes: int = 200 * 1024 * 1024
    max_rows: int = 1_000_000
    max_cells: int = 10_000_000
    max_active_sessions: int = 100
    session_ttl_hours: float = 24.0
    rate_limit_per_minute: int = 30
    max_concurrent_analyses: int = 2
    language: str = "zh"

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        provider: str | None = None,
    ) -> AgentSettings:
        """从环境变量和 .env 文件加载配置。

        Args:
            env_file: 可选的 .env 文件路径，默认自动查找项目根目录。
            provider: 强制指定提供商，覆盖 MODEL_PROVIDER 环境变量。

        Returns:
            填充完毕的 AgentSettings 实例（未验证，需调用 validate_for_model）。
        """
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
            max_upload_bytes=int(os.getenv("DATA_AGENT_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024))),
            max_rows=int(os.getenv("DATA_AGENT_MAX_ROWS", "1000000")),
            max_cells=int(os.getenv("DATA_AGENT_MAX_CELLS", "10000000")),
            max_active_sessions=int(os.getenv("DATA_AGENT_MAX_ACTIVE_SESSIONS", "100")),
            session_ttl_hours=float(os.getenv("DATA_AGENT_SESSION_TTL_HOURS", "24")),
            rate_limit_per_minute=int(os.getenv("DATA_AGENT_RATE_LIMIT_PER_MINUTE", "30")),
            max_concurrent_analyses=int(os.getenv("DATA_AGENT_MAX_CONCURRENT_ANALYSES", "2")),
            language=(os.getenv("DATA_AGENT_LANGUAGE", "zh").strip().lower() or "zh"),
        )

    def validate_for_model(self) -> None:
        """验证创建 Chat Model 所需的必填配置项。

        Raises:
            ValueError: 当 provider、api_key、model 或其他配置不合法时。
        """
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
        if self.max_upload_bytes <= 0 or self.max_rows <= 0 or self.max_cells <= 0:
            raise ValueError("数据资源上限必须为正数。")
        if self.max_active_sessions <= 0 or self.session_ttl_hours <= 0:
            raise ValueError("会话资源上限必须为正数。")
        if self.rate_limit_per_minute <= 0:
            raise ValueError("DATA_AGENT_RATE_LIMIT_PER_MINUTE 必须为正数。")
        if self.max_concurrent_analyses <= 0:
            raise ValueError("DATA_AGENT_MAX_CONCURRENT_ANALYSES 必须为正数。")
        if self.language not in {"zh", "en"}:
            raise ValueError(f"不支持的 language：{self.language}。可用值：zh、en。")
