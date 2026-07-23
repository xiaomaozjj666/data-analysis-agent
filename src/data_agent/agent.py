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

import logging
from collections.abc import Callable, Iterator
from threading import Event
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

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
from data_agent.nodes._utils import _message_text
from data_agent.nodes.graph import build_graph
from data_agent.prompts import (
    _apply_query_constraints,
    _fallback_plan,
    _handle_tool_error,
    _is_recoverable_format_error,
    get_prompts,
)
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

# 公共 API：``DataAnalysisAgent`` 等主类与本模块定义的工具函数构成对外入口；
# _apply_query_constraints / _fallback_plan / _is_recoverable_format_error 自
# data_agent.prompts 重新导出以保持历史导入路径（``from data_agent.agent
# import _fallback_plan``）向后兼容。列出 __all__ 同时让 ruff 将其视为有意导出。
__all__ = [
    "AnalysisCancelled",
    "AnalysisResult",
    "DataAnalysisAgent",
    "_apply_query_constraints",
    "_fallback_plan",
    "_is_recoverable_format_error",
    "create_chat_model",
]

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
        self.prompts = get_prompts(self.settings.language)
        self.react_agent = create_agent(
            model=self.model,
            tools=self.tools,
            system_prompt=self.prompts["system_prompt"],
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

    def _ensure_not_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise AnalysisCancelled("分析已取消。")

    def _build_workflow(self):
        return build_graph(self)

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
