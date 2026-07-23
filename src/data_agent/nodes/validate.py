"""validate_dataset 节点：检查数据集结构并初始化工作流状态。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage

from data_agent.nodes.state import WorkflowState

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent


def validate_dataset(agent: DataAnalysisAgent, state: WorkflowState) -> dict[str, Any]:
    agent._ensure_not_cancelled()
    agent._enter_node("validate_dataset", "正在检查数据集结构")
    profile = agent.workspace.profile(sample_rows=5)
    messages = list(state.get("input_messages", []))
    if not messages:
        messages = [HumanMessage(content=state["query"])]
    # 断点续跑：保留已有的 completed_steps / plan / remaining_steps / objective，
    # 否则会被覆盖为空导致 plan_analysis 重新规划。
    return {
        "dataset_profile": profile,
        "input_messages": messages,
        "trace": list(state.get("trace", [])),
        "artifacts": list(agent.workspace.artifacts),
        "completed_steps": list(state.get("completed_steps", [])),
        "plan": list(state.get("plan", [])),
        "remaining_steps": list(state.get("remaining_steps", [])),
        "objective": state.get("objective", ""),
    }
