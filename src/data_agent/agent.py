"""DataAgent — Plan-and-Execute + ReAct hybrid.
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
import time
from collections.abc import Callable, Iterator
from threading import Event
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

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


class _ToolSchemaRunnable:
    """把"让模型按 Pydantic schema 返回结构化结果"做成一个可靠的可调用对象。

    为什么不用 ``model.with_structured_output(Schema)``：它默认走 function
    calling 并**强制 tool_choice**，而 DeepSeek 的 thinking 模式会直接拒绝：
    ``400 Thinking mode does not support this tool_choice``。此时规划节点会
    静默退化成"默认计划"——分析照样跑完，但模型其实从未参与规划（实测：所有
    planner/replanner 调用 100% 失败，只在日志里留了 exception）。

    这里改为 ``bind_tools([Schema])``（tool_choice 保持 auto，thinking 模式
    接受），并保留两级兜底：
    1. 正常路径：取 ``tool_calls[0].args`` 用 Pydantic 校验；
    2. 兜底路径：模型若把 JSON 写在正文里，从 ``content`` 里抠出 JSON 再校验
       （thinking 模型偶尔会"说"出 JSON 而不调用工具）；
    3. 仍失败则抛异常，由调用方决定是否降级——降级必须可见，不能静默。

    ``invoke`` 的签名与 LangChain Runnable 兼容（prompt / config）。
    """

    def __init__(self, model: Any, schema: type[BaseModel], *, logger: logging.Logger) -> None:
        self._runnable = model.bind_tools([schema])
        self._schema = schema
        self._logger = logger

    def invoke(self, prompt: Any, config: dict[str, Any] | None = None) -> BaseModel:
        message = self._runnable.invoke(prompt, config=config)
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            arguments = calls[0].get("args") if isinstance(calls[0], dict) else None
            if arguments:
                return self._schema.model_validate(arguments)
        content = getattr(message, "content", "")
        text = content if isinstance(content, str) else str(content)
        parsed = _extract_json_object(text)
        if parsed is not None:
            self._logger.warning("结构化输出退回正文 JSON 解析（模型未调用工具）")
            return self._schema.model_validate(parsed)
        raise ValueError(
            f"模型既未调用 {self._schema.__name__} 工具，正文里也没有可用 JSON：{text[:200]!r}"
        )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型正文里抠出第一个平衡的 JSON 对象（```json 代码块也认）。

    扫描时**必须跟踪字符串状态**：分析文本里出现 `{"steps": ["统计 profit} 列"]}`
    这种带右花括号的字符串很常见，纯数括号会提前截断——那样 planner 又会静默
    退化成默认计划（自测用例就是这么抓到这个洞的）。
    """
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    for start, char in enumerate(cleaned):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(cleaned)):
            current = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    return parsed if isinstance(parsed, dict) else None
    return None


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
        # ReAct 执行器的两道预算闸门（实测一次"按地区对比销售额 + 画关系图"的
        # 简单任务跑出 23 次工具调用 / 418 秒，用户感受就是"卡住了"）：
        #   - 单步工具调用上限：超过后本步**直接结束**，用已有结果写小结，
        #     而不是继续无限试探；复杂任务相当于强制收敛。
        #   - 单步模型调用上限：兜住"工具调用没有增长但模型一直在绕"的情况。
        # 两者都设 end 而非 error：超出预算属于可预期的降级，不该让整次分析失败。
        self.react_agent = create_agent(
            model=self.model,
            tools=self.tools,
            system_prompt=self.prompts["system_prompt"],
            middleware=[
                _handle_tool_error,
                ToolCallLimitMiddleware(
                    run_limit=self.settings.max_tool_calls_per_step, exit_behavior="end",
                ),
                ModelCallLimitMiddleware(
                    run_limit=self.settings.max_iterations, exit_behavior="end",
                ),
            ],
            name="data_analysis_react_executor",
        )
        # 结构化输出用 bind_tools（tool_choice=auto）：见 _ToolSchemaRunnable 的说明，
        # with_structured_output 会强制 tool_choice，thinking 模式直接 400。
        self.planner = _ToolSchemaRunnable(self.model, AnalysisPlan, logger=logger)
        self.replanner = _ToolSchemaRunnable(self.model, ReplanDecision, logger=logger)
        self.graph = self._build_workflow()
        # 上一次 chat() 调用累计的 token 用量与思考过程，供 API 层在 chat_done
        # 事件中读取。每次 chat() 调用会覆盖。stream()/run() 不使用这两个属性
        # （其用量通过 AnalysisResult 返回）。
        self._last_usage: dict[str, int] | None = None
        self._last_reasoning: str = ""
        #: 本轮分析的开始时刻（monotonic），供墙钟预算判断使用；None 表示未在跑。
        self._run_started_at: float | None = None

    def analysis_budget_left(self) -> float:
        """本轮分析剩余的时间预算（秒）；未在跑时返回配置值。"""
        if self._run_started_at is None:
            return self.settings.max_analysis_seconds
        return self.settings.max_analysis_seconds - (time.monotonic() - self._run_started_at)

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
        self._run_started_at = time.monotonic()
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
        self._run_started_at = time.monotonic()
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
