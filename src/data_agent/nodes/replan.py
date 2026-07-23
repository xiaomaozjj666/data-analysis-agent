"""replan 节点：审查执行进度并决定提前结束或补充后续步骤。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from data_agent.models import ReplanDecision
from data_agent.nodes.state import WorkflowState
from data_agent.prompts import _REPLAN_PAYLOAD_MAX_CHARS

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent


def replan(agent: DataAnalysisAgent, state: WorkflowState) -> dict[str, Any]:
    agent._ensure_not_cancelled()
    agent._enter_node("replan", "正在审查进度并重规划")
    current = state.get("last_step_result", {})
    completed = [*state.get("completed_steps", [])]
    if current:
        completed.append(current)
    original_remaining = list(state.get("remaining_steps", []))[1:]
    if len(completed) >= agent.settings.max_plan_steps:
        return {
            "completed_steps": completed,
            "remaining_steps": [],
            "replan_reason": "已达到计划步骤上限，进入汇总。",
        }

    # 如果当前步骤失败且没有后续步骤，直接结束避免无意义的重规划循环。
    if current.get("status") == "failed" and not original_remaining:
        return {
            "completed_steps": completed,
            "remaining_steps": [],
            "replan_reason": "步骤执行失败且无后续步骤，进入汇总。",
        }

    review_payload = {
        "objective": state.get("objective"),
        "completed": completed,
        "remaining": original_remaining,
        "artifact_count": len(state.get("artifacts", [])),
        "failed_steps": [item for item in completed if item.get("status") == "failed"],
    }
    try:
        replan_prompt = agent.prompts["replan_template"].format(
            payload=json.dumps(review_payload, ensure_ascii=False)[:_REPLAN_PAYLOAD_MAX_CHARS]
        )
        decision = agent.replanner.invoke(
            replan_prompt,
            config=agent._invoke_config(),
        )
        if not isinstance(decision, ReplanDecision):
            decision = ReplanDecision.model_validate(decision)
        if decision.done:
            next_steps: list[dict[str, str]] = []
        else:
            executed_ids = {item.get("id") for item in completed}
            next_steps = [
                step.model_dump()
                for step in decision.remaining_steps
                if step.id not in executed_ids
            ][: max(agent.settings.max_plan_steps - len(completed), 0)]
            if not next_steps:
                next_steps = original_remaining
        reason = decision.rationale
    except Exception:
        next_steps = original_remaining
        reason = "保留原计划中的后续步骤。"
    return {
        "completed_steps": completed,
        "remaining_steps": next_steps,
        "replan_reason": reason,
    }
