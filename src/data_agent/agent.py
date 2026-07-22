"""Plan-and-Execute + ReAct 双范式数据分析工作流引擎。

本模块实现基于 LangGraph StateGraph 的五节点有向无环工作流：
    validate_dataset → plan_analysis → execute_step ⇄ replan → finalize

核心设计决策：
- 规划器（Planner）使用 LLM structured output 生成 AnalysisPlan，失败时回退到
  内置的 _fallback_plan，确保任何情况下都有可执行步骤。
- 执行器（Executor）是一个 ReAct Agent，每步独立运行并带有 snapshot/rollback
  保护，步骤失败不会污染主数据。
- 重规划器（Replanner）在每步结束后审查进度，可提前终止或补充步骤。
- 取消机制通过 CancelCallback 注入到每次 LLM/Tool 调用，实现亚秒级响应。

线程安全：
    DataAnalysisAgent 实例本身不是线程安全的。API 层通过 run_lock 保证
    同一会话同一时刻只有一个分析在运行。

数据模型、回调处理器和提示词/辅助函数已分别拆分到
``data_agent.models``、``data_agent.callbacks`` 和 ``data_agent.prompts``。
本模块保留工作流引擎主类 ``DataAnalysisAgent`` 及其直接依赖的工具函数。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from threading import Event
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from data_agent.callbacks import (
    CancelCallback,
    ReasoningStreamCallback,
    ReportStreamCallback,
    ToolTraceCallback,
    UsageAccumulator,
)
from data_agent.config import AgentSettings
from data_agent.models import (
    AnalysisCancelled,
    AnalysisPlan,
    AnalysisResult,
    ReplanDecision,
    WorkflowState,
)
from data_agent.prompts import (
    _FINALIZE_EVIDENCE_BUDGET,
    _FINALIZE_PER_STEP_MIN_CHARS,
    _FORMAT_ERROR_DISPLAY_MAX_CHARS,
    _PLAN_PROFILE_MAX_CHARS,
    _REPLAN_PAYLOAD_MAX_CHARS,
    _TRACE_DETAIL_MAX_CHARS,
    SYSTEM_PROMPT,
    _apply_query_constraints,
    _fallback_plan,
    _handle_tool_error,
    _humanize_error,
    _is_recoverable_format_error,
    _query_allows_format_repair,
)
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

logger = logging.getLogger(__name__)


def create_chat_model(settings: AgentSettings) -> BaseChatModel:
    """根据配置创建提供商原生的 Chat Model 实例。

    使用提供商原生 SDK（ChatDeepSeek / ChatOpenAI）而非通用 ChatLiteLLM，
    以确保 tool calling 的可靠性和 streaming 兼容性。

    Args:
        settings: 已验证的运行时配置。

    Returns:
        绑定了 API Key、超时和重试策略的 BaseChatModel 实例。

    Raises:
        ValueError: 当 provider/api_key/model 配置不合法时。
    """
    settings.validate_for_model()
    if settings.provider == "deepseek":
        deepseek_args: dict[str, Any] = {
            "model": settings.model,
            "api_key": settings.api_key,
            "api_base": settings.base_url,
            "timeout": settings.timeout_seconds,
            "max_retries": 2,
            "extra_body": {
                "thinking": {"type": "enabled" if settings.thinking_enabled else "disabled"}
            },
        }
        if settings.thinking_enabled:
            deepseek_args["reasoning_effort"] = settings.reasoning_effort
        else:
            deepseek_args["temperature"] = settings.temperature
        return ChatDeepSeek(**deepseek_args)
    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=settings.temperature,
        timeout=settings.timeout_seconds,
        max_retries=2,
    )


def _message_text(message: BaseMessage | None) -> str:
    """从 LangChain 消息对象中提取纯文本内容。

    兼容 str 和 list[dict] 两种 content 格式（后者出现在多模态/
    thinking-mode 响应中）。返回空字符串表示无有效文本。
    """
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            parts.append(str(item.get("text", "")))
    return "\n".join(part for part in parts if part)


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


class DataAnalysisAgent:
    """Plan-and-Execute LangGraph 工作流，内嵌 ReAct 执行器。

    生命周期：
        1. 构造时绑定 workspace、settings、model 和 tools。
        2. 调用 run() 或 stream() 启动一次完整分析。
        3. 分析结束后通过 AnalysisResult 获取报告和产物。

    取消语义：
        外部通过 cancel_event.set() 请求取消；CancelCallback 在每次
        LLM/Tool 调用入口检查 event，节点边界也会检查。取消后抛出
        AnalysisCancelled，workspace 自动回滚到步骤开始前的快照。
    """

    def __init__(
        self,
        workspace: DataWorkspace,
        settings: AgentSettings | None = None,
        model: BaseChatModel | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[str, str], None] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.settings = settings or AgentSettings.from_env()
        self.model = model or create_chat_model(self.settings)
        self.cancel_event = cancel_event or Event()
        self.cancel_callback = CancelCallback(self.cancel_event)
        self.progress_callback = progress_callback or (lambda node, title: None)
        # event_callback: 通用事件通道，用于推送 report_chunk/tool_call/tool_result
        # 等细粒度事件。与 progress_callback(node, title) 并存，后者保持后向兼容。
        self.event_callback = event_callback or (lambda event_type, payload: None)
        self.tools = build_tools(workspace)
        self.react_agent = create_agent(
            model=self.model,
            tools=self.tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=[_handle_tool_error],
            name="data_analysis_react_executor",
        )
        self.planner = self.model.with_structured_output(AnalysisPlan)
        self.replanner = self.model.with_structured_output(ReplanDecision)
        self.graph = self._build_workflow()
        # 上一次 chat() 调用累计的 token 用量与思考过程，供 API 层在 chat_done
        # 事件中读取。每次 chat() 调用会覆盖。stream()/run() 不使用这两个属性
        # （其用量通过 AnalysisResult 返回）。
        self._last_usage: dict[str, int] | None = None
        self._last_reasoning: str = ""

    def _invoke_config(self, *extra_callbacks: BaseCallbackHandler, **extra: Any) -> dict[str, Any]:
        """Build a RunnableConfig that wires the cancel callback into every
        LLM/tool call inside a node so cancellation takes effect promptly.

        Args:
            *extra_callbacks: 额外的 callback handler（如 ReportStreamCallback、
                ToolTraceCallback），会与 cancel_callback 一起注入。
            **extra: 其他 RunnableConfig 字段。
        """
        callbacks: list[BaseCallbackHandler] = [self.cancel_callback, *extra_callbacks]
        return {"callbacks": callbacks, **extra}

    def _enter_node(self, node: str, title: str) -> None:
        """Emit a progress signal at node entry so the SSE stream can surface
        "正在检查数据" / "正在规划" etc. before the (potentially slow) LLM call
        inside the node returns."""
        try:
            self.progress_callback(node, title)
        except Exception:
            # Progress is best-effort; never let it break the workflow.
            pass

    def _build_workflow(self):
        def ensure_not_cancelled() -> None:
            if self.cancel_event.is_set():
                raise AnalysisCancelled("分析已取消。")

        def validate_dataset(state: WorkflowState) -> dict[str, Any]:
            ensure_not_cancelled()
            self._enter_node("validate_dataset", "正在检查数据集结构")
            profile = self.workspace.profile(sample_rows=5)
            messages = list(state.get("input_messages", []))
            if not messages:
                messages = [HumanMessage(content=state["query"])]
            # 断点续跑：保留已有的 completed_steps / plan / remaining_steps / objective，
            # 否则会被覆盖为空导致 plan_analysis 重新规划。
            return {
                "dataset_profile": profile,
                "input_messages": messages,
                "trace": list(state.get("trace", [])),
                "artifacts": list(self.workspace.artifacts),
                "completed_steps": list(state.get("completed_steps", [])),
                "plan": list(state.get("plan", [])),
                "remaining_steps": list(state.get("remaining_steps", [])),
                "objective": state.get("objective", ""),
            }

        def plan_analysis(state: WorkflowState) -> dict[str, Any]:
            ensure_not_cancelled()
            # 断点续跑：如果已有 plan（从 resume_from 注入），跳过 LLM 规划直接复用。
            # 这样 execute_step 会从 remaining_steps 的第一项开始，跳过已完成的步骤。
            existing_plan = state.get("plan") or []
            existing_completed = state.get("completed_steps") or []
            if existing_plan and existing_completed:
                completed_ids = {item.get("id") for item in existing_completed if item.get("id")}
                remaining = [step for step in existing_plan if step.get("id") not in completed_ids]
                self._enter_node("plan_analysis", "正在恢复分析进度")
                return {
                    "objective": state.get("objective") or state["query"],
                    "plan": existing_plan,
                    "remaining_steps": remaining,
                }
            self._enter_node("plan_analysis", "正在规划分析步骤")
            profile_text = json.dumps(state["dataset_profile"], ensure_ascii=False)[:_PLAN_PROFILE_MAX_CHARS]
            prompt = (
                "为数据分析任务制定 2 到 6 个可执行步骤。第一步必须检查数据，最后应包含必要的图表和导出。"
                "不要写空泛步骤，每步都要能由数据工具完成。\n"
                "步骤设计原则：\n"
                "- 检查步骤要具体指出需要关注的字段和质量问题\n"
                "- 统计步骤要明确方法（如相关、回归、分组对比、分布检验）\n"
                "- 图表步骤要指定图表类型和展示维度\n"
                "- 避免重复步骤，每步应有独立价值\n\n"
                f"用户目标：{state['query']}\n数据概况：{profile_text}"
            )
            try:
                plan = self.planner.invoke(prompt, config=self._invoke_config())
                if not isinstance(plan, AnalysisPlan):
                    plan = AnalysisPlan.model_validate(plan)
            except Exception:
                plan = _fallback_plan(state["query"])
            plan = _apply_query_constraints(state["query"], plan)
            steps = [step.model_dump() for step in plan.steps[: self.settings.max_plan_steps]]
            return {"objective": plan.objective, "plan": steps, "remaining_steps": steps}

        def execute_step(state: WorkflowState) -> dict[str, Any]:
            ensure_not_cancelled()
            remaining = list(state.get("remaining_steps", []))
            if not remaining:
                return {"current_step": {}, "last_step_result": {}}
            step = remaining[0]
            step_index = len(state.get("completed_steps", [])) + 1
            total_steps = step_index + len(remaining) - 1
            self._enter_node(
                "execute_step",
                f"正在执行 ({step_index}/{total_steps})：{step.get('title', step.get('id', '未知步骤'))}",
            )
            completed = state.get("completed_steps", [])
            completed_text = "\n".join(
                f"- {item['title']}: {item.get('summary', '')[:800]}" for item in completed
            ) or "尚无"
            # 增强上下文：把数据概况传递给 ReAct 执行器，避免每步都重新 inspect_data。
            profile_brief = json.dumps(
                {
                    "rows": state.get("dataset_profile", {}).get("rows"),
                    "columns": state.get("dataset_profile", {}).get("columns"),
                    "column_names": [
                        col["name"] for col in state.get("dataset_profile", {}).get("column_info", [])[:15]
                    ],
                },
                ensure_ascii=False,
            )
            execution_prompt = (
                f"总目标：{state['objective']}\n"
                f"当前计划步骤：{step['title']}\n"
                f"具体任务：{step['instruction']}\n"
                f"完成标准：{step['success_criteria']}\n"
                f"数据概况：{profile_brief}\n"
                f"已完成步骤：\n{completed_text}\n\n"
                "只执行当前步骤。使用工具获得证据，然后用简短文字报告实际结果。"
            )
            messages: list[BaseMessage] = []
            recovery_note = ""
            snapshot = self.workspace.snapshot_state()
            # 工具追踪：ToolTraceCallback 把 ReAct 循环内每次工具调用实时
            # 推送到前端，让用户看到"正在读取数据→正在清洗→正在生成图表"，
            # 而不是只看到"正在执行 (2/4)"一行字等 30 秒。
            tool_tracer = ToolTraceCallback(self.event_callback)
            try:
                result = self.react_agent.invoke(
                    {"messages": [*state.get("input_messages", []), HumanMessage(content=execution_prompt)]},
                    config=self._invoke_config(tool_tracer, recursion_limit=self.settings.max_iterations * 2 + 5),
                )
                messages = result["messages"]
                format_error = _format_error_text(messages)
                if format_error and _query_allows_format_repair(state["query"]) and _is_recoverable_format_error(format_error):
                    repair_tool = next((item for item in self.tools if item.name == "repair_data_format"), None)
                    if repair_tool is not None:
                        repair_result = repair_tool.invoke({})
                        recovery_note = f"已执行一次安全格式修复并重试：{repair_result}"
                        retry_prompt = (
                            f"{execution_prompt}\n\n上一次工具调用失败：{format_error[:_FORMAT_ERROR_DISPLAY_MAX_CHARS]}\n"
                            f"自动修复结果：{repair_result}\n请只重试当前步骤，不要扩大任务范围。"
                        )
                        retry = self.react_agent.invoke(
                            {"messages": [*state.get("input_messages", []), HumanMessage(content=retry_prompt)]},
                            config=self._invoke_config(tool_tracer, recursion_limit=self.settings.max_iterations * 2 + 5),
                        )
                        messages = [*messages, *retry["messages"]]
                ensure_not_cancelled()
            except Exception as exc:
                self.workspace.restore_state(snapshot)
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
                    "artifacts": list(self.workspace.artifacts),
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
                self.workspace.restore_state(snapshot)
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
                    "artifacts": list(self.workspace.artifacts),
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
                "artifacts": list(self.workspace.artifacts),
            }

        def replan(state: WorkflowState) -> dict[str, Any]:
            ensure_not_cancelled()
            self._enter_node("replan", "正在审查进度并重规划")
            current = state.get("last_step_result", {})
            completed = [*state.get("completed_steps", [])]
            if current:
                completed.append(current)
            original_remaining = list(state.get("remaining_steps", []))[1:]
            if len(completed) >= self.settings.max_plan_steps:
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
                decision = self.replanner.invoke(
                    "审查数据分析计划的执行进度。根据已经获得的证据判断是否可以结束；"
                    "否则只返回仍然必要的后续步骤，删除重复或没有价值的步骤。\n"
                    + json.dumps(review_payload, ensure_ascii=False)[:_REPLAN_PAYLOAD_MAX_CHARS],
                    config=self._invoke_config(),
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
                    ][: max(self.settings.max_plan_steps - len(completed), 0)]
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

        def route_after_review(state: WorkflowState) -> str:
            return "execute_step" if state.get("remaining_steps") else "finalize"

        def finalize(state: WorkflowState) -> dict[str, Any]:
            ensure_not_cancelled()
            self._enter_node("finalize", "正在汇总最终报告")
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
            prompt = (
                "请基于以下工具执行结果，写一份给业务负责人看的中文数据分析报告。"
                "读者不懂统计、时间有限，只想快速知道三件事：结论是什么、凭什么、接下来怎么办。\n\n"
                "硬性要求：\n"
                "1. 只能引用执行结果中实际出现的数字和文件，不得编造数据或未验证的推断；\n"
                "2. 全文 1200-2000 字，信息密度优先，禁止“通过分析可知”“综上所述”这类空话套话；\n"
                "3. 按以下章节组织，每节用二级标题（## ）：\n"
                "   - ## 结论速览：3-5 条要点，第一条必须直接回答用户的分析目标；每条一个核心判断，"
                "关键数字 **加粗**，让读者 30 秒看懂全局；\n"
                "   - ## 关键发现：按业务价值从高到低排序，每条发现按三步写——先一句话说清发现了什么，"
                "再列出支撑它的具体数字，最后补一句这对业务意味着什么；\n"
                "   - ## 数据与处理：简要说明数据规模、质量问题和做过的清洗动作（前后行数/缺失变化）；\n"
                "   - ## 图表与产物：逐个说明生成的图表应该看什么、得出什么印象，并提及可下载的数据文件；\n"
                "   - ## 注意事项与建议：指出数据或方法的局限性，再给 2-3 条具体可执行的下一步建议。\n"
                "4. 引用统计指标时必须当场用大白话解释，例如写“差异显著（p=0.001，即这种差距只有 "
                "0.1% 的可能是随机巧合）”，不允许只堆术语不解释；\n"
                "5. 数字对比尽量换算成读者有感的形式（倍数、百分比、排名），而不是只列原始值；\n"
                "6. 如需表格，使用 Markdown 表格语法，列数不超过 5 列，表头用业务语言而非字段名。\n\n"
                f"用户目标：{state['query']}\n\n执行结果：\n{evidence}"
            )
            try:
                # 流式报告：ReportStreamCallback 把每个 token 通过 event_callback
                # 推送到前端，用户看着报告逐字写出，而不是等 30-60 秒看完整报告。
                # ReasoningStreamCallback 把 DeepSeek reasoning_content 以 thinking_chunk
                # 事件实时推送，让用户看到 Agent 的思考过程。
                # UsageAccumulator 累计 finalize 节点的 token 用量。
                reasoning_buffer: list[str] = []
                report_streamer = ReportStreamCallback(self.event_callback)
                reasoning_streamer = ReasoningStreamCallback(
                    self.event_callback, buffer=reasoning_buffer
                )
                usage_acc = UsageAccumulator()
                final_message = self.model.invoke(
                    prompt,
                    config=self._invoke_config(report_streamer, reasoning_streamer, usage_acc),
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
                "artifacts": list(self.workspace.artifacts),
                "dataset_profile": self.workspace.profile(sample_rows=5),
                "plan": state.get("plan", []),
                "completed_steps": state.get("completed_steps", []),
                "trace": state.get("trace", []),
                "usage": usage_acc.snapshot() if usage_acc else None,
                "reasoning": "".join(reasoning_buffer),
            }

        graph = StateGraph(WorkflowState)
        graph.add_node("validate_dataset", validate_dataset)
        graph.add_node("plan_analysis", plan_analysis)
        graph.add_node("execute_step", execute_step)
        graph.add_node("replan", replan)
        graph.add_node("finalize", finalize)
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

    def _input_state(
        self,
        query: str,
        history: list[BaseMessage] | None = None,
        resume_from: dict[str, Any] | None = None,
    ) -> WorkflowState:
        """构建工作流初始状态。

        Args:
            query: 用户的分析任务描述。
            history: 可选的多轮对话历史。
            resume_from: 断点续跑的恢复点，包含 ``plan`` 和 ``completed_steps``。
                提供时，``plan_analysis`` 节点会跳过 LLM 规划直接复用已有计划，
                ``execute_step`` 会跳过已完成的步骤，从中断处继续。
        """
        if not query.strip():
            raise ValueError("分析任务不能为空。")
        messages = list(history or [])
        messages.append(HumanMessage(content=query.strip()))
        state: WorkflowState = {"query": query.strip(), "input_messages": messages}
        if resume_from:
            # 注入已有计划与完成步骤，plan_analysis 和 execute_step 会据此跳过
            existing_plan = resume_from.get("plan") or []
            completed = resume_from.get("completed_steps") or []
            completed_ids = {item.get("id") for item in completed if item.get("id")}
            remaining = [step for step in existing_plan if step.get("id") not in completed_ids]
            state["plan"] = existing_plan
            state["completed_steps"] = completed
            state["remaining_steps"] = remaining
            state["objective"] = resume_from.get("objective") or query.strip()
        return state

    def run(
        self,
        query: str,
        history: list[BaseMessage] | None = None,
        resume_from: dict[str, Any] | None = None,
    ) -> AnalysisResult:
        """同步执行完整分析流程并返回最终结果。

        Args:
            query: 用户的分析任务描述（中文）。
            history: 可选的多轮对话历史，用于上下文续接。
            resume_from: 断点续跑的恢复点，包含 ``plan`` 和 ``completed_steps``。
                提供时跳过已完成步骤，从中断处继续。

        Returns:
            包含报告、轨迹、产物和计划执行情况的 AnalysisResult。

        Raises:
            ValueError: query 为空时。
            AnalysisCancelled: 外部请求取消时。
        """
        result = self.graph.invoke(
            self._input_state(query, history, resume_from=resume_from),
            config={
                "configurable": {"thread_id": uuid4().hex},
                "recursion_limit": self.settings.max_plan_steps * 3 + 10,
            },
        )
        return AnalysisResult(
            response=result["response"],
            trace=result.get("trace", []),
            artifacts=result.get("artifacts", []),
            dataset_profile=result["dataset_profile"],
            plan=result.get("plan", []),
            completed_steps=result.get("completed_steps", []),
            usage=result.get("usage"),
            reasoning=result.get("reasoning", ""),
        )

    def stream(
        self,
        query: str,
        history: list[BaseMessage] | None = None,
        resume_from: dict[str, Any] | None = None,
        plan_only: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """流式执行分析，逐节点 yield 中间状态更新。

        每次 yield 一个 ``{"node": <节点名>, "data": <状态增量>}`` 字典，
        API 层将其转换为 SSE 事件推送给前端。

        Args:
            query: 用户的分析任务描述。
            history: 可选的多轮对话历史。
            resume_from: 断点续跑的恢复点，包含 ``plan`` 和 ``completed_steps``。
            plan_only: 仅规划模式。为 True 时在 yield 出 ``plan_analysis``
                节点后立即停止，不进入 execute_step/finalize。用于"规划-
                审批-执行"工作流：先展示计划等待用户确认，再通过
                ``resume_from`` 注入已确认的计划启动执行。

        Yields:
            包含节点名和状态增量的字典。
        """
        config = {
            "configurable": {"thread_id": uuid4().hex},
            "recursion_limit": self.settings.max_plan_steps * 3 + 10,
        }
        for update in self.graph.stream(
            self._input_state(query, history, resume_from=resume_from), config=config
        ):
            node, payload = next(iter(update.items()))
            yield {"node": node, "data": payload}
            # 仅规划模式：在 plan_analysis 节点输出后立即终止迭代，
            # 不进入 execute_step/finalize。调用方（API 层）从 payload 中
            # 提取 plan/objective 推送给前端等待用户审批。
            if plan_only and node == "plan_analysis":
                return

    def chat(
        self,
        query: str,
        history: list[BaseMessage] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """轻量追问：不走 plan-and-execute 工作流，直接用 ReAct 执行器回答。

        用于多轮对话中的快速追问场景（如"把刚才那张图改成红色"、
        "再分析一下年龄分布"、"解释一下这个相关系数"），避免每次追问都触发
        完整的 plan→execute→finalize 流程（动辄 30-60 秒）。

        回答过程中的 token 通过 event_callback 以 ``chat_chunk`` 事件实时推送，
        工具调用通过 ``tool_call``/``tool_result`` 事件推送，前端渲染为对话气泡 +
        工具时间线，体验与 ChatGPT/Claude 追问一致。

        思考过程通过 ``thinking_chunk`` 事件实时推送，token 用量累计到
        ``self._last_usage``，思考过程文本累计到 ``self._last_reasoning``，
        供 API 层在 ``chat_done`` 事件中读取。

        Args:
            query: 用户的追问内容。
            history: 之前的对话历史，用于上下文续接。

        Returns:
            ``(response_text, new_artifacts)`` 元组。``new_artifacts`` 是本次
            追问期间通过工具调用新生成的产物（图表、导出数据等）元数据。
            调用后可通过 ``self._last_usage`` 和 ``self._last_reasoning``
            获取本次追问的 token 用量和思考过程。

        Raises:
            ValueError: query 为空时。
            AnalysisCancelled: 外部请求取消时。
        """
        if not query.strip():
            raise ValueError("追问不能为空。")
        messages = list(history or [])
        messages.append(HumanMessage(content=query.strip()))

        # 记录追问前的产物数量，事后 diff 出本次新增的产物。
        artifact_before = len(self.workspace._artifacts)
        tool_tracer = ToolTraceCallback(self.event_callback)
        chat_streamer = ReportStreamCallback(self.event_callback, event_type="chat_chunk")
        # 思考过程 + 用量：与 finalize 节点一致的回调注入，让追问也具备
        # reasoning_content 流式展示和 token 用量统计。
        reasoning_buffer: list[str] = []
        reasoning_streamer = ReasoningStreamCallback(
            self.event_callback, buffer=reasoning_buffer
        )
        usage_acc = UsageAccumulator()

        result = self.react_agent.invoke(
            {"messages": messages},
            config=self._invoke_config(
                tool_tracer,
                chat_streamer,
                reasoning_streamer,
                usage_acc,
                recursion_limit=self.settings.max_iterations * 2 + 5,
            ),
        )
        final_ai = next(
            (message for message in reversed(result["messages"]) if isinstance(message, AIMessage)),
            None,
        )
        response_text = _message_text(final_ai) or "（未生成回复）"
        new_artifacts = list(self.workspace.artifacts)[artifact_before:]
        # 存储到实例属性，供 API 层在 chat_done 事件中读取
        self._last_usage = usage_acc.snapshot()
        self._last_reasoning = "".join(reasoning_buffer)
        return response_text, new_artifacts
