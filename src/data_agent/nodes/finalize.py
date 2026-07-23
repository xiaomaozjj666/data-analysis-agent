"""finalize 节点：汇总各步骤证据并生成最终中文分析报告。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from data_agent.callbacks import (
    ReasoningStreamCallback,
    ReportStreamCallback,
    UsageAccumulator,
)
from data_agent.nodes._utils import _message_text
from data_agent.nodes.state import WorkflowState
from data_agent.prompts import _FINALIZE_EVIDENCE_BUDGET, _FINALIZE_PER_STEP_MIN_CHARS

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent


def finalize(agent: DataAnalysisAgent, state: WorkflowState) -> dict[str, Any]:
    agent._ensure_not_cancelled()
    agent._enter_node("finalize", "正在汇总最终报告")
    # 按步数分配 evidence 预算，避免硬截断 30k 时把后面步骤的 summary 整段丢掉。
    # 每步至少保留 800 字符（短 summary 不受影响），剩余预算按步均分。
    completed_steps = state.get("completed_steps", []) or []
    total_budget = _FINALIZE_EVIDENCE_BUDGET
    per_step_min = _FINALIZE_PER_STEP_MIN_CHARS
    if completed_steps:
        reserved = min(total_budget, per_step_min * len(completed_steps))
        remaining = max(0, total_budget - reserved)
        per_step_extra = remaining // len(completed_steps)
        per_step_limit = per_step_min + per_step_extra
    else:
        per_step_limit = total_budget

    def _trim_summary(text: str, limit: int) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}…（已截断）"

    evidence_parts = [
        f"## {item['title']}\n{_trim_summary(item.get('summary', ''), per_step_limit)}"
        for item in completed_steps
    ]
    evidence = "\n\n".join(evidence_parts)

    # 报告写给不懂统计的业务读者：结论先行（金字塔结构），证据跟上，
    # 统计术语必须当场翻译成大白话，禁止学术八股式的章节堆砌。
    prompt = agent.prompts["finalize_template"].format(
        query=state["query"], evidence=evidence
    )
    try:
        # 流式报告：ReportStreamCallback 把每个 token 通过 event_callback
        # 推送到前端，用户看着报告逐字写出，而不是等 30-60 秒看完整报告。
        # ReasoningStreamCallback 把 DeepSeek reasoning_content 以 thinking_chunk
        # 事件实时推送，让用户看到 Agent 的思考过程。
        # UsageAccumulator 累计 finalize 节点的 token 用量。
        reasoning_buffer: list[str] = []
        report_streamer = ReportStreamCallback(agent.event_callback)
        reasoning_streamer = ReasoningStreamCallback(
            agent.event_callback, buffer=reasoning_buffer
        )
        usage_acc = UsageAccumulator()
        final_message = agent.model.invoke(
            prompt,
            config=agent._invoke_config(report_streamer, reasoning_streamer, usage_acc),
        )
        response = _message_text(final_message)
    except Exception:
        response = ""
        reasoning_buffer = []
        usage_acc = None
    if not response:
        # LLM 汇总失败时，把各步骤的标题与摘要作为兜底返回，避免用户看到空白。
        if evidence:
            response = (
                "> 模型汇总失败，以下是各分析步骤的执行摘要。\n\n"
                + evidence
            )
        else:
            response = "分析已完成，但没有可汇总的步骤结果。"
    return {
        "response": response,
        "artifacts": list(agent.workspace.artifacts),
        "dataset_profile": agent.workspace.profile(sample_rows=5),
        "plan": state.get("plan", []),
        "completed_steps": state.get("completed_steps", []),
        "trace": state.get("trace", []),
        "usage": usage_acc.snapshot() if usage_acc else None,
        "reasoning": "".join(reasoning_buffer),
    }
