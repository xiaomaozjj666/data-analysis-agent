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
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 命名常量：消除散落在各节点中的魔法数字
# ---------------------------------------------------------------------------

#: 工具调用结果写入 trace 时的最大字符数，防止单条 trace 膨胀到 MB 级。
_TRACE_DETAIL_MAX_CHARS = 4_000

#: 规划提示词中数据概况的最大字符数，避免超大 profile 撑爆 context window。
_PLAN_PROFILE_MAX_CHARS = 12_000

#: 重规划审查载荷的最大字符数。
_REPLAN_PAYLOAD_MAX_CHARS = 16_000

#: 最终报告汇总时所有步骤 summary 的总字符预算。
_FINALIZE_EVIDENCE_BUDGET = 30_000

#: 每步 summary 在 finalize 中至少保留的字符数。
_FINALIZE_PER_STEP_MIN_CHARS = 800

#: 格式修复重试时展示给 LLM 的错误文本最大字符数。
_FORMAT_ERROR_DISPLAY_MAX_CHARS = 3_000

SYSTEM_PROMPT = """你是一名严谨、主动的数据分析专家。你通过 ReAct 循环选择下一项最小必要行动，
并且只能使用提供的工具读取和变更数据。

工作规范：
1. 不得猜测数据结构；进行清洗、统计或绘图前必须确认字段、类型和缺失情况。
2. 如果工具因列类型、日期格式、数值格式或编码问题失败，先检查错误，再调用 repair_data_format，修复后重试原操作一次。
3. repair_data_format 只允许修复明确的格式问题；不得把负数、离群值、重复记录或业务缺失值擅自改掉。
4. 清洗必须采用保守策略，说明处理前后的行数、缺失值和异常值变化。
5. 统计结论给出样本量、指标、适用时的 p 值、效应量与显著性；相关不等于因果。
6. 图表必须匹配变量类型并使用清晰标题；复杂关系优先使用热力图或关系图。若极端值会压缩主体数据，必须使用 create_visualization 的默认 auto 尺度生成“主体尺度/全量视图”切换，不得交付正常点全部挤在零线上的图，也不得为了好看擅自删除异常值。分组图缺少某些类别组合时，必须保留工具生成的“无样本/无记录”说明，不能把缺失组合解释成数值 0 或渲染失败。
7. 只能引用工具实际返回的数字和文件，不得编造结果。
8. 不展示隐藏的内部推理，只简要说明已执行的动作和可验证结果。
9. 当前只完成计划中指定的步骤，不要擅自重复已经完成的工作。
10. transform_data 只生成派生视图，不会改变主数据；不得把筛选视图当作最终清洗数据导出。

分析深度要求：
11. 统计分析时优先选择最能揭示数据特征的指标：分布形态（偏度/峰度）、离散程度、分位数而非仅仅均值。
12. 发现显著关系时，主动补充效应量和置信区间，帮助用户判断实际意义而非仅仅统计显著性。
13. 多维度数据优先使用分组对比、小倍数图或热力图揭示模式，避免将所有信息塞进一张图。
14. 每步执行完毕后，用一句话总结本步核心发现，必须包含具体数字和它对分析目标意味着什么，
    便于后续步骤和最终报告直接引用（例："华东区收入 120 万最高，是西北区的 2.3 倍，区域差异是收入的主要驱动"）。
"""


class PlanStep(BaseModel):
    """分析计划中的单个可执行步骤。

    Attributes:
        id: 稳定的短标识符（如 ``inspect``、``visualize``），用于去重和引用。
        title: 面向用户展示的中文短标题。
        instruction: 传递给 ReAct 执行器的具体任务指令。
        success_criteria: 可观测的完成标准，供重规划器判断是否达标。
    """

    id: str = Field(description="Stable short step id, for example inspect or visualize")
    title: str = Field(description="Short Chinese title shown to the user")
    instruction: str = Field(description="Concrete instruction for the ReAct executor")
    success_criteria: str = Field(description="Observable completion criteria")


class AnalysisPlan(BaseModel):
    """LLM 结构化输出的完整分析计划。

    Attributes:
        objective: 一句话分析目标，贯穿所有步骤。
        steps: 2-8 个有序步骤，第一步必须是数据检查。
    """

    objective: str = Field(description="One-sentence analysis objective")
    steps: list[PlanStep] = Field(min_length=1, max_length=8)


class ReplanDecision(BaseModel):
    """重规划器的结构化决策输出。

    Attributes:
        done: 是否已有足够证据结束分析。
        rationale: 简短理由（不暴露内部推理链）。
        remaining_steps: 若未结束，返回仍需执行的后续步骤。
    """

    done: bool = Field(description="Whether enough evidence exists to finish")
    rationale: str = Field(description="Brief reason without hidden chain of thought")
    remaining_steps: list[PlanStep] = Field(default_factory=list, max_length=8)


class WorkflowState(TypedDict, total=False):
    """LangGraph 工作流的全局状态字典。

    所有节点通过返回 partial dict 来更新状态（reducer 语义为覆盖）。
    """

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
    """一次完整分析的最终输出，由 finalize 节点组装。

    Attributes:
        response: Markdown 格式的中文分析报告。
        trace: 工具调用与结果的审计轨迹。
        artifacts: 生成的图表、数据文件等产物元数据。
        dataset_profile: 数据集概况快照。
        plan: 原始计划步骤列表。
        completed_steps: 各步骤执行结果（含 status 和 summary）。
    """

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


class ReportStreamCallback(BaseCallbackHandler):
    """流式文本回调：把 LLM 生成的 token 实时推送到前端。

    主流 Agent 体验的核心在于"边生成边出字"。之前 finalize 用 model.invoke
    阻塞调用，用户要等完整报告生成才能看到任何文字（可能 30-60 秒）。
    本回调通过 on_llm_new_token 钩子把每个 token 通过 event_callback 推送，
    前端逐字拼接渲染，体验从"等 30 秒看完整报告"变为"看着报告逐字写出"。

    event_type 区分用途：
    - ``report_chunk``：finalize 节点的最终报告流式输出。
    - ``chat_chunk``：轻量追问（chat）节点的回答流式输出。
    前端用不同事件类型区分渲染区域（报告区 vs 对话气泡）。
    """

    def __init__(
        self,
        event_callback: Callable[[str, dict[str, Any]], None],
        event_type: str = "report_chunk",
    ) -> None:
        self.event_callback = event_callback
        self.event_type = event_type

    def on_llm_new_token(self, token: str, **kwargs: Any) -> Any:
        if token:
            try:
                self.event_callback(self.event_type, {"chunk": token})
            except Exception:
                pass


class ToolTraceCallback(BaseCallbackHandler):
    """工具追踪回调：把 ReAct 执行器内部每次工具调用实时推送到前端。

    之前 execute_step 整个 ReAct 循环（可能 5-25 次工具调用）聚合为一次
    yield，用户只看到"正在执行 (2/4)"一行字，完全不知道内部在做什么。
    本回调通过 on_tool_start/on_tool_end 钩子推送 tool_call/tool_result
    事件，前端在步骤卡片内展开工具调用时间线，让用户看到"正在读取数据
    → 正在清洗 → 正在生成图表"的实时过程。
    """

    def __init__(self, event_callback: Callable[[str, dict[str, Any]], None]) -> None:
        self.event_callback = event_callback
        self._tool_starts: dict[str, float] = {}

    def on_tool_start(self, serialized: dict[str, Any] | None = None, input_str: str = "", **kwargs: Any) -> Any:
        import time
        tool_name = (serialized or {}).get("name", "unknown") if serialized else "unknown"
        run_id = str(kwargs.get("run_id", ""))
        self._tool_starts[run_id] = time.time()
        try:
            self.event_callback("tool_call", {
                "call_id": run_id,
                "name": tool_name,
                "input_preview": (input_str or "")[:200],
                "started_at": self._tool_starts[run_id],
            })
        except Exception:
            pass

    def on_tool_end(self, output: str = "", **kwargs: Any) -> Any:
        import time
        run_id = str(kwargs.get("run_id", ""))
        started = self._tool_starts.pop(run_id, None)
        duration_ms = int((time.time() - started) * 1000) if started else 0
        try:
            self.event_callback("tool_result", {
                "call_id": run_id,
                "output_preview": (str(output) if output else "")[:300],
                "duration_ms": duration_ms,
            })
        except Exception:
            pass


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


def _is_recoverable_format_error(text: str) -> bool:
    """判断工具错误是否属于可通过 repair_data_format 自动修复的格式问题。

    仅匹配明确的类型/编码/日期格式错误标记；业务数据异常（如离群值、
    负数）不在此列，避免 Agent 擅自修改有效数据。
    """
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
                report_streamer = ReportStreamCallback(self.event_callback)
                final_message = self.model.invoke(prompt, config=self._invoke_config(report_streamer))
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
        )

    def stream(
        self,
        query: str,
        history: list[BaseMessage] | None = None,
        resume_from: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """流式执行分析，逐节点 yield 中间状态更新。

        每次 yield 一个 ``{"node": <节点名>, "data": <状态增量>}`` 字典，
        API 层将其转换为 SSE 事件推送给前端。

        Args:
            query: 用户的分析任务描述。
            history: 可选的多轮对话历史。
            resume_from: 断点续跑的恢复点，包含 ``plan`` 和 ``completed_steps``。

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

        Args:
            query: 用户的追问内容。
            history: 之前的对话历史，用于上下文续接。

        Returns:
            ``(response_text, new_artifacts)`` 元组。``new_artifacts`` 是本次
            追问期间通过工具调用新生成的产物（图表、导出数据等）元数据。

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

        result = self.react_agent.invoke(
            {"messages": messages},
            config=self._invoke_config(
                tool_tracer,
                chat_streamer,
                recursion_limit=self.settings.max_iterations * 2 + 5,
            ),
        )
        final_ai = next(
            (message for message in reversed(result["messages"]) if isinstance(message, AIMessage)),
            None,
        )
        response_text = _message_text(final_ai) or "（未生成回复）"
        new_artifacts = list(self.workspace.artifacts)[artifact_before:]
        return response_text, new_artifacts
