"""LangChain 回调处理器集合。

包含取消、流式报告、工具追踪、思考过程流式和 token 用量累计等回调，
用于在工作流执行过程中实现取消响应、实时事件推送和用量统计。
从 agent.py 拆分以保持模块职责单一。
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from data_agent.models import AnalysisCancelled


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


class ReasoningStreamCallback(BaseCallbackHandler):
    """思考过程流式回调：把 DeepSeek reasoning_content 实时推送到前端。

    DeepSeek thinking 模式下，LLM 输出分两段：先是 reasoning_content（思考过程），
    然后是 content（最终回答）。ReportStreamCallback 只捕获 content token，
    reasoning_content 被丢弃，用户完全看不到 Agent 的推理链路。

    本回调通过 on_llm_new_token 钩子检查 chunk.additional_kwargs.reasoning_content，
    把思考过程以 thinking_chunk 事件实时推送，前端 ReasoningBlock 流式展示，
    让用户看到"Agent 正在想什么"，减少黑盒等待焦虑（与 DeepSeek 官网 / Claude
    思考过程展示体验一致）。

    同时把 reasoning 累计到 buffer 列表，供 finalize/chat 返回时一次性给出完整文本。
    """

    def __init__(
        self,
        event_callback: Callable[[str, dict[str, Any]], None],
        buffer: list[str] | None = None,
        event_type: str = "thinking_chunk",
    ) -> None:
        self.event_callback = event_callback
        self.buffer = buffer if buffer is not None else []
        self.event_type = event_type

    def on_llm_new_token(self, token: str, **kwargs: Any) -> Any:
        # DeepSeek 的 reasoning_content 在 chunk.additional_kwargs 中，不在 token 参数里。
        # chunk 可能是 ChatGenerationChunk（有 .message）或 AIMessageChunk（直接有 .additional_kwargs）。
        chunk = kwargs.get("chunk")
        reasoning = ""
        if chunk is not None:
            message = getattr(chunk, "message", chunk)
            additional_kwargs = getattr(message, "additional_kwargs", None) or {}
            reasoning = additional_kwargs.get("reasoning_content", "") or ""
        if not reasoning:
            return
        self.buffer.append(reasoning)
        try:
            self.event_callback(self.event_type, {"chunk": reasoning})
        except Exception:
            pass


class UsageAccumulator(BaseCallbackHandler):
    """Token 用量累计回调：在每次 LLM 调用结束时累计 prompt/completion/total tokens。

    一次完整分析可能包含 plan + 多次 ReAct LLM 调用 + finalize，每调用一次
    on_llm_end 触发一次。本回调把所有调用的用量求和，分析结束后通过 snapshot()
    一次性给出总用量，前端在报告底部以 chip 形式展示（与 ChatGPT/Claude 用量
    展示一致）。

    兼容两种用量来源：
    1. response.llm_output["token_usage"]（OpenAI/DeepSeek 原生格式，prompt_tokens 字段）
    2. generation.message.usage_metadata（LangChain 标准格式，input_tokens 字段）
    """

    def __init__(self) -> None:
        self.total_input = 0
        self.total_output = 0
        self.total_total = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> Any:
        # 优先从 llm_output 提取（OpenAI/DeepSeek 原生格式）
        llm_output = getattr(response, "llm_output", None) or {}
        token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if token_usage:
            self.total_input += int(token_usage.get("prompt_tokens", 0) or 0)
            self.total_output += int(token_usage.get("completion_tokens", 0) or 0)
            self.total_total += int(token_usage.get("total_tokens", 0) or 0)
            return
        # 回退到 generation 级别的 usage_metadata（LangChain 标准格式）
        generations = getattr(response, "generations", None) or []
        for batch in generations:
            for generation in batch or []:
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                usage = getattr(message, "usage_metadata", None) or {}
                if usage:
                    self.total_input += int(usage.get("input_tokens", 0) or 0)
                    self.total_output += int(usage.get("output_tokens", 0) or 0)
                    self.total_total += int(usage.get("total_tokens", 0) or 0)

    def snapshot(self) -> dict[str, int]:
        """返回当前累计的 token 用量快照。"""
        return {
            "prompt_tokens": self.total_input,
            "completion_tokens": self.total_output,
            "total_tokens": self.total_total,
        }
