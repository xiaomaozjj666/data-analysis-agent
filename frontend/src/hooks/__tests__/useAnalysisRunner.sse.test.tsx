import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";

// useAnalysisRunner SSE 与失败路径测试（补充现有核心流程测试）。
// 覆盖：完整 SSE 事件序列（started/progress/plan/step/replan/thinking/
// finalize/report_chunk/tool/complete）→ 会话刷新与 UI 归位；plan_only 审批
// 流程；用户切到历史会话时不覆盖 UI；HTTP 404 会话失效；SSE error；网络错误
// （未收到事件 / 中途断线自动恢复）；idle 超时续跑入口；stopAnalysis 的两种
// 收尾文案与无运行守卫；complete 后刷新会话失败提示；断点续跑保留计划。

type SSEHandlerMap = Record<string, (data?: any) => void>;
type SSEOptions = { onChunk?: () => void; onEvent?: (event: string) => void };

const mocks = vi.hoisted(() => {
  class MockApiError extends Error {
    status: number;
    constructor(message: string, status = 500) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
  api: vi.fn(),
  consumeSSEStream: vi.fn<
    (response: Response, handlers: SSEHandlerMap, options?: SSEOptions) => Promise<void>
  >(async () => {}),
  notify: vi.fn(),
  wait: vi.fn(async () => {}),
  ApiError: MockApiError,
  };
});

vi.mock("../../utils/api", () => ({
  api: mocks.api,
  ApiError: mocks.ApiError,
  describeApiError: (payload: { detail?: string } | string, status: number) =>
    typeof payload === "string" ? payload : payload?.detail || `请求失败（${status}）`,
  requestHeaders: (headers: Record<string, string> = {}) => headers,
}));
vi.mock("../../utils/sse", () => ({ consumeSSEStream: mocks.consumeSSEStream }));
vi.mock("../../utils/notify", () => ({ notifyAnalysisDone: mocks.notify }));
vi.mock("../../utils/format", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../utils/format")>()),
  wait: mocks.wait,
}));

import useAnalysisRunner from "../useAnalysisRunner";
import { useAnalysisStore } from "../../store/useAnalysisStore";
import { useBusyStore } from "../../store/useBusyStore";
import { useChatStore } from "../../store/useChatStore";
import { useErrorStore } from "../../store/useErrorStore";
import { useReasoningStore } from "../../store/useReasoningStore";
import { useRetryStore } from "../../store/useRetryStore";
import { useSessionStore } from "../../store/useSessionStore";
import { useTimerStore } from "../../store/useTimerStore";
import { useToolTraceStore } from "../../store/useToolTraceStore";
import { API_URL } from "../../constants";
import type {
  AnalysisResult,
  CompletedStep,
  PlanStep,
  Session,
} from "../../types";

function planStep(id = "p1"): PlanStep {
  return { id, title: `步骤 ${id}`, instruction: "执行", success_criteria: "完成" };
}

function completedStep(id = "c1"): CompletedStep {
  return { id, title: `完成 ${id}`, status: "completed" };
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    filename: "sales.csv",
    preview: [],
    profile: { row_count: 0, column_count: 0, column_info: [] },
    artifacts: [],
    analysis_status: "running",
    ...overrides,
  };
}

function makeResult(overrides: Partial<AnalysisResult> = {}): AnalysisResult {
  return {
    response: "# 最终报告",
    artifacts: [],
    plan: [planStep()],
    completed_steps: [completedStep()],
    reasoning: "终态思考",
    usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
    ...overrides,
  };
}

function setup(overrides: { autoRecover?: (offer: unknown) => void } = {}) {
  const handleSessionLost = vi.fn();
  const retryController = createRef<AbortController | null>();
  // 不传 autoRecover 时模拟"没有断线自动恢复回调"的部署形态
  const autoRecover = overrides.autoRecover === undefined
    ? undefined
    : vi.fn(overrides.autoRecover);
  const { result, unmount } = renderHook(() =>
    useAnalysisRunner({ handleSessionLost, retryController, autoRecover }),
  );
  return { result, unmount, handleSessionLost, retryController, autoRecover };
}

function resetStores() {
  useSessionStore.setState({ session: makeSession(), activeTab: "analysis", lastActiveTab: "analysis" });
  useAnalysisStore.setState({
    task: "",
    plan: [],
    completed: [],
    result: null,
    running: false,
    awaitingApproval: false,
    pendingObjective: "",
    planSource: "model",
    replanReason: "",
    stepProgress: null,
    currentNodeTitle: "",
  });
  useBusyStore.setState({ stopping: false });
  useRetryStore.setState({ retryOffer: null, retryChecking: false });
  useReasoningStore.setState({ reasoning: "", reasoningStreaming: false, usage: null });
  useToolTraceStore.setState({ toolTrace: [] });
  useTimerStore.setState({ elapsedSeconds: null });
  useChatStore.setState({ followUps: [] });
  useErrorStore.setState({ error: "", errorExpanded: false });
}

beforeEach(() => {
  mocks.api.mockReset();
  mocks.consumeSSEStream.mockReset();
  mocks.consumeSSEStream.mockImplementation(async () => {});
  mocks.notify.mockReset();
  mocks.wait.mockReset();
  mocks.wait.mockImplementation(async () => {});
  resetStores();
});

describe("useAnalysisRunner 完整流", () => {
  it("全部 SSE 事件按语义更新 UI，结束后刷新会话并归位运行态", async () => {
    useAnalysisStore.setState({ task: "分析销售趋势" });
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const completePayload = makeResult();
    mocks.api.mockResolvedValue({
      ...makeSession(),
      analysis_status: "completed",
      analysis_started_at: 1700000000,
      elapsed_seconds: 42,
      last_result: completePayload,
    });
    mocks.consumeSSEStream.mockImplementation(async (_response, handlers, options) => {
      handlers.started?.({});
      expect(useAnalysisStore.getState().currentNodeTitle).toBe("后端已接收任务");
      handlers.progress?.({ title: "读取数据" });
      expect(useAnalysisStore.getState().currentNodeTitle).toBe("读取数据");
      handlers.validate_dataset?.({});
      handlers.plan_analysis?.({ plan: [planStep("a"), planStep("b")] });
      expect(useAnalysisStore.getState().plan).toHaveLength(2);
      handlers.step_progress?.({ progress: 40, tool_calls: 2, message: "查聚合", step_index: 1, total_steps: 3 });
      expect(useAnalysisStore.getState().stepProgress).toMatchObject({
        progress: 40, toolCalls: 2, message: "查聚合", stepIndex: 1, totalSteps: 3,
      });
      handlers.execute_step?.({});
      handlers.replan?.({ completed_steps: [completedStep("r1")], replan_reason: "已用完分析预算" });
      expect(useAnalysisStore.getState().replanReason).toBe("已用完分析预算");
      handlers.thinking_chunk?.({ chunk: "先看字段" });
      handlers.thinking_chunk?.({});
      expect(useReasoningStore.getState().reasoning).toBe("先看字段");
      expect(useReasoningStore.getState().reasoningStreaming).toBe(true);
      handlers.finalize?.({});
      expect(useReasoningStore.getState().reasoningStreaming).toBe(false);
      handlers.tool_call?.({ call_id: "t1", name: "run_sql", input_preview: "select 1", started_at: 5 });
      handlers.tool_result?.({ call_id: "t1", output_preview: "ok", duration_ms: 12 });
      handlers.report_chunk?.({ chunk: "缓缓输出 " });
      // 业务事件重置慢响应计时器；心跳只保活
      options?.onEvent?.("progress");
      options?.onEvent?.("heartbeat");
      options?.onChunk?.();
      handlers.complete?.(completePayload);
    });
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis();
    });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}/api/sessions/s1/analyze/stream`);
    expect(JSON.parse(String(init.body))).toMatchObject({
      task: "分析销售趋势",
      resume_from: null,
      plan_only: false,
    });

    const state = useAnalysisStore.getState();
    expect(state.result).toEqual(completePayload);
    expect(state.plan).toEqual(completePayload.plan);
    expect(state.completed).toEqual(completePayload.completed_steps);
    expect(state.running).toBe(false);
    expect(state.currentNodeTitle).toBe("");
    expect(state.stepProgress).toBeNull();
    expect(useReasoningStore.getState()).toMatchObject({
      reasoning: "终态思考",
      reasoningStreaming: false,
      usage: completePayload.usage,
    });
    expect(useToolTraceStore.getState().toolTrace).toEqual([
      expect.objectContaining({
        call_id: "t1",
        status: "done",
        output_preview: "ok",
        duration_ms: 12,
      }),
    ]);
    // complete 后刷新会话，拿到服务端精确耗时
    expect(mocks.api).toHaveBeenCalledWith("/api/sessions/s1");
    expect(mocks.api).toHaveBeenCalledTimes(1);
    expect(useTimerStore.getState().elapsedSeconds).toBe(42);
    expect(result.current.startedAtRef.current).toBe(1700000000);
    expect(useSessionStore.getState().session?.last_result).toEqual(completePayload);
    expect(mocks.notify).toHaveBeenCalledWith("分析已完成", "分析销售趋势");
    vi.unstubAllGlobals();
  });

  it("plan_only 流程：plan_ready 进入待审批并回填空任务的目标", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    mocks.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      handlers.plan_ready?.({
        plan: [planStep("x")],
        objective: "比较各区域销售",
        source: "fallback",
      });
    });
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis("比较各区域销售", null, true);
    });

    const state = useAnalysisStore.getState();
    expect(state.awaitingApproval).toBe(true);
    expect(state.pendingObjective).toBe("比较各区域销售");
    expect(state.planSource).toBe("fallback");
    expect(state.plan).toHaveLength(1);
    // 任务为空时用目标回填，用户能在输入框看到要做什么
    expect(state.task).toBe("比较各区域销售");
    expect(state.running).toBe(false);
    expect(useSessionStore.getState().activeTab).toBe("analysis");
    expect(mocks.api).not.toHaveBeenCalled();
    // 请求体带 plan_only
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({ plan_only: true });
    vi.unstubAllGlobals();
  });

  it("用户已切到历史会话时，complete/finalize 不把结果写进历史视图", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const other = makeSession({ id: "history", filename: "old.csv" });
    mocks.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      // 用户切走：后台流继续到达
      useSessionStore.setState({ session: other });
      handlers.progress?.({ title: "后台仍在跑" });
      handlers.plan_analysis?.({ plan: [planStep()] });
      handlers.finalize?.({});
      handlers.report_chunk?.({ chunk: "不应出现在历史视图" });
      handlers.complete?.(makeResult());
    });
    mocks.api.mockResolvedValue(makeSession({ analysis_status: "completed" }));
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis("跑一个分析");
    });

    // finalize/report_chunk/complete 都以"当前查看的会话"为界：历史视图不被覆盖
    expect(useAnalysisStore.getState().result).toBeNull();
    // 会话对象也不能被运行中会话的刷新结果替换
    expect(useSessionStore.getState().session).toEqual(other);
    // complete 仍会通知用户任务已完成（切到别的 Tab 也能收到）
    expect(mocks.notify).toHaveBeenCalledWith("分析已完成", "跑一个分析");
    vi.unstubAllGlobals();
  });

  it("断点续跑：保留已有计划与完成步骤，只清空旧报告", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const existingPlan = [planStep("keep")];
    const existingCompleted = [completedStep("keep")];
    useAnalysisStore.setState({
      plan: existingPlan,
      completed: existingCompleted,
      result: makeResult({ response: "旧报告" }),
    });
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis("继续跑", {
        plan: existingPlan,
        completed_steps: existingCompleted,
      });
    });

    expect(useAnalysisStore.getState().plan).toEqual(existingPlan);
    expect(useAnalysisStore.getState().completed).toEqual(existingCompleted);
    expect(useAnalysisStore.getState().result).toBeNull();
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      task: "继续跑",
      resume_from: { plan: existingPlan, completed_steps: existingCompleted },
      plan_only: false,
    });
    vi.unstubAllGlobals();
  });

  it("complete 后刷新会话失败时保留结果并给出轻提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    mocks.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      handlers.complete?.(makeResult({ response: "完整报告" }));
    });
    mocks.api.mockRejectedValue(new Error("网关超时"));
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(useAnalysisStore.getState().result?.response).toBe("完整报告");
    expect(useErrorStore.getState().error).toBe(
      "分析已完成，但同步最新会话数据失败，产物列表可能不完整；可在历史列表重新选择该会话刷新。",
    );
    vi.unstubAllGlobals();
  });
});

describe("useAnalysisRunner 失败与恢复", () => {
  it("HTTP 404：引导重新上传数据集", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 404,
        json: async () => ({ detail: "会话不存在" }),
      })),
    );
    const { result, handleSessionLost } = setup();

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(handleSessionLost).toHaveBeenCalledWith(
      "会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。",
    );
    expect(useAnalysisStore.getState().running).toBe(false);
    vi.unstubAllGlobals();
  });

  it("SSE error 事件：展示错误、通知失败并给手动重试入口", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    mocks.consumeSSEStream.mockImplementation(async (_response, handlers) => {
      handlers.error?.({ message: "工具执行崩溃" });
    });
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(useErrorStore.getState().error).toBe("工具执行崩溃");
    expect(useRetryStore.getState().retryOffer).toEqual({ task: "分析", reason: "error" });
    expect(mocks.notify).toHaveBeenCalledWith("分析失败", "工具执行崩溃");
    expect(useAnalysisStore.getState().awaitingApproval).toBe(false);
    expect(useAnalysisStore.getState().stepProgress).toBeNull();
    vi.unstubAllGlobals();
  });

  it("尚未收到任何事件时的网络错误：不自动恢复，保留手动重试", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const { result, autoRecover } = setup({ autoRecover: () => {} });

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(useErrorStore.getState().error).toBe("无法连接分析服务：Failed to fetch");
    expect(useRetryStore.getState().retryOffer).toEqual({ task: "分析", reason: "network" });
    expect(autoRecover).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("已收到事件后中途断线：自动触发状态轮询恢复", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    mocks.consumeSSEStream.mockImplementation(async (_response, handlers, options) => {
      handlers.progress?.({ title: "执行中" });
      options?.onEvent?.("progress");
      throw new TypeError("network lost");
    });
    const { result, autoRecover } = setup({ autoRecover: () => {} });

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(useErrorStore.getState().error).toBe("与服务器的连接中断，正在自动检查后台任务状态…");
    await waitFor(() =>
      expect(autoRecover).toHaveBeenCalledWith({ task: "分析", reason: "network" }),
    );
    vi.unstubAllGlobals();
  });

  it("idle 超时（AbortError）：带已完成步骤时提供可续跑的断点入口", async () => {
    useAnalysisStore.setState({
      plan: [planStep("p")],
      completed: [completedStep("c")],
    });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    mocks.consumeSSEStream.mockImplementation(async () => {
      const err = new Error("idle");
      err.name = "AbortError";
      throw err;
    });
    const { result, autoRecover } = setup({ autoRecover: () => {} });

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(useErrorStore.getState().error).toBe(
      "长时间未收到分析进度，正在自动检查后台任务状态…",
    );
    expect(useRetryStore.getState().retryOffer).toMatchObject({
      task: "分析",
      reason: "idle",
      canResume: true,
    });
    await waitFor(() => expect(autoRecover).toHaveBeenCalledTimes(1));
    vi.unstubAllGlobals();
  });

  it("没有自动恢复回调时 idle 超时给出明确的断线文案", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    mocks.consumeSSEStream.mockImplementation(async () => {
      const err = new Error("idle");
      err.name = "AbortError";
      throw err;
    });
    const { result } = setup();

    await act(async () => {
      await result.current.startAnalysis("分析");
    });

    expect(useErrorStore.getState().error).toBe("长时间未收到分析进度，连接已断开。");
    vi.unstubAllGlobals();
  });
});

describe("useAnalysisRunner 停止分析", () => {
  it("请求 cancel → 轮询到后端退出 → 提示已完成步骤已保留，并中断 SSE/轮询", async () => {
    useAnalysisStore.setState({ running: true });
    let sessionPolls = 0;
    const apiMock = mocks.api.mockImplementation(async (path: string) => {
      if (String(path).endsWith("/cancel")) return {};
      sessionPolls += 1;
      return { analysis_status: sessionPolls === 1 ? "running" : "completed" };
    });
    const { result, retryController } = setup();
    result.current.analysisController.current = new AbortController();
    retryController.current = new AbortController();
    const analysisSignal = result.current.analysisController.current.signal;
    const retrySignal = retryController.current.signal;

    await act(async () => {
      await result.current.stopAnalysis();
    });

    expect(apiMock.mock.calls[0][0]).toBe("/api/sessions/s1/cancel");
    expect(analysisSignal.aborted).toBe(true);
    expect(retrySignal.aborted).toBe(true);
    expect(mocks.wait).toHaveBeenCalledTimes(1);
    expect(useBusyStore.getState().stopping).toBe(false);
    expect(useAnalysisStore.getState().running).toBe(false);
    expect(useAnalysisStore.getState().currentNodeTitle).toBe("");
    expect(useRetryStore.getState().retryChecking).toBe(false);
    expect(useErrorStore.getState().error).toBe("分析已停止，已完成步骤的结果已保留。");
  });

  it("后端迟迟未退出（10 次轮询仍 running）时给出安全退出文案", async () => {
    useAnalysisStore.setState({ running: true });
    mocks.api.mockImplementation(async (path: string) =>
      String(path).endsWith("/cancel") ? {} : { analysis_status: "running" },
    );
    const { result } = setup();

    await act(async () => {
      await result.current.stopAnalysis();
    });

    expect(mocks.wait).toHaveBeenCalledTimes(10);
    expect(useErrorStore.getState().error).toBe(
      "已发送停止指令，后端会在当前模型调用结束后安全退出，已完成步骤不会丢失。",
    );
    expect(useBusyStore.getState().stopping).toBe(false);
  });

  it("未在运行或正在停止时不发 cancel 请求", async () => {
    const { result } = setup();
    await act(async () => {
      await result.current.stopAnalysis();
    });
    act(() => {
      useAnalysisStore.setState({ running: true });
      useBusyStore.setState({ stopping: true });
    });
    await act(async () => {
      await result.current.stopAnalysis();
    });
    expect(mocks.api).not.toHaveBeenCalled();
  });
});
