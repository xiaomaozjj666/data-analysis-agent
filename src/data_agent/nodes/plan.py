"""plan_analysis 节点：使用 LLM structured output 生成分析计划。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from data_agent.models import AnalysisPlan
from data_agent.nodes.state import WorkflowState
from data_agent.prompts import _PLAN_PROFILE_MAX_CHARS, _apply_query_constraints, _fallback_plan

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent


def plan_analysis(agent: DataAnalysisAgent, state: WorkflowState) -> dict[str, Any]:
    agent._ensure_not_cancelled()
    # 断点续跑：如果已有 plan（从 resume_from 注入），跳过 LLM 规划直接复用。
    # 这样 execute_step 会从 remaining_steps 的第一项开始，跳过已完成的步骤。
    existing_plan = state.get("plan") or []
    existing_completed = state.get("completed_steps") or []
    if existing_plan and existing_completed:
        completed_ids = {item.get("id") for item in existing_completed if item.get("id")}
        remaining = [step for step in existing_plan if step.get("id") not in completed_ids]
        agent._enter_node("plan_analysis", "正在恢复分析进度")
        return {
            "objective": state.get("objective") or state["query"],
            "plan": existing_plan,
            "remaining_steps": remaining,
        }
    agent._enter_node("plan_analysis", "正在规划分析步骤")
    profile_text = json.dumps(state["dataset_profile"], ensure_ascii=False)[:_PLAN_PROFILE_MAX_CHARS]
    prompt = agent.prompts["plan_template"].format(
        query=state["query"], profile_text=profile_text
    )
    try:
        plan = agent.planner.invoke(prompt, config=agent._invoke_config())
        if not isinstance(plan, AnalysisPlan):
            plan = AnalysisPlan.model_validate(plan)
    except Exception:
        plan = _fallback_plan(state["query"], agent.prompts)
    plan = _apply_query_constraints(state["query"], plan)
    steps = [step.model_dump() for step in plan.steps[: agent.settings.max_plan_steps]]
    return {"objective": plan.objective, "plan": steps, "remaining_steps": steps}
