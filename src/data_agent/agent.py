from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Event
from typing import Any, TypedDict
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from data_agent.config import AgentSettings
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

SYSTEM_PROMPT = """你是一名严谨、主动的数据分析专家。你通过 ReAct 循环选择下一项最小必要行动，
并且只能使用提供的工具读取和变更数据。

工作规范：
1. 不得猜测数据结构；进行清洗、统计或绘图前必须确认字段、类型和缺失情况。
2. 如果工具因列类型、日期格式、数值格式或编码问题失败，先检查错误，再调用 repair_data_format，修复后重试原操作一次。
3. repair_data_format 只允许修复明确的格式问题；不得把负数、离群值、重复记录或业务缺失值擅自改掉。
4. 清洗必须采用保守策略，说明处理前后的行数、缺失值和异常值变化。
5. 统计结论给出样本量、指标、适用时的 p 值与显著性；相关不等于因果。
6. 图表必须匹配变量类型并使用清晰标题；复杂关系优先使用热力图或关系图。若极端值会压缩主体数据，必须使用 create_visualization 的默认 auto 尺度生成“主体尺度/全量视图”切换，不得交付正常点全部挤在零线上的图，也不得为了好看擅自删除异常值。分组图缺少某些类别组合时，必须保留工具生成的“无样本/无记录”说明，不能把缺失组合解释成数值 0 或渲染失败。
7. 只能引用工具实际返回的数字和文件，不得编造结果。
8. 不展示隐藏的内部推理，只简要说明已执行的动作和可验证结果。
9. 当前只完成计划中指定的步骤，不要擅自重复已经完成的工作。
10. transform_data 只生成派生视图，不会改变主数据；不得把筛选视图当作最终清洗数据导出。
"""


class PlanStep(BaseModel):
    id: str = Field(description="Stable short step id, for example inspect or visualize")
    title: str = Field(description="Short Chinese title shown to the user")
    instruction: str = Field(description="Concrete instruction for the ReAct executor")
    success_criteria: str = Field(description="Observable completion criteria")


class AnalysisPlan(BaseModel):
    objective: str = Field(description="One-sentence analysis objective")
    steps: list[PlanStep] = Field(min_length=1, max_length=8)


class ReplanDecision(BaseModel):
    done: bool = Field(description="Whether enough evidence exists to finish")
    rationale: str = Field(description="Brief reason without hidden chain of thought")
    remaining_steps: list[PlanStep] = Field(default_factory=list, max_length=8)


class WorkflowState(TypedDict, total=False):
    query: str
    input_messages: list[BaseMessage]
    dataset_profile: dict[str, Any]
    objective: str
    plan: list[dict[str, str]]
    remaining_steps: list[dict[str, str]]
    current_step: dict[str, str]
    last_step_result: dict[str, Any]
    completed_steps: list[dict[str, Any]]
    response: str
    trace: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    replan_reason: str


@dataclass(slots=True)
class AnalysisResult:
    response: str
    trace: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    dataset_profile: dict[str, Any]
    plan: list[dict[str, str]]
    completed_steps: list[dict[str, Any]]


class AnalysisCancelled(RuntimeError):
    """Raised between workflow nodes when the user cancels an analysis."""


class CancelCallback(BaseCallbackHandler):
    """LangChain callback that aborts long-running LLM/tool calls on cancel.

    The workflow only checks ``cancel_event`` at node boundaries, so a single
    DeepSeek thinking-mode call (60+ s) or a 25-iteration ReAct loop would
    otherwise keep running for minutes after the user clicks Cancel. This
    handler raises :class:`AnalysisCancelled` from ``on_llm_start`` /
    ``on_tool_start`` / ``on_chat_model_start`` so the cancellation takes
    effect within one LLM call instead of one node.
    """

    def __init__(self, cancel_event: Event) -> None:
        self.cancel_event = cancel_event

    def _ensure(self) -> None:
        if self.cancel_event.is_set():
            raise AnalysisCancelled("分析已取消。")

    def on_llm_start(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure()

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure()

    def on_tool_start(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure()


def create_chat_model(settings: AgentSettings) -> BaseChatModel:
    """Create the provider-native chat model required for reliable tool calling."""
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


def _fallback_plan(query: str) -> AnalysisPlan:
    """Build a default plan; query constraints are applied by the caller."""
    return AnalysisPlan(
        objective=f"基于当前数据集完成可验证的分析：{query}",
        steps=[
            PlanStep(
                id="inspect",
                title="检查数据质量",
                instruction="检查字段、类型、缺失、重复和样例，指出最重要的数据质量问题。",
                success_criteria="返回数据规模、字段类型、缺失和重复情况。",
            ),
            PlanStep(
                id="prepare",
                title="准备分析数据",
                instruction="根据已发现的问题采用保守策略完成必要清洗，并保存清洗结果。",
                success_criteria="说明处理动作和前后数据变化，生成清洗数据产物。",
            ),
            PlanStep(
                id="analyze",
                title="执行统计分析",
                instruction=f"围绕用户目标执行描述统计、关系分析和适用的统计检验：{query}",
                success_criteria="给出样本量、关键指标及适用时的显著性或模型指标。",
            ),
            PlanStep(
                id="visualize",
                title="生成图表与导出",
                instruction="创建最有解释力的图表并导出当前分析数据。",
                success_criteria="至少生成一个可读的交互图表和一个数据文件产物；存在极端值时图表须同时保留主体尺度与全量视图。",
            ),
        ],
    )


def _apply_query_constraints(query: str, plan: AnalysisPlan) -> AnalysisPlan:
    """Keep explicit user constraints intact even when structured planning falls back."""
    read_only = any(
        token in query
        for token in (
            "不修改", "无需修改", "只检查", "仅检查", "不要改动", "不要修改",
            "保持原样", "只读", "只看",
        )
    )
    no_charts = any(
        token in query
        for token in (
            "不生成图表", "不要图表", "不画图", "无需绘图", "不用画图", "不用绘图",
            "不需要图表", "不需要可视化", "不用可视化", "无需图表", "不要可视化",
        )
    )
    inspect_only = any(token in query for token in ("只检查", "仅检查")) and "质量" in query
    steps = list(plan.steps)
    if inspect_only:
        steps = [step for step in steps if step.id == "inspect"]
    if read_only:
        steps = [
            step
            for step in steps
            if step.id not in {"prepare", "clean", "transform", "export"}
            and not any(word in f"{step.title}{step.instruction}" for word in ("清洗", "转换", "导出"))
        ]
    if no_charts:
        steps = [
            step
            for step in steps
            if step.id not in {"visualize", "chart", "plot"}
            and not any(word in f"{step.title}{step.instruction}" for word in ("图表", "绘图", "可视化"))
        ]
    if not steps:
        steps = [
            PlanStep(
                id="inspect",
                title="检查数据质量",
                instruction="只读取数据并检查字段、类型、缺失、重复和样例，不修改任何数据。",
                success_criteria="返回数据规模、字段类型、缺失和重复情况。",
            )
        ]
    return AnalysisPlan(objective=plan.objective, steps=steps)


def _tool_trace(messages: list[BaseMessage]) -> list[dict[str, str]]:
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
                    "detail": _message_text(message)[:4000],
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


def _is_recoverable_format_error(text: str) -> bool:
    markers = (
        "dtype",
        "datetime",
        "could not convert",
        "not numeric",
        "不是数值列",
        "无法转换",
        "unable to parse",
        "time data",
        "日期格式",
        "数值格式",
        "编码",
    )
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in markers)


def _query_allows_format_repair(query: str) -> bool:
    return not any(token in query for token in ("不修改", "无需修改", "只检查", "仅检查"))


@wrap_tool_call
def _handle_tool_error(request: Any, handler: Any) -> ToolMessage:
    try:
        return handler(request)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        error_code = "format_error" if _is_recoverable_format_error(detail) else "tool_error"
        return ToolMessage(
            content=(
                f"工具执行失败：{detail}。"
                "如果原因与列类型、日期或数值格式有关，请先调用 repair_data_format，"
                "再用修正后的列名和参数重试；如果是业务数据异常，不要擅自修改。"
            ),
            tool_call_id=request.tool_call["id"],
            additional_kwargs={"error_code": error_code},
        )


class DataAnalysisAgent:
    """Plan-and-Execute LangGraph workflow with a ReAct executor."""

    def __init__(
        self,
        workspace: DataWorkspace,
        settings: AgentSettings | None = None,
        model: BaseChatModel | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.settings = settings or AgentSettings.from_env()
        self.model = model or create_chat_model(self.settings)
        self.cancel_event = cancel_event or Event()
        self.cancel_callback = CancelCallback(self.cancel_event)
        self.progress_callback = progress_callback or (lambda node, title: None)
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

    def _invoke_config(self, **extra: Any) -> dict[str, Any]:
        """Build a RunnableConfig that wires the cancel callback into every
        LLM/tool call inside a node so cancellation takes effect promptly."""
        return {"callbacks": [self.cancel_callback], **extra}

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
            return {
                "dataset_profile": profile,
                "input_messages": messages,
                "trace": list(state.get("trace", [])),
                "artifacts": list(self.workspace.artifacts),
                "completed_steps": [],
            }

        def plan_analysis(state: WorkflowState) -> dict[str, Any]:
            ensure_not_cancelled()
            self._enter_node("plan_analysis", "正在规划分析步骤")
            profile_text = json.dumps(state["dataset_profile"], ensure_ascii=False)[:12000]
            prompt = (
                "为数据分析任务制定 2 到 6 个可执行步骤。第一步必须检查数据，最后应包含必要的图表和导出。"
                "不要写空泛步骤，每步都要能由数据工具完成。\n\n"
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
            self._enter_node("execute_step", f"正在执行：{step.get('title', step.get('id', '未知步骤'))}")
            completed = state.get("completed_steps", [])
            completed_text = "\n".join(
                f"- {item['title']}: {item.get('summary', '')[:800]}" for item in completed
            ) or "尚无"
            execution_prompt = (
                f"总目标：{state['objective']}\n"
                f"当前计划步骤：{step['title']}\n"
                f"具体任务：{step['instruction']}\n"
                f"完成标准：{step['success_criteria']}\n"
                f"已完成步骤：\n{completed_text}\n\n"
                "只执行当前步骤。使用工具获得证据，然后用简短文字报告实际结果。"
            )
            messages: list[BaseMessage] = []
            recovery_note = ""
            snapshot = self.workspace.snapshot_state()
            try:
                result = self.react_agent.invoke(
                    {"messages": [*state.get("input_messages", []), HumanMessage(content=execution_prompt)]},
                    config=self._invoke_config(recursion_limit=self.settings.max_iterations * 2 + 5),
                )
                messages = result["messages"]
                format_error = _format_error_text(messages)
                if format_error and _query_allows_format_repair(state["query"]) and _is_recoverable_format_error(format_error):
                    repair_tool = next((item for item in self.tools if item.name == "repair_data_format"), None)
                    if repair_tool is not None:
                        repair_result = repair_tool.invoke({})
                        recovery_note = f"已执行一次安全格式修复并重试：{repair_result}"
                        retry_prompt = (
                            f"{execution_prompt}\n\n上一次工具调用失败：{format_error[:3000]}\n"
                            f"自动修复结果：{repair_result}\n请只重试当前步骤，不要扩大任务范围。"
                        )
                        retry = self.react_agent.invoke(
                            {"messages": [*state.get("input_messages", []), HumanMessage(content=retry_prompt)]},
                            config=self._invoke_config(recursion_limit=self.settings.max_iterations * 2 + 5),
                        )
                        messages = [*messages, *retry["messages"]]
                ensure_not_cancelled()
            except Exception as exc:
                self.workspace.restore_state(snapshot)
                if isinstance(exc, AnalysisCancelled):
                    raise
                error = f"{type(exc).__name__}: {exc}"
                step_result = {
                    **step,
                    "status": "failed",
                    "summary": f"步骤执行失败：{error}。后续将由重规划判断是否需要补偿。",
                }
                return {
                    "current_step": step,
                    "last_step_result": step_result,
                    "trace": [
                        *state.get("trace", []),
                        {"type": "error", "name": step["id"], "detail": error[:4000]},
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
                    + json.dumps(review_payload, ensure_ascii=False)[:16000],
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
            total_budget = 30000
            per_step_min = 800
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

            prompt = (
                "请基于以下工具执行结果生成最终中文数据分析报告。要求：\n"
                "1. 不得新增不存在的数字或推断未提供的结论；\n"
                "2. 全文控制在 1500-2500 字之间，避免过长或过短；\n"
                "3. 结构按以下章节组织，每节用二级标题（## ）：数据质量、处理动作、关键发现、"
                "统计解释、图表与产物、局限与建议；\n"
                "4. 关键数字用 **加粗** 标注，便于快速定位；\n"
                "5. 如有表格，使用 Markdown 表格语法，列数不超过 5 列。\n\n"
                f"用户目标：{state['query']}\n\n执行结果：\n{evidence}"
            )
            try:
                final_message = self.model.invoke(prompt, config=self._invoke_config())
                response = _message_text(final_message)
            except Exception:
                response = ""
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
        self, query: str, history: list[BaseMessage] | None = None
    ) -> WorkflowState:
        if not query.strip():
            raise ValueError("分析任务不能为空。")
        messages = list(history or [])
        messages.append(HumanMessage(content=query.strip()))
        return {"query": query.strip(), "input_messages": messages}

    def run(self, query: str, history: list[BaseMessage] | None = None) -> AnalysisResult:
        result = self.graph.invoke(
            self._input_state(query, history),
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
        )

    def stream(
        self, query: str, history: list[BaseMessage] | None = None
    ) -> Iterator[dict[str, Any]]:
        config = {
            "configurable": {"thread_id": uuid4().hex},
            "recursion_limit": self.settings.max_plan_steps * 3 + 10,
        }
        for update in self.graph.stream(self._input_state(query, history), config=config):
            node, payload = next(iter(update.items()))
            yield {"node": node, "data": payload}
