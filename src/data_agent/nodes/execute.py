"""execute_step 节点：ReAct 执行器运行单个分析步骤。

包含从 ``agent.py`` 拆分出的 ``_tool_trace`` / ``_format_error_text``
辅助函数（仅本节点使用）。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from data_agent.callbacks import ToolTraceCallback
from data_agent.models import AnalysisCancelled
from data_agent.nodes._utils import _message_text
from data_agent.nodes.state import WorkflowState
from data_agent.prompts import (
    _FORMAT_ERROR_DISPLAY_MAX_CHARS,
    _TRACE_DETAIL_MAX_CHARS,
    _humanize_error,
    _is_recoverable_format_error,
    _query_allows_format_repair,
)

if TYPE_CHECKING:
    from data_agent.agent import DataAnalysisAgent


def _tool_trace(messages: list[BaseMessage]) -> list[dict[str, str]]:
    """从 ReAct 执行器的消息序列中提取工具调用审计轨迹。

    每条 ToolMessage 的 detail 截断到 _TRACE_DETAIL_MAX_CHARS，
    防止单条工具输出（如 inspect_data 返回的大 profile）撑爆 trace。
    """
    trace: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                trace.append(
                    {
                        "type": "tool_call",
                        "name": call["name"],
                        "detail": str(call.get("args", {})),
                    }
                )
        elif isinstance(message, ToolMessage):
            trace.append(
                {
                    "type": "tool_result",
                    "name": message.name or "tool",
                    "detail": _message_text(message)[:_TRACE_DETAIL_MAX_CHARS],
                }
            )
    return trace


def _format_error_text(messages: list[BaseMessage]) -> str:
    return "\n".join(
        _message_text(message)
        for message in messages
        if isinstance(message, ToolMessage)
        and message.additional_kwargs.get("error_code") == "format_error"
    )


def execute_step(agent: DataAnalysisAgent, state: WorkflowState) -> dict[str, Any]:
    agent._ensure_not_cancelled()
    remaining = list(state.get("remaining_steps", []))
    if not remaining:
        return {"current_step": {}, "last_step_result": {}}
    step = remaining[0]
    step_index = len(state.get("completed_steps", [])) + 1
    total_steps = step_index + len(remaining) - 1
    agent._enter_node(
        "execute_step",
        f"正在执行 ({step_index}/{total_steps})：{step.get('title', step.get('id', '未知步骤'))}",
    )
    completed = state.get("completed_steps", [])
    completed_text = "\n".join(
        f"- {item['title']}: {item.get('summary', '')[:800]}" for item in completed
    ) or "尚无"
    # 增强上下文：把数据概况传递给 ReAct 执行器，避免每步都重新 inspect_data。
    # 附带每列的 dtype 与 unique 计数：模型选图前能直接判断列基数
    # （unique ≈ 行数→标识符列，unique = 1→常量列），从源头避免无意义图表。
    profile_brief = json.dumps(
        {
            "rows": state.get("dataset_profile", {}).get("rows"),
            "columns": state.get("dataset_profile", {}).get("columns"),
            "column_brief": [
                {"name": col["name"], "dtype": col.get("dtype"), "unique": col.get("unique")}
                for col in state.get("dataset_profile", {}).get("column_info", [])[:20]
            ],
        },
        ensure_ascii=False,
    )
    execution_prompt = agent.prompts["execution_template"].format(
        objective=state["objective"],
        step_title=step["title"],
        step_instruction=step["instruction"],
        step_success_criteria=step["success_criteria"],
        profile_brief=profile_brief,
        completed_text=completed_text,
    )
    messages: list[BaseMessage] = []
    recovery_note = ""
    snapshot = agent.workspace.snapshot_state()
    # 工具追踪：ToolTraceCallback 把 ReAct 循环内每次工具调用实时
    # 推送到前端，让用户看到"正在读取数据→正在清洗→正在生成图表"，
    # 而不是只看到"正在执行 (2/4)"一行字等 30 秒。
    tool_tracer = ToolTraceCallback(agent.event_callback)
    try:
        result = agent.react_agent.invoke(
            {"messages": [*state.get("input_messages", []), HumanMessage(content=execution_prompt)]},
            config=agent._invoke_config(tool_tracer, recursion_limit=agent.settings.max_iterations * 2 + 5),
        )
        messages = result["messages"]
        format_error = _format_error_text(messages)
        if format_error and _query_allows_format_repair(state["query"]) and _is_recoverable_format_error(format_error):
            repair_tool = next((item for item in agent.tools if item.name == "repair_data_format"), None)
            if repair_tool is not None:
                repair_result = repair_tool.invoke({})
                recovery_note = f"已执行一次安全格式修复并重试：{repair_result}"
                retry_prompt = agent.prompts["retry_template"].format(
                    execution_prompt=execution_prompt,
                    format_error=format_error[:_FORMAT_ERROR_DISPLAY_MAX_CHARS],
                    repair_result=repair_result,
                )
                retry = agent.react_agent.invoke(
                    {"messages": [*state.get("input_messages", []), HumanMessage(content=retry_prompt)]},
                    config=agent._invoke_config(tool_tracer, recursion_limit=agent.settings.max_iterations * 2 + 5),
                )
                messages = [*messages, *retry["messages"]]
        agent._ensure_not_cancelled()
    except Exception as exc:
        agent.workspace.restore_state(snapshot)
        if isinstance(exc, AnalysisCancelled):
            raise
        error = f"{type(exc).__name__}: {exc}"
        friendly = _humanize_error(exc)
        step_result = {
            **step,
            "status": "failed",
            "summary": f"步骤执行失败：{friendly}。后续将由重规划判断是否需要补偿。",
        }
        return {
            "current_step": step,
            "last_step_result": step_result,
            "trace": [
                *state.get("trace", []),
                {"type": "error", "name": step["id"], "detail": error[:_TRACE_DETAIL_MAX_CHARS]},
            ],
            "artifacts": list(agent.workspace.artifacts),
        }
    last_error_index = max(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, ToolMessage)
            and message.additional_kwargs.get("error_code")
        ),
        default=-1,
    )
    recovered = any(
        isinstance(message, ToolMessage)
        and not message.additional_kwargs.get("error_code")
        for message in messages[last_error_index + 1 :]
    )
    if last_error_index >= 0 and not recovered:
        agent.workspace.restore_state(snapshot)
        error_text = _message_text(messages[last_error_index])
        step_result = {
            **step,
            "status": "failed",
            "summary": f"步骤未完成，已自动回滚数据与产物：{error_text[:1200]}",
        }
        return {
            "current_step": step,
            "last_step_result": step_result,
            "trace": [*state.get("trace", []), *_tool_trace(messages)],
            "artifacts": list(agent.workspace.artifacts),
        }

    final_ai = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)), None
    )
    summary = _message_text(final_ai) or "步骤已执行，但模型未返回文字摘要。"
    if recovery_note:
        summary = f"{summary}\n\n{recovery_note}"
    step_result = {**step, "status": "ok", "summary": summary}
    return {
        "current_step": step,
        "last_step_result": step_result,
        "trace": [*state.get("trace", []), *_tool_trace(messages)],
        "artifacts": list(agent.workspace.artifacts),
    }
