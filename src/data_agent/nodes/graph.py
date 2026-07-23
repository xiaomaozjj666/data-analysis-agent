"""LangGraph StateGraph 装配：把节点函数接入有向图。

``build_graph(agent)`` 接收 ``DataAnalysisAgent`` 实例，把五个节点函数
（接收 ``(agent, state)``）通过 ``functools.partial`` 绑定 agent 后接入
StateGraph，保持原 ``_build_workflow`` 的边与条件路由完全不变。
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from data_agent.nodes.execute import execute_step
from data_agent.nodes.finalize import finalize
from data_agent.nodes.plan import plan_analysis
from data_agent.nodes.replan import replan
from data_agent.nodes.state import WorkflowState
from data_agent.nodes.validate import validate_dataset

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent


def route_after_review(state: WorkflowState) -> str:
    return "execute_step" if state.get("remaining_steps") else "finalize"


def build_graph(agent: DataAnalysisAgent) -> Any:
    graph = StateGraph(WorkflowState)
    graph.add_node("validate_dataset", partial(validate_dataset, agent))
    graph.add_node("plan_analysis", partial(plan_analysis, agent))
    graph.add_node("execute_step", partial(execute_step, agent))
    graph.add_node("replan", partial(replan, agent))
    graph.add_node("finalize", partial(finalize, agent))
    graph.add_edge(START, "validate_dataset")
    graph.add_edge("validate_dataset", "plan_analysis")
    graph.add_edge("plan_analysis", "execute_step")
    graph.add_edge("execute_step", "replan")
    graph.add_conditional_edges(
        "replan",
        route_after_review,
        {"execute_step": "execute_step", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile()
