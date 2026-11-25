import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";

// useSessionMutations 行为测试：导入 / 删除 / 重命名历史会话三条写路径。
// 覆盖：成功路径的副作用（刷新历史、切换会话、清空当前会话）、HTTP 错误的
// 两种响应体（JSON detail / 整页文本）、当前会话删除时三条在途请求的中断、
// 以及重命名失败时把用户输入的新名字带进提示。

import useSessionMutations from "../useSessionMutations";
import { useAnalysisStore } from "../../store/useAnalysisStore";
import { useChatStore } from "../../store/useChatStore";
import { useErrorStore } from "../../store/useErrorStore";
import { useHistoryStore } from "../../store/useHistoryStore";
import { useRetryStore } from "../../store/useRetryStore";
import { useSessionStore } from "../../store/useSessionStore";
import { useUIStore } from "../../store/useUIStore";
import { API_URL } from "../../constants";
import type { HistorySessionItem, Session } from "../../types";

interface FakeResponseInit {
  ok?: boolean;
  status?: number;
  contentType?: string;
  jsonBody?: unknown;
  text?: string;
}

function fakeResponse(init: FakeResponseInit = {}): Response {
  const contentType = init.contentType ?? "application/json";
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    headers: {
      get: (name: string) =>
        name.toLowerCase() === "content-type" ? contentType : null,
    },
    json: async () => init.jsonBody ?? {},
    text: async () => init.text ?? "",
  } as unknown as Response;
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    filename: "sales.csv",
    preview: [],
    profile: { row_count: 0, column_count: 0, column_info: [] },
    artifacts: [],
    analysis_status: "completed",
    ...overrides,
  };
}

function historyItem(id = "s1"): HistorySessionItem {
  return { id, filename: "sales.csv", analysis_status: "completed" };
}

function makeControllers() {
  return {
    retryController: createRef<AbortController | null>(),
    analysisController: createRef<AbortController | null>(),
    chatControllerRef: createRef<AbortController | null>(),
  };
}

function setup(overrides: { selectSession?: () => Promise<void>; fetchHistory?: () => Promise<void> } = {}) {
  const selectSession = vi.fn(overrides.selectSession ?? (async () => {}));
  const fetchHistory = vi.fn(overrides.fetchHistory ?? (async () => {}));
  const controllers = makeControllers();
  const { result } = renderHook(() =>
    useSessionMutations({
      selectSession,
      fetchHistory,
      retryController: controllers.retryController,
      analysisController: controllers.analysisController,
      chatControllerRef: controllers.chatControllerRef,
    }),
  );
  return { result, selectSession, fetchHistory, ...controllers };
}

function resetStores() {
  useSessionStore.setState({ session: null });
  useAnalysisStore.setState({
    task: "",
    plan: [],
    completed: [],
    result: null,
    running: false,
    awaitingApproval: false,
    pendingObjective: "",
    stepProgress: null,
    currentNodeTitle: "",
  });
  useHistoryStore.setState({ history: [] });
  useChatStore.setState({ followUps: [] });
  useRetryStore.setState({ retryOffer: null });
  useUIStore.setState({ uploading: false });
  useErrorStore.setState({ error: "", errorExpanded: false });
}

beforeEach(() => {
  resetStores();
});

describe("useSessionMutations 导入会话", () => {
  it("上传成功后刷新历史并切到新会话", async () => {
    const payload = makeSession({ id: "imported", filename: "imported.zip" });
    const fetchMock = vi.fn(async () => fakeResponse({ jsonBody: payload }));
    vi.stubGlobal("fetch", fetchMock);
    const { result, selectSession, fetchHistory } = setup();

    await act(async () => {
      await result.current.importSession(new File(["zip-bytes"], "imported.zip"));
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}/api/sessions/import`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("file")).toBeInstanceOf(File);
    expect(fetchHistory).toHaveBeenCalledTimes(1);
    expect(selectSession).toHaveBeenCalledWith({
      id: "imported",
      filename: "imported.zip",
      analysis_status: "completed",
    });
    expect(useUIStore.getState().uploading).toBe(false);
    expect(useErrorStore.getState().error).toBe("");
    vi.unstubAllGlobals();
  });

  it("HTTP 错误时透传服务端 detail，并复位上传态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        fakeResponse({ ok: false, status: 413, jsonBody: { detail: "文件超过大小限制" } }),
      ),
    );
    const { result, selectSession, fetchHistory } = setup();

    await act(async () => {
      await result.current.importSession(new File(["x"], "big.zip"));
    });

    expect(useUIStore.getState().uploading).toBe(false);
    expect(useErrorStore.getState().error).toBe("导入会话失败：文件超过大小限制");
    expect(selectSession).not.toHaveBeenCalled();
    expect(fetchHistory).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("上传期间 uploading 为 true（失败也能复位）", async () => {
    let rejectFetch: ((err: unknown) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((_resolve, reject) => {
            rejectFetch = reject;
          }),
      ),
    );
    const { result } = setup();

    let pending: Promise<void> | undefined;
    act(() => {
      pending = result.current.importSession(new File(["x"], "a.zip"));
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(useUIStore.getState().uploading).toBe(true);

    await act(async () => {
      rejectFetch?.(new Error("磁盘已满"));
      await pending;
    });
    expect(useUIStore.getState().uploading).toBe(false);
    expect(useErrorStore.getState().error).toBe("导入会话失败：磁盘已满");
    vi.unstubAllGlobals();
  });
});

describe("useSessionMutations 删除会话", () => {
  it("删除当前会话：清空所有前端状态并中断三条在途请求", async () => {
    useSessionStore.setState({ session: makeSession({ id: "s1" }) });
    useAnalysisStore.setState({
      result: { response: "旧报告", artifacts: [], plan: [], completed_steps: [] },
      plan: [{ id: "1", title: "步骤", instruction: "执行", success_criteria: "完成" }],
      completed: [{ id: "1", title: "完成", status: "completed" }],
      currentNodeTitle: "正在分析",
      awaitingApproval: true,
      pendingObjective: "目标",
      stepProgress: { progress: 10, toolCalls: 1, message: "", stepIndex: 0, totalSteps: 2 },
      task: "分析销售",
    });
    useChatStore.setState({
      followUps: [{ role: "user", content: "追问" }],
    });
    useRetryStore.setState({ retryOffer: { task: "重试" } });

    const controllers = makeControllers();
    controllers.retryController.current = new AbortController();
    controllers.analysisController.current = new AbortController();
    controllers.chatControllerRef.current = new AbortController();
    const selectSession = vi.fn(async () => {});
    const fetchHistory = vi.fn(async () => {});
    const { result } = renderHook(() =>
      useSessionMutations({ selectSession, fetchHistory, ...controllers }),
    );
    vi.stubGlobal("fetch", vi.fn(async () => fakeResponse()));

    await act(async () => {
      await result.current.deleteSession(historyItem("s1"));
    });

    expect(useSessionStore.getState().session).toBeNull();
    expect(useAnalysisStore.getState()).toMatchObject({
      result: null,
      plan: [],
      completed: [],
      currentNodeTitle: "",
      awaitingApproval: false,
      pendingObjective: "",
      stepProgress: null,
      task: "",
    });
    expect(useChatStore.getState().followUps).toEqual([]);
    expect(useRetryStore.getState().retryOffer).toBeNull();
    expect(controllers.retryController.current?.signal.aborted).toBe(true);
    expect(controllers.analysisController.current?.signal.aborted).toBe(true);
    expect(controllers.chatControllerRef.current?.signal.aborted).toBe(true);
    expect(fetchHistory).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("删除非当前会话：保留当前会话状态，只刷新历史", async () => {
    const current = makeSession({ id: "keep" });
    useSessionStore.setState({ session: current });
    useAnalysisStore.setState({ task: "正在看的问题" });
    const { result, fetchHistory } = setup();
    vi.stubGlobal("fetch", vi.fn(async () => fakeResponse()));

    await act(async () => {
      await result.current.deleteSession(historyItem("other"));
    });

    expect(useSessionStore.getState().session).toEqual(current);
    expect(useAnalysisStore.getState().task).toBe("正在看的问题");
    expect(fetchHistory).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("删除失败且返回整页文本时给出可读的截断提示", async () => {
    const { result, fetchHistory } = setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        fakeResponse({
          ok: false,
          status: 502,
          contentType: "text/html",
          text: "<html>Bad Gateway</html>",
        }),
      ),
    );

    await act(async () => {
      await result.current.deleteSession(historyItem("s1"));
    });

    expect(useErrorStore.getState().error).toBe("删除会话失败：<html>Bad Gateway</html>");
    expect(fetchHistory).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("useSessionMutations 重命名会话", () => {
  it("成功后同步 history 列表与当前会话标题", async () => {
    const current = makeSession({ id: "s1" });
    useSessionStore.setState({ session: current });
    useHistoryStore.setState({
      history: [historyItem("s1"), historyItem("s2")],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => fakeResponse({ jsonBody: { title: "华东专题" } })),
    );
    const { result } = setup();

    await act(async () => {
      await result.current.renameSession(historyItem("s1"), "华东专题");
    });

    expect(useHistoryStore.getState().history).toMatchObject([
      { id: "s1", title: "华东专题" },
      { id: "s2" },
    ]);
    expect(useSessionStore.getState().session?.title).toBe("华东专题");
    vi.unstubAllGlobals();
  });

  it("空标题视为清除自定义标题（回退 filename）", async () => {
    useSessionStore.setState({ session: makeSession({ id: "s1", title: "旧标题" }) });
    useHistoryStore.setState({ history: [historyItem("s1")] });
    vi.stubGlobal("fetch", vi.fn(async () => fakeResponse({ jsonBody: { title: "" } })));
    const { result } = setup();

    await act(async () => {
      await result.current.renameSession(historyItem("s1"), "");
    });

    expect(useHistoryStore.getState().history[0].title).toBeUndefined();
    expect(useSessionStore.getState().session?.title).toBeUndefined();
    vi.unstubAllGlobals();
  });

  it("重命名非当前会话时不改当前 session，只更新历史列表", async () => {
    const current = makeSession({ id: "current", title: "当前" });
    useSessionStore.setState({ session: current });
    useHistoryStore.setState({ history: [historyItem("other")] });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => fakeResponse({ jsonBody: { title: "新名字" } })),
    );
    const { result } = setup();

    await act(async () => {
      await result.current.renameSession(historyItem("other"), "新名字");
    });

    expect(useHistoryStore.getState().history[0].title).toBe("新名字");
    expect(useSessionStore.getState().session).toEqual(current);
    vi.unstubAllGlobals();
  });

  it("失败提示包含用户输入的新名字与失败原因", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        fakeResponse({ ok: false, status: 400, jsonBody: { detail: "标题过长" } }),
      ),
    );
    const { result } = setup();

    await act(async () => {
      await result.current.renameSession(historyItem("s1"), "一个特别长的标题");
    });

    expect(useErrorStore.getState().error).toBe(
      "重命名会话失败，新名称「一个特别长的标题」未保存，可重试：标题过长",
    );
    vi.unstubAllGlobals();
  });
});
