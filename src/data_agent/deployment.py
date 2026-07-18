from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.workspace import DataWorkspace


class DeploymentState(TypedDict, total=False):
    query: str
    dataset_path: str
    response: str
    plan: list[dict[str, str]]
    completed_steps: list[dict[str, Any]]
    trace: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    dataset_profile: dict[str, Any]


def make_graph(config: RunnableConfig | None = None):
    """Build the graph exported to LangSmith Agent Server."""
    settings = AgentSettings.from_env(provider="deepseek")
    configurable = (config or {}).get("configurable", {})
    thread_id = str(configurable.get("thread_id") or uuid4().hex)

    def run_analysis(state: DeploymentState) -> dict[str, Any]:
        source = Path(state["dataset_path"]).expanduser().resolve()
        workspace = DataWorkspace(settings.runs_dir, session_id=f"deploy_{thread_id[:24]}")
        workspace.load(source, copy_into_workspace=True)
        result = DataAnalysisAgent(workspace, settings).run(state["query"])
        return {
            "response": result.response,
            "plan": result.plan,
            "completed_steps": result.completed_steps,
            "trace": result.trace,
            "artifacts": result.artifacts,
            "dataset_profile": result.dataset_profile,
        }

    builder = StateGraph(DeploymentState)
    builder.add_node("plan_and_execute_analysis", run_analysis)
    builder.add_edge(START, "plan_and_execute_analysis")
    builder.add_edge("plan_and_execute_analysis", END)
    return builder.compile()
