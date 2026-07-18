from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.storage import build_session_storage
from data_agent.workspace import DataWorkspace


class DeploymentState(TypedDict, total=False):
    query: str
    dataset_path: str
    dataset_id: str
    response: str
    plan: list[dict[str, str]]
    completed_steps: list[dict[str, Any]]
    trace: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    dataset_profile: dict[str, Any]


def make_graph(config: RunnableConfig | None = None):
    """Build the graph exported to LangSmith Agent Server."""
    settings = AgentSettings.from_env(provider="deepseek")
    storage = build_session_storage()
    configurable = (config or {}).get("configurable", {})
    thread_id = str(configurable.get("thread_id") or uuid4().hex)

    def run_analysis(state: DeploymentState) -> dict[str, Any]:
        if state.get("dataset_id"):
            dataset_id = state["dataset_id"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", dataset_id):
                raise ValueError("dataset_id 格式无效。")
            input_dir = (settings.runs_dir / dataset_id / "input").resolve()
            if not input_dir.is_dir():
                storage.restore_session(dataset_id, input_dir.parent)
            if settings.runs_dir.resolve() not in input_dir.parents or not input_dir.is_dir():
                raise FileNotFoundError("找不到指定的数据集工作区。")
            candidates = sorted(path for path in input_dir.iterdir() if path.is_file())
            if not candidates:
                raise FileNotFoundError("指定的数据集没有可读取的文件。")
            source = candidates[0]
        else:
            source = Path(state["dataset_path"]).expanduser().resolve()
        workspace = DataWorkspace(settings.runs_dir, session_id=f"deploy_{thread_id[:24]}")
        workspace.load(source, copy_into_workspace=True)
        result = DataAnalysisAgent(workspace, settings).run(state["query"])
        storage.sync_session(workspace.root.name, workspace.root)
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
