"""Data analysis agent built with LangChain, LangGraph and ReAct."""

from __future__ import annotations

from typing import Any

__all__ = ["AgentSettings", "AnalysisResult", "DataAnalysisAgent", "DataWorkspace"]
__version__ = "1.0.0"


def __getattr__(name: str) -> Any:
    # 延迟导入重模块：沙箱子进程 ``python -m data_agent.sandbox_worker`` 只应
    # 加载 pandas/numpy，不能把 LangGraph / FastAPI 整栈拉进来。
    if name == "AgentSettings":
        from data_agent.config import AgentSettings

        return AgentSettings
    if name in {"AnalysisResult", "DataAnalysisAgent"}:
        from data_agent.agent import AnalysisResult, DataAnalysisAgent

        return AnalysisResult if name == "AnalysisResult" else DataAnalysisAgent
    if name == "DataWorkspace":
        from data_agent.workspace import DataWorkspace

        return DataWorkspace
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
