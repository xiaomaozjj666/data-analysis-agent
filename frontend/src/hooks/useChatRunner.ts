import { useRef, type RefObject } from "react";
import { useAppStore } from "../store/useAppStore";
import { ApiError, describeApiError, requestHeaders } from "../utils/api";
import { consumeSSEStream } from "../utils/sse";
import { SSE_EVENT_TYPES } from "../utils/sse-events";
import { API_URL } from "../constants";
import type { FollowUpMessage } from "../types";

interface UseChatRunnerResult {
  startFollowUp: () => Promise<void>;
  stopFollowUp: () => void;
  // 共享 ref：App.tsx 的 handleSessionLost / deleteSession 需要中断追问流；
  // followUpInputRef 由 ConversationThread 受控聚焦使用（handleEditFollowUp
  // 也读取它聚焦输入框），故从 hook 导出。
  chatControllerRef: RefObject<AbortController | null>;
  followUpInputRef: RefObject<HTMLTextAreaElement | null>;
}

// 追问运行器 hook：提取自 App.tsx 的 startFollowUp / stopFollowUp 与相关 ref。
// 行为与原 App.tsx 完全一致——startFollowUp / stopFollowUp 保持为普通函数
// （每次渲染重建，闭包始终读取最新 store 值），与原 function 声明语义相同。
function useChatRunner(): UseChatRunnerResult {
  // 所有 UI 状态从 Zustand store 获取，与原 App.tsx 同源。
  const {
    session, followUpInput, chatRunning, running,
    setChatRunning, setError, setFollowUpInput, setFollowUps, setSession,
  } = useAppStore();

  const followUpInputRef = useRef<HTMLTextAreaElement>(null);
  const chatControllerRef = useRef<AbortController | null>(null);

  // 轻量追问：基于已有分析结果继续提问，走 /chat/stream 端点。
  // 与 startAnalysis 的区别：
  //   - 不触发 plan→execute→finalize 工作流，单次 ReAct 循环回答
  //   - 回答渲染为对话气泡（而非主报告区），保留主报告不动
  //   - 工具调用时间线内嵌在气泡内，不占用 PlanPanel 的全局 toolTrace
  //   - 新产物追加到 session.artifacts，让产物中心即时更新
  async function startFollowUp() {
    const message = followUpInput.trim();
    if (!session || !message || chatRunning || running) return;
    setChatRunning(true);
    setError("");
    setFollowUpInput("");
    // 乐观插入用户气泡 + 空壳 assistant 气泡，让用户立即看到自己的提问
    const userBubble: FollowUpMessage = { role: "user", content: message };
    const assistantBubble: FollowUpMessage = { role: "assistant", content: "", streaming: true, tools: [] };
    setFollowUps((prev) => [...prev, userBubble, assistantBubble]);
    const controller = new AbortController();
    chatControllerRef.current = controller;
    let idleTimeout: number | null = null;
    const resetIdleTimeout = () => {
      if (idleTimeout != null) window.clearTimeout(idleTimeout);
      idleTimeout = window.setTimeout(() => controller.abort(), 120000);
    };
    resetIdleTimeout();
    try {
      const response = await fetch(`${API_URL}/api/sessions/${session.id}/chat/stream`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ task: message }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new ApiError(describeApiError(payload, response.status), response.status);
      }
      // SSE 事件分发：buffer 拆分 / event+data 提取 / JSON 解析由
      // consumeSSEStream 统一处理，这里只声明各事件的业务逻辑。
      // error 事件抛出的异常会被 consumeSSEStream 重新抛出，落到下面的 catch。
      const appendToLastAssistant = (mutate: (last: FollowUpMessage) => FollowUpMessage) => setFollowUps((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant") {
          next[next.length - 1] = mutate(last);
        }
        return next;
      });
      await consumeSSEStream(response, {
        [SSE_EVENT_TYPES.CHAT_CHUNK]: (data) => {
          // 流式追加到最后一个 assistant 气泡
          appendToLastAssistant((last) => ({
            ...last,
            content: (last.content || "") + (data.chunk || ""),
          }));
        },
        [SSE_EVENT_TYPES.THINKING_CHUNK]: (data) => {
          // 思考过程追加到最后一个 assistant 气泡的 reasoning 字段，
          // ConversationBubble 内嵌的 ReasoningBlock 会自动展示。
          if (!data.chunk) return;
          appendToLastAssistant((last) => ({
            ...last,
            reasoning: (last.reasoning || "") + (data.chunk || ""),
          }));
        },
        [SSE_EVENT_TYPES.TOOL_CALL]: (data) => {
          appendToLastAssistant((last) => ({
            ...last,
            tools: [...(last.tools || []), {
              call_id: data.call_id, name: data.name,
              status: "running", started_at: data.started_at,
            }],
          }));
        },
        [SSE_EVENT_TYPES.TOOL_RESULT]: (data) => {
          appendToLastAssistant((last) => ({
            ...last,
            tools: (last.tools || []).map((t) => t.call_id === data.call_id
              ? { ...t, status: "done", duration_ms: data.duration_ms }
              : t),
          }));
        },
        [SSE_EVENT_TYPES.CHAT_DONE]: (data) => {
          // 终态：写入完整回复（防止 chunk 丢失），标记非流式，追加新产物
          appendToLastAssistant((last) => ({
            ...last,
            content: data.response || last.content,
            streaming: false,
            // 后端可能在终态一次性给出完整 reasoning / usage，覆盖流式累计值
            reasoning: data.reasoning || last.reasoning,
            usage: data.usage || last.usage,
          }));
          if (data.artifacts?.length) {
            setSession((current) => current
              ? { ...current, artifacts: [...(current.artifacts || []), ...(data.artifacts || [])] }
              : current);
          }
        },
        [SSE_EVENT_TYPES.CANCELLED]: (data) => {
          appendToLastAssistant((last) => ({
            ...last,
            streaming: false,
            error: data.message || "追问已取消。",
          }));
        },
        [SSE_EVENT_TYPES.ERROR]: (data) => {
          throw new Error(data.message || "追问失败");
        },
      }, { onChunk: resetIdleTimeout });
    } catch (err) {
      const error = err as Error;
      if (error.name === "AbortError") {
        setFollowUps((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === "assistant" && last.streaming) {
            next[next.length - 1] = { ...last, streaming: false, error: "追问已取消或超时。" };
          }
          return next;
        });
      } else {
        setFollowUps((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === "assistant") {
            next[next.length - 1] = { ...last, streaming: false, error: error.message };
          }
          return next;
        });
      }
    } finally {
      if (idleTimeout != null) window.clearTimeout(idleTimeout);
      if (chatControllerRef.current === controller) chatControllerRef.current = null;
      setChatRunning(false);
    }
  }

  function stopFollowUp() {
    chatControllerRef.current?.abort();
  }

  return { startFollowUp, stopFollowUp, chatControllerRef, followUpInputRef };
}

export default useChatRunner;
