// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { createRef } from "react";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  consumeSSEStream: vi.fn(async () => {}),
}));

vi.mock("../../utils/api", () => ({
  api: mocks.api,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status = 500) {
      super(message);
      this.status = status;
    }
  },
  describeApiError: (payload: { detail?: string }, status: number) =>
    payload?.detail || `请求失败（${status}）`,
  requestHeaders: (headers: Record<string, string> = {}) => headers,
}));

vi.mock("../../utils/sse", () => ({ consumeSSEStream: mocks.consumeSSEStream }));
vi.mock("../../utils/notify", () => ({ notifyAnalysisDone: vi.fn() }));

import useAnalysisRunner from "../useAnalysisRunner";
import { useAnalysisStore } from "../../store/useAnalysisStore";
import { useBusyStore } from "../../store/useBusyStore";
import { useErrorStore } from "../../store/useErrorStore";
import { useSessionStore } from "../../store/useSessionStore";

function makeSession(id = "s1") {
  return {
    id,
    filename: "a.csv",
    preview: [],
    profile: {},
    artifacts: [],
    analysis_status: "idle",
  } as never;
}

function setup() {
  const handleSessionLost = vi.fn();
  const { result } = renderHook(() =>
    useAnalysisRunner({
      handleSessionLost,
      retryController: createRef<AbortController | null>(),
      autoRecover: vi.fn(),
    }),
  );
  return { result, handleSessionLost };
}

beforeEach(() => {
  mocks.api.mockReset();
  mocks.consumeSSEStream.mockClear();
  useSessionStore.setState({ session: null });
  useAnalysisStore.setState({ running: false, task: "" });
  useBusyStore.setState({ stopping: false });
  useErrorStore.setState({ error: "", errorExpanded: false });
});

describe("useAnalysisRunner 核心流程", () => {
  it("没有会话或任务为空时不发请求", async () => {
    const { result } = setup();
    await act(async () => {
      await result.current.startAnalysis("分析一下");
    });
    expect(mocks.consumeSSEStream).not.toHaveBeenCalled();
  });

  it("有会话时向 /analyze/stream POST 任务并在结束后复位 running", async () => {
    useSessionStore.setState({ session: makeSession("s1") });
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = setup();
    await act(async () => {
      await result.current.startAnalysis("分析销售趋势");
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/sessions/s1/analyze/stream");
    expect(init?.body ? JSON.parse(String(init.body)) : null).toMatchObject({ task: "分析销售趋势" });
    expect(mocks.consumeSSEStream).toHaveBeenCalledTimes(1);
    expect(useAnalysisStore.getState().running).toBe(false);
    vi.unstubAllGlobals();
  });

  it("stopAnalysis 先给「正在停止」反馈，再请求 cancel 接口", async () => {
    useSessionStore.setState({ session: makeSession("s1") });
    useAnalysisStore.setState({ running: true });
    useBusyStore.setState({ stopping: false });
    mocks.api.mockResolvedValue({ analysis_status: "completed" });

    const { result } = setup();
    await act(async () => {
      await result.current.stopAnalysis();
    });

    const calls = mocks.api.mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes("/api/sessions/s1/cancel"))).toBe(true);
    expect(useAnalysisStore.getState().running).toBe(false);
    expect(useBusyStore.getState().stopping).toBe(false);
    expect(useErrorStore.getState().error).toContain("已停止");
  });
});
