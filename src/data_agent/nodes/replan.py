"""replan 节点：审查执行进度并决定提前结束或补充后续步骤。"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from data_agent.models import ReplanDecision
from data_agent.nodes.state import WorkflowState
from data_agent.prompts import _REPLAN_PAYLOAD_MAX_CHARS

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent

logger = logging.getLogger(__name__)


def _needs_replan(
    *, completed: list[dict[str, Any]], remaining: list[dict[str, str]], artifact_count: int,
) -> bool:
    """是否值得花一次 LLM 往返咨询重规划器。

    实测：一次"按地区对比销售额 + 画关系图"的简单任务会走 6 轮 replan，
    每轮约 6 秒、合计 ~37 秒，而绝大多数轮次的结论就是"按原计划继续"——
    这笔开销换来的是"用户觉得卡住"。真正需要重规划的情形很少：

    - 有步骤失败 → 需要补偿步骤；
    - 计划已执行完 → 需要判断"目标是否达成、要不要补步骤"；
    - 连续两步没有产出任何产物 → 方向可能不对，值得让模型重新审视。

    其余情况（步骤成功、计划还有后续、已有产物）直接沿用原计划剩余步骤，
    省掉往返；``replan_reason`` 里写明是"跳过咨询"而不是模型的决定。
    """
    if any(item.get("status") == "failed" for item in completed):
        return True
    if not remaining:
        return True
    if artifact_count == 0 and len(completed) >= 2:
        return True
    return False


def replan(agent: DataAnalysisAgent, state: WorkflowState) -> dict[str, Any]:
    agent._ensure_not_cancelled()
    agent._enter_node("replan", "正在审查进度并重规划")
    current = state.get("last_step_result", {})
    completed = [*state.get("completed_steps", [])]
    if current:
        completed.append(current)
    original_remaining = list(state.get("remaining_steps", []))[1:]
    # 墙钟预算：超时后不再开新步骤，直接进汇总。宁可给一份"基于已完成部分"的
    # 报告，也不要让用户无限等下去（实测同一句任务可能 230s 也可能 680s）。
    budget_left = agent.analysis_budget_left()
    if budget_left <= 0 and original_remaining:
        return {
            "completed_steps": completed,
            "remaining_steps": [],
            "replan_reason": (
                f"已用完 {agent.settings.max_analysis_seconds / 60:.0f} 分钟分析预算，"
                f"跳过剩余 {len(original_remaining)} 个步骤并直接汇总已完成部分。"
            ),
        }
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
    if not _needs_replan(
        completed=completed,
        remaining=original_remaining,
        artifact_count=review_payload["artifact_count"],
    ):
        # 进度正常：不花 LLM 往返，直接按原计划继续（reason 里说明是跳过而非模型判断）
        return {
            "completed_steps": completed,
            "remaining_steps": original_remaining,
            "replan_reason": "步骤正常完成且仍有后续步骤，按原计划继续（已跳过重规划咨询）。",
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
        logger.exception("Replan LLM decision failed, keeping original remaining steps")
        next_steps = original_remaining
        reason = "保留原计划中的后续步骤。"
    return {
        "completed_steps": completed,
        "remaining_steps": next_steps,
        "replan_reason": reason,
    }
