"""分析与流式接口路由。

- POST /api/sessions/{id}/analyze：同步执行分析。
- POST /api/sessions/{id}/analyze/stream：SSE 流式分析（前端主用）。
- POST /api/sessions/{id}/chat/stream：轻量追问 SSE。
- POST /api/sessions/{id}/cancel：取消正在运行的分析。

SSE 流通过 asyncio.Queue 在工作线程和事件循环之间传递事件，取消通过
threading.Event + CancelCallback 实现亚秒级响应。这些复杂函数从原 ``api.py``
VERBATIM 迁移，仅把 ``registry`` / ``_effective_settings`` / ``analysis_slots`` /
``DataAnalysisAgent`` 改为经 ``data_agent.api`` 访问，以兼容测试 monkeypatch。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from data_agent.agent import AnalysisCancelled, AnalysisResult
from data_agent.registry import (
    API_VERSION_INT,
    SSE_QUEUE_MAXSIZE,
    AnalyzeRequest,
    _artifact_payload,
    _history,
    _result_payload,
)
from data_agent.serialization import to_jsonable

logger = logging.getLogger(__name__)

router = APIRouter()

#: 回传给前端的失败文案最大长度。完整堆栈只进服务器日志，
#: 防止超长异常文本（如带文件路径的 traceback repr）泄露到客户端或撞坏 UI。
_CLIENT_ERROR_MAX_CHARS = 300


def _client_error_detail(exc: Exception) -> str:
    """把异常压缩成适合回传前端的简短文案。

    保留异常消息（多数是面向用户的中文业务提示，有实际价值），
    但截断过长内容；完整堆栈由调用方用 ``logger.exception`` 写入服务器日志。
    """
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > _CLIENT_ERROR_MAX_CHARS:
        return f"{text[:_CLIENT_ERROR_MAX_CHARS]}…"
    return text


#: 错误分类规则：(code, 友好提示, 关键词元组)。按顺序匹配，命中即停。
#: 关键词对照 openai SDK / LangChain 抛出的异常类名与消息（DeepSeek 兼容
#: OpenAI 协议）：APITimeoutError/"timed out" → 超时；RateLimitError/429 → 限流；
#: "insufficient balance"/402 → 余额不足；AuthenticationError/401 → Key 无效。
#: 用关键词而非 isinstance 判断：避免强依赖 openai 异常类层级（LangChain
#: 可能包装/转换异常），且对未来更换 SDK 版本更鲁棒。
_ERROR_CLASSIFY_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("model_timeout", "模型响应超时，可能是网络波动或服务繁忙，稍后重试通常可恢复",
     ("apitimeouterror", "timed out", "timeout", "超时")),
    ("quota_exhausted", "模型账户余额不足，请充值后重试",
     ("insufficient balance", "insufficient_quota", "error code: 402", "余额不足")),
    ("rate_limited", "请求触发模型限流，请稍等片刻再重试",
     ("ratelimiterror", "rate limit", "error code: 429", "too many requests")),
    ("auth_failed", "API Key 无效或已过期，请在设置中重新配置",
     ("authenticationerror", "invalid api key", "error code: 401", "unauthorized")),
    ("connection_failed", "无法连接模型服务，请检查网络或 base_url 配置",
     ("apiconnectionerror", "connection error", "connect timeout", "name resolution")),
)


def _classify_analysis_error(exc: Exception) -> tuple[str, str]:
    """把异常归类为 (code, hint)，供前端展示针对性文案与重试策略。

    未命中任何规则时回退 ("analysis_failed", "")，保持与旧版行为一致。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    for code, hint, keywords in _ERROR_CLASSIFY_RULES:
        if any(keyword in text for keyword in keywords):
            return code, hint
    return "analysis_failed", ""


def _error_payload(exc: Exception, *, prefix: str = "") -> dict[str, str]:
    """组装 SSE error 事件载荷：分类错误码 + 友好提示 + 原始简短文案。

    hint 非空时拼到 message 前缀，前端无需额外适配即可展示针对性文案；
    同时保留独立的 code/hint 字段，供前端未来按错误码定制重试策略。
    """
    code, hint = _classify_analysis_error(exc)
    detail = _client_error_detail(exc)
    message = f"{hint}（{detail}）" if hint else f"{prefix}{detail}"
    return {"message": message, "code": code, "hint": hint}


def _safe_emit(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, item: tuple[str, Any] | None) -> None:
    """跨线程安全推送 SSE 事件到事件循环的队列。

    loop.call_soon_threadsafe 在事件循环已关闭时抛 RuntimeError（进程关闭、
    ASGI worker 被 kill、客户端断连后 cleanup 等场景）。本函数捕获该异常
    避免崩溃——worker 线程的 finally 块仍需正常释放 slot 和 lock，若因
    call_soon_threadsafe 抛错而跳过 release，会导致 analysis_slots 和
    run_lock 永久泄漏（max_concurrent_analyses=2 时泄漏 2 次后服务死锁）。

    队列满时（QueueFull）按事件类型决策：thinking_chunk / report_chunk 等
    流式 token 可丢弃（过程信息），complete / error / cancelled 等终态事件
    强制入队（即使需淘汰最旧的 thinking_chunk 腾位置）。

    注意：asyncio.Queue.full() 不是线程安全的，不能在 worker 线程直接调用。
    改为在 call_soon_threadsafe 回调内部 catch QueueFull 来安全处理满队列。
    """
    event_type = item[0] if item else None
    droppable = event_type in ("thinking_chunk", "report_chunk", "chat_chunk")

    def _put() -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            if not droppable:
                # 终态事件不能丢：淘汰最旧元素后重试一次
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass  # 极端情况：队列已被其他回调清空或仍满，放弃本次写入

    try:
        loop.call_soon_threadsafe(_put)
    except RuntimeError:
        # 事件循环已关闭，丢弃事件但不崩溃，确保 finally 中的 release 能执行
        pass


def _sse(event: str, data: Any) -> str:
    # 向所有 dict 类型的 SSE 载荷注入 v 字段，声明事件版本。非 dict 载荷
    # （理论上不存在，但防御性处理）原样序列化。使用浅拷贝避免修改调用方
    # 传入的原始对象（如 _result_payload 返回的 dict 可能被其他逻辑引用）。
    if isinstance(data, dict):
        data = {**data, "v": API_VERSION_INT}
    return f"event: {event}\ndata: {json.dumps(to_jsonable(data), ensure_ascii=False)}\n\n"


@router.post("/api/sessions/{session_id}/analyze")
def analyze(session_id: str, request: AnalyzeRequest) -> dict[str, Any]:
    from data_agent import api

    record = api.registry.get(session_id)
    settings = api._effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    history = _history(record)
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话已有分析正在运行。")
    if not api.analysis_slots.acquire(blocking=False):
        record.run_lock.release()
        raise HTTPException(status_code=429, detail="当前服务正在处理其他分析，请稍后再试。")
    record.cancel_event.clear()
    record.set_running()
    record.current_task = request.task
    try:
        agent = api.DataAnalysisAgent(record.workspace, settings, cancel_event=record.cancel_event)
        result = agent.run(request.task, history=history, resume_from=request.resume_from)
        # 在持有 run_lock 的窗口内完成 chat/last_result/persist，避免另一线程
        # 在 release 与 persist 之间拿到锁并基于旧 chat 启动新分析。cancelled
        # 和 failed 分支没有 result，不需要写 chat，直接落到对应 except 持久化。
        record.chat.extend(
            [
                {"role": "user", "content": request.task},
                {"role": "assistant", "content": result.response},
            ]
        )
        record.last_result = result
        record.set_finished("completed")
        api.registry.persist(session_id, record)
    except AnalysisCancelled as exc:
        record.set_finished("cancelled")
        api.registry.persist(session_id, record)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Analysis failed for session %s", session_id)
        record.set_finished("failed")
        api.registry.persist(session_id, record)
        raise HTTPException(
            status_code=502, detail=f"分析执行失败：{_error_payload(exc)['message']}"
        ) from exc
    finally:
        record.current_task = ""
        record.cancel_event.clear()
        api.analysis_slots.release()
        record.run_lock.release()
    return _result_payload(session_id, result)


@router.post("/api/sessions/{session_id}/analyze/stream")
async def analyze_stream(session_id: str, request: AnalyzeRequest) -> StreamingResponse:
    from data_agent import api

    record = api.registry.get(session_id)
    settings = api._effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    history = _history(record)
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话已有分析正在运行。")
    if not api.analysis_slots.acquire(blocking=False):
        record.run_lock.release()
        raise HTTPException(status_code=429, detail="当前服务正在处理其他分析，请稍后再试。")
    record.cancel_event.clear()
    record.set_running()
    record.current_task = request.task

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE)

    def _emit_progress(node: str, title: str) -> None:
        # Called from the worker thread at node entry; hop back to the event
        # loop so the SSE generator can flush a progress frame immediately
        # instead of waiting for the node to finish.
        _safe_emit(loop, queue, ("progress", {"node": node, "title": title}))

    def _emit_event(event_type: str, payload: Any) -> None:
        # 通用事件通道：推送 report_chunk / tool_call / tool_result 等细粒度
        # 事件。与 _emit_progress 并存，前者保持后向兼容（progress_callback
        # 签名不变），后者承载新的流式体验。同样需要 call_soon_threadsafe
        # 跨线程回到事件循环。
        _safe_emit(loop, queue, (event_type, payload))

    def _run_analysis() -> None:
        try:
            agent = api.DataAnalysisAgent(
                record.workspace,
                settings,
                cancel_event=record.cancel_event,
                progress_callback=_emit_progress,
                event_callback=_emit_event,
            )
            final_payload: dict[str, Any] | None = None
            # plan_only 模式下记录 plan_analysis 节点输出，用于待审批计划
            # 持久化和 plan_ready 事件推送。完整执行模式下保持为 None，
            # 不影响原有 finalize 完成路径。
            plan_payload: dict[str, Any] | None = None
            for update in agent.stream(
                request.task,
                history=history,
                resume_from=request.resume_from,
                plan_only=request.plan_only,
            ):
                node = update["node"]
                data = update["data"]
                if node == "finalize":
                    final_payload = data
                if node == "plan_analysis":
                    plan_payload = data
                _safe_emit(loop, queue, (node, data))
            if request.plan_only:
                # 仅规划模式：不进入 finalize，提取计划并切换到待审批态。
                # 前端收到 plan_ready 事件后展示审批面板，用户确认后用
                # resume_from 注入 pending_plan 发起执行请求。
                if plan_payload is None:
                    raise RuntimeError("规划阶段未返回计划。")
                record.pending_plan = plan_payload.get("plan", [])
                record.set_awaiting_approval()
                api.registry.persist(session_id, record)
                _safe_emit(loop, queue, ("plan_ready", {
                    "plan": plan_payload.get("plan", []),
                    "objective": plan_payload.get("objective", ""),
                }))
            else:
                if final_payload is None:
                    raise RuntimeError("工作流没有返回最终结果。")
                result = AnalysisResult(
                    response=final_payload["response"],
                    trace=final_payload.get("trace", []),
                    artifacts=final_payload.get("artifacts", []),
                    dataset_profile=final_payload["dataset_profile"],
                    plan=final_payload.get("plan", []),
                    completed_steps=final_payload.get("completed_steps", []),
                    usage=final_payload.get("usage"),
                    reasoning=final_payload.get("reasoning", ""),
                )
                record.chat.extend(
                    [
                        {"role": "user", "content": request.task},
                        {"role": "assistant", "content": result.response},
                    ]
                )
                record.last_result = result
                record.set_finished("completed")
                try:
                    api.registry.persist(session_id, record)
                except Exception:
                    logger.exception("Failed to persist completed state for session %s", session_id)
                _safe_emit(loop, queue, ("complete", _result_payload(session_id, result)))
        except AnalysisCancelled:
            record.set_finished("cancelled")
            try:
                api.registry.persist(session_id, record)
            except Exception:
                logger.exception("Failed to persist cancelled state for session %s", session_id)
            _safe_emit(loop, queue, ("cancelled", {"message": "分析已取消。"}))
        except Exception as exc:
            logger.exception("Analysis worker failed for session %s", session_id)
            record.set_finished("failed")
            try:
                api.registry.persist(session_id, record)
            except Exception:
                logger.exception("Failed to persist failed state for session %s", session_id)
            _safe_emit(loop, queue, ("error", _error_payload(exc)))
        finally:
            record.current_task = ""
            # Always clear the cancel event so the next analysis on this
            # session starts from a clean state, even if the client aborted
            # mid-stream and set the event after the worker already finished.
            record.cancel_event.clear()
            # 关键：先释放 slot 和 lock，再 call_soon_threadsafe。
            # call_soon_threadsafe 在事件循环已关闭时会抛 RuntimeError
            # （进程关闭、ASGI worker 被 kill 等场景），若放在 release 之前，
            # 异常会跳过 release 导致 analysis_slots 和 run_lock 永久泄漏
            # —— max_concurrent_analyses=2 时泄漏 2 次后整个服务无法启动新分析。
            api.analysis_slots.release()
            record.run_lock.release()
            # 发送哨兵值 None 通知 SSE 生成器结束循环。_safe_emit 内部已处理
            # RuntimeError（loop 关闭）和 QueueFull，无需再 try/except。
            _safe_emit(loop, queue, None)

    worker = threading.Thread(target=_run_analysis, name=f"analysis-{session_id}", daemon=True)

    async def _await_worker_exit(timeout: float) -> bool:
        """Poll worker.is_alive() without blocking the event loop.

        ``threading.Thread.join`` is a blocking call; invoking it directly in a
        coroutine stalls the event loop for the entire timeout window, which
        means other HTTP requests (history polling, new uploads, cancels) are
        frozen while we wait for a possibly-stuck LLM call to unwind. Polling
        with ``asyncio.sleep`` keeps the loop responsive.
        """
        deadline = loop.time() + timeout
        while worker.is_alive() and loop.time() < deadline:
            await asyncio.sleep(0.1)
        return not worker.is_alive()

    async def generate():
        worker_started = False
        try:
            yield _sse("started", {"task": request.task})
            worker.start()
            worker_started = True
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield _sse("heartbeat", {"status": record.analysis_status})
                        continue
                    if item is None:
                        break
                    event, data = item
                    yield _sse(event, data)
            except asyncio.CancelledError:
                record.cancel_event.set()
                # CAS 式转换：只在当前仍是 running 时才写 cancelling 过渡态。
                # 若 worker 已先于本块写入 completed/cancelled/failed 终态，不能覆盖。
                with record._status_lock:
                    already_terminal = record._analysis_status not in {"running", "cancelling"}
                    if not already_terminal:
                        record._analysis_status = "cancelling"
                if not already_terminal:
                    api.registry.persist(session_id, record)
                exited = await _await_worker_exit(timeout=5.0)
                if not exited:
                    logger.warning(
                        "Analysis worker for session %s did not exit within 5s of cancel; "
                        "slot will be released when the current LLM call returns.",
                        session_id,
                    )
                raise
            finally:
                if worker.is_alive():
                    logger.debug(
                        "Worker still running at stream teardown for session %s; "
                        "daemon thread will release resources via its own finally.",
                        session_id,
                    )
        finally:
            # 兜底：客户端在首帧 started 后断开时（GeneratorExit 在 yield 处抛出），
            # worker.start() 尚未执行，worker 的 finally 不会释放 run_lock 和
            # analysis_slots。此处手动释放，避免锁永久泄漏导致会话不可用
            # （max_concurrent_analyses=2 时泄漏 2 次即全服务瘫痪）。
            if not worker_started:
                with record._status_lock:
                    if record._analysis_status == "running":
                        record._analysis_status = "failed"
                try:
                    api.analysis_slots.release()
                except ValueError:
                    pass
                try:
                    record.run_lock.release()
                except RuntimeError:
                    pass
                try:
                    api.registry.persist(session_id, record)
                except Exception:
                    logger.exception(
                        "Failed to persist abort state for session %s", session_id
                    )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, request: AnalyzeRequest) -> StreamingResponse:
    """轻量追问 SSE 端点：不走 plan-and-execute，直接用 ReAct 执行器回答。

    与 ``analyze/stream`` 的区别：
    - 不触发 validate→plan→execute→finalize 完整工作流，单次 ReAct 循环即可回答。
    - 不占用 ``analysis_slots``（追问更轻量，不与全量分析竞争全局并发槽）。
    - 仍持有 ``run_lock``，防止追问与全量分析在同一会话并发写 workspace。
    - 事件类型用 ``chat_chunk``（而非 ``report_chunk``），前端渲染到对话气泡。
    - 终态事件为 ``chat_done``（而非 ``complete``），携带回答文本和新增产物。

    适合场景：基于已有分析结果的快速追问，如"把刚才那张图改成红色"、
    "解释一下这个 p 值"、"再做一个年龄分布图"。
    """
    from data_agent import api

    record = api.registry.get(session_id)
    settings = api._effective_settings()
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key。")
    if record.analysis_status == "running":
        raise HTTPException(status_code=409, detail="当前会话有分析正在运行，请等待完成后再追问。")
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话正在处理其他请求，请稍后再试。")
    history = _history(record)
    record.cancel_event.clear()
    record.current_task = request.task

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE)

    def _emit_event(event_type: str, payload: Any) -> None:
        _safe_emit(loop, queue, (event_type, payload))

    def _run_chat() -> None:
        try:
            agent = api.DataAnalysisAgent(
                record.workspace,
                settings,
                cancel_event=record.cancel_event,
                event_callback=_emit_event,
            )
            response_text, new_artifacts = agent.chat(request.task, history=history)
            record.chat.extend(
                [
                    {"role": "user", "content": request.task},
                    {"role": "assistant", "content": response_text},
                ]
            )
            try:
                api.registry.persist(session_id, record)
            except Exception:
                logger.exception("Failed to persist chat state for session %s", session_id)
            _safe_emit(
                loop,
                queue,
                (
                    "chat_done",
                    {
                        "response": response_text,
                        "artifacts": _artifact_payload(session_id, new_artifacts),
                        "usage": agent._last_usage,
                        "reasoning": agent._last_reasoning,
                    },
                ),
            )
        except AnalysisCancelled:
            try:
                api.registry.persist(session_id, record)
            except Exception:
                logger.exception("Failed to persist cancelled chat state for session %s", session_id)
            _safe_emit(loop, queue, ("cancelled", {"message": "追问已取消。"}))
        except Exception as exc:
            logger.exception("Chat worker failed for session %s", session_id)
            _safe_emit(loop, queue, ("error", _error_payload(exc, prefix="追问失败：")))
        finally:
            record.current_task = ""
            record.cancel_event.clear()
            record.run_lock.release()
            _safe_emit(loop, queue, None)

    worker = threading.Thread(target=_run_chat, name=f"chat-{session_id}", daemon=True)

    async def generate():
        worker_started = False
        try:
            yield _sse("started", {"task": request.task})
            worker.start()
            worker_started = True
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield _sse("heartbeat", {"status": record.analysis_status})
                        continue
                    if item is None:
                        break
                    event, data = item
                    yield _sse(event, data)
            except asyncio.CancelledError:
                record.cancel_event.set()
                raise
            finally:
                if worker.is_alive():
                    logger.debug(
                        "Chat worker still running at stream teardown for session %s; "
                        "daemon thread will release run_lock via its own finally.",
                        session_id,
                    )
        finally:
            # 兜底：客户端在首帧 started 后断开时，worker 未启动，
            # run_lock 不会被 worker 的 finally 释放。手动释放避免会话永久不可用。
            if not worker_started:
                try:
                    record.run_lock.release()
                except RuntimeError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/sessions/{session_id}/cancel")
def cancel_analysis(session_id: str) -> dict[str, str]:
    from data_agent import api

    record = api.registry.get(session_id)
    # CAS 式状态转换：在 _status_lock 内原子地检查并切换 running → cancelling，
    # 避免"检查通过后 worker 已 set_finished('completed') 覆盖终态"的 TOCTOU 竞态。
    # 直接用 record.analysis_status = "cancelling" 会在 worker 已写入 completed 后
    # 把状态回退到 cancelling，而此时 worker 已退出，没有任何线程会再推进到 cancelled，
    # 导致会话永久卡在 cancelling。
    with record._status_lock:
        if record._analysis_status != "running":
            return {"status": record._analysis_status}
        record._analysis_status = "cancelling"
    # cancel_event 必须在锁外 set：worker 等待 event 时不会持有 _status_lock，
    # 但持锁调用 event.set() 不会带来收益，反而拉长锁持有时间。
    record.cancel_event.set()
    # Persist so a restart between this call and the worker's unwind does
    # not leave the manifest stuck on "running"; the retry poller relies on
    # seeing "cancelling" to recognize an interrupted analysis.
    try:
        api.registry.persist(session_id, record)
    except Exception:
        logger.exception("Failed to persist cancelling status for %s — may show stale state on restart", session_id)
    return {"status": "cancelling"}
