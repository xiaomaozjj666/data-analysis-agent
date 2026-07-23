"""LangGraph 工作流节点包。

从 ``agent.py`` 拆分出的五节点工作流：每个节点是接收 ``(agent, state)``
的独立函数，``build_graph`` 负责把它们接入 StateGraph。节点逻辑、提示词
和控制流保持不变，仅做结构拆分。
"""

from __future__ import annotations

from data_agent.nodes.execute import execute_step
from data_agent.nodes.finalize import finalize
from data_agent.nodes.graph import build_graph
from data_agent.nodes.plan import plan_analysis
from data_agent.nodes.replan import replan
from data_agent.nodes.state import WorkflowState
from data_agent.nodes.validate import validate_dataset

__all__ = [
    "WorkflowState",
    "build_graph",
    "execute_step",
    "finalize",
    "plan_analysis",
    "replan",
    "validate_dataset",
]
