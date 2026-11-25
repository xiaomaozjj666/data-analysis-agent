import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// useChatRunner 行为测试：追问流（/chat/stream）的完整生命周期。
// 覆盖：入口守卫（无会话 / 空问题 / 正在分析 / 正在追问）→ 乐观插入气泡 →
// 各 SSE 事件（chunk / thinking / tool_call / tool_result / chat_done /
// cancelled / error）→ 终态产物合并、HTTP 错误、AbortError 与手动停止。
// consumeSSEStream 被替换为直接调用 handlers 的桩，不产生真实网络请求。

type SSEHandlerMap = Record<string, (data?: any) => void>;
type SSEOptions = { onChunk?: () => void; onEvent?: (event: string) => void };

const sseMock = vi.hoisted(() => ({
  consumeSSEStream: vi.fn<
    (response: Response, handlers: SSEHandlerMap, options?: SSEOptions) => Promise<void>
  >(async () => {}),
}));

vi.mock("../../utils/sse", () => ({ consumeSSEStream: sseMock.consumeSSEStream }));

import useChatRunner from "../useChatRunner";
import { useAnalysisStore } from "../../store/useAnalysisStore";
import { useChatStore } from "../../store/useChatStore";
import { useErrorStore } from "../../store/useErrorStore";
import { useSessionStore } from "../../store/useSessionStore";
import { API_URL } from "../../constants";
import type { Artifact, Session } from "../../types";

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "s-chat",
    filename: "sales.csv",
    preview: [],
    profile: { row_count: 0, column_count: 0, column_info: [] },
    artifacts: [],
    analysis_status: "completed",
    ...overrides,
  };
}

function chartArtifact(name = "追问图.html"): Artifact {
  return { name, kind: "visualization", path: `/tmp/${name}`, description: "追问生成" };
}

function resetStores() {
  useSessionStore.setState({ session: makeSession() });
  useAnalysisStore.setState({ running: false, task: "" });
  useChatStore.setState({ followUps: [], followUpInput: "", chatRunning: false });
  useErrorStore.setState({ error: "", errorExpanded: false });
}

beforeEach(() => {
  sseMock.consumeSSEStream.mockReset();
  sseMock.consumeSSEStream.mockImplementation(async () => {});
  resetStores();
});

describe("useChatRunner 入口守卫", () => {
  it("没有会话 / 问题为空 / 分析或追问进行中时不发请求，也不插入气泡", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useChatRunner());

    // 问题为空
    await act(async () => {
      await result.current.startFollowUp();
    });
    // 没有会话
    act(() => {
      useChatStore.setState({ followUpInput: "继续看看华东" });
      useSessionStore.setState({ session: null });
    });
    await act(async () => {
      await result.current.startFollowUp();
    });
    // 正在分析
    act(() => {
      useSessionStore.setState({ session: makeSession() });
      useAnalysisStore.setState({ running: true });
    });
    await act(async () => {
      await result.current.startFollowUp();
    });
    // 正在追问
    act(() => {
      useAnalysisStore.setState({ running: false });
      useChatStore.setState({ chatRunning: true });
    });
    await act(async () => {
      await result.current.startFollowUp();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(useChatStore.getState().followUps).toEqual([]);
    vi.unstubAllGlobals();
  });
});

describe("useChatRunner 追问流", () => {
  it("成功流：乐观气泡 → 流式内容/思考/工具 → 终态覆盖并合并新产物", async () => {
    useChatStore.setState({ followUpInput: "  华东为什么下降？  " });
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // 模拟服务端依次推送事件后正常结束；顺带记录 onChunk 是否被传下来
    //（idle 超时续期的唯一触发点）。
    let chunkHook = 0;
    sseMock.consumeSSEStream.mockImplementation(async (_response, handlers, options) => {
      handlers.chat_chunk({ chunk: "因为" });
      handlers.chat_chunk({ chunk: "退货率上升" });
      handlers.thinking_chunk({ chunk: "先检查退货字段" });
      handlers.thinking_chunk({}); // 空 chunk 直接忽略
      handlers.tool_call({ call_id: "c1", name: "run_sql", started_at: 10 });
      handlers.tool_result({ call_id: "c1", duration_ms: 42 });
      handlers.chat_done({
        response: "因为退货率上升。",
        reasoning: "完整思考",
        usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
        artifacts: [chartArtifact()],
      });
      if (options?.onChunk) {
        chunkHook += 1;
        options.onChunk();
      }
    });

    const { result } = renderHook(() => useChatRunner());
    await act(async () => {
      await result.current.startFollowUp();
    });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}/api/sessions/s-chat/chat/stream`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ task: "华东为什么下降？" });
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(chunkHook).toBe(1);

    const followUps = useChatStore.getState().followUps;
    expect(followUps).toHaveLength(2);
    expect(followUps[0]).toEqual({ role: "user", content: "华东为什么下降？" });
    const assistant = followUps[1];
    // chat_done 的完整回复覆盖流式累计值；reasoning / usage 取终态
    expect(assistant).toMatchObject({
      role: "assistant",
      content: "因为退货率上升。",
      reasoning: "完整思考",
      streaming: false,
      usage: { total_tokens: 15 },
    });
    expect(assistant.tools).toEqual([
      { call_id: "c1", name: "run_sql", status: "done", started_at: 10, duration_ms: 42 },
    ]);
    // 输入框清空、错误清空、追问标记复位
    expect(useChatStore.getState().followUpInput).toBe("");
    expect(useChatStore.getState().chatRunning).toBe(false);
    expect(useErrorStore.getState().error).toBe("");
    // 新产物并入当前会话
    expect(useSessionStore.getState().session?.artifacts).toEqual([chartArtifact()]);
    // 流结束后控制器归 null，避免误中断
    expect(result.current.chatControllerRef.current).toBeNull();
    vi.unstubAllGlobals();
  });

  it("chat_done 不带 response 时保留已流式生成的内容", async () => {
    useChatStore.setState({ followUpInput: "补充一句" });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    sseMock.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      handlers.chat_chunk({ chunk: "流式内容" });
      handlers.chat_done({});
    });

    const { result } = renderHook(() => useChatRunner());
    await act(async () => {
      await result.current.startFollowUp();
    });

    const assistant = useChatStore.getState().followUps[1];
    expect(assistant.content).toBe("流式内容");
    expect(assistant.streaming).toBe(false);
    // 没有 artifacts 时不动 session
    expect(useSessionStore.getState().session?.artifacts).toEqual([]);
    expect(result.current.chatControllerRef.current).toBeNull();
    vi.unstubAllGlobals();
  });

  it("HTTP 非 2xx：把服务端 detail 写进 assistant 气泡并复位状态", async () => {
    useChatStore.setState({ followUpInput: "问个问题" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 422,
        json: async () => ({ detail: "问题过长，请精简后再试" }),
      })),
    );

    const { result } = renderHook(() => useChatRunner());
    await act(async () => {
      await result.current.startFollowUp();
    });

    const assistant = useChatStore.getState().followUps[1];
    expect(assistant.streaming).toBe(false);
    expect(assistant.error).toBe("问题过长，请精简后再试");
    expect(useChatStore.getState().chatRunning).toBe(false);
    expect(sseMock.consumeSSEStream).not.toHaveBeenCalled();
    expect(result.current.chatControllerRef.current).toBeNull();
    vi.unstubAllGlobals();
  });

  it("SSE error 事件：抛出的异常落到气泡错误态", async () => {
    useChatStore.setState({ followUpInput: "触发失败" });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    sseMock.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      // 与真实实现一致：dispatchSSEEvent 调用 handler，handler 抛错即中断流
      handlers.error({ message: "模型调用超时" });
    });

    const { result } = renderHook(() => useChatRunner());
    await act(async () => {
      await result.current.startFollowUp();
    });

    expect(useChatStore.getState().followUps[1].error).toBe("模型调用超时");
    expect(useChatStore.getState().chatRunning).toBe(false);
    vi.unstubAllGlobals();
  });

  it("cancelled 事件：显示后端取消原因并结束流式态", async () => {
    useChatStore.setState({ followUpInput: "取消看看" });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    sseMock.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      handlers.cancelled({ message: "服务端已取消本次追问" });
    });

    const { result } = renderHook(() => useChatRunner());
    await act(async () => {
      await result.current.startFollowUp();
    });

    const assistant = useChatStore.getState().followUps[1];
    expect(assistant.streaming).toBe(false);
    expect(assistant.error).toBe("服务端已取消本次追问");
    vi.unstubAllGlobals();
  });

  it("AbortError：把流式气泡标记为已取消/超时", async () => {
    useChatStore.setState({ followUpInput: "长问题" });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    sseMock.consumeSSEStream.mockImplementation(async () => {
      const err = new Error("aborted");
      err.name = "AbortError";
      throw err;
    });

    const { result } = renderHook(() => useChatRunner());
    await act(async () => {
      await result.current.startFollowUp();
    });

    const assistant = useChatStore.getState().followUps[1];
    expect(assistant.streaming).toBe(false);
    expect(assistant.error).toBe("追问已取消或超时。");
    expect(useChatStore.getState().chatRunning).toBe(false);
    vi.unstubAllGlobals();
  });

  it("stopFollowUp 中断进行中的请求（信号置为 aborted）", async () => {
    useChatStore.setState({ followUpInput: "进行中的追问" });
    let release: (() => void) | null = null;
    const streamPending = new Promise<void>((resolve) => {
      release = resolve;
    });
    let capturedSignal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        capturedSignal = init?.signal ?? undefined;
        return { ok: true, status: 200 };
      }),
    );
    sseMock.consumeSSEStream.mockImplementation(async () => {
      await streamPending;
    });

    const { result } = renderHook(() => useChatRunner());
    let startPromise: Promise<void> | undefined;
    act(() => {
      startPromise = result.current.startFollowUp();
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(useChatStore.getState().chatRunning).toBe(true);
    expect(capturedSignal?.aborted).toBe(false);

    act(() => {
      result.current.stopFollowUp();
    });
    expect(capturedSignal?.aborted).toBe(true);

    await act(async () => {
      release?.();
      await startPromise;
    });
    expect(useChatStore.getState().chatRunning).toBe(false);
    vi.unstubAllGlobals();
  });
});
