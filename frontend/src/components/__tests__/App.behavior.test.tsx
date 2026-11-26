import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// App 行为测试：桩掉重型子组件与网络层，覆盖上传 / 示例 / 会话切换 /
// 命令面板 / 错误恢复 / 候选数据集 / Tab 与侧栏等交互分支。
const apiMock = vi.fn();
const uploadMock = vi.fn();

vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return {
    ...actual,
    api: (...args: unknown[]) => apiMock(...args),
    uploadWithProgress: (...args: unknown[]) => uploadMock(...args),
  };
});

// 轮询/批量下载里的固定等待在测试中立即返回，避免真实 3 秒等待。
vi.mock("../../utils/format", async () => {
  const actual = await vi.importActual<typeof import("../../utils/format")>("../../utils/format");
  return { ...actual, wait: () => Promise.resolve() };
});

vi.mock("../../components/AccessGate", () => ({
  AccessGate: () => <div data-testid="access-gate">access-gate</div>,
  Metric: () => null,
}));

// 重型/依赖 DOM 的子组件替换为可交互桩，把测试焦点放在 App 的接线逻辑。
vi.mock("../../components/EmptyWorkspace", () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="empty-workspace">
      <button type="button" onClick={() => (props.onUpload as () => void)()}>empty-upload</button>
      <button type="button" onClick={() => (props.onCancelUpload as () => void)()}>cancel-upload</button>
    </div>
  ),
}));
vi.mock("../../components/HistoryPanel", () => ({
  default: (props: Record<string, never> & { sessions?: { id: string }[]; onSelect?: (item: unknown) => void; onRefresh?: () => void }) => (
    <div data-testid="history-panel">
      {(props.sessions || []).map((item) => (
        <button key={item.id} type="button" onClick={() => props.onSelect?.(item)}>open-{item.id}</button>
      ))}
      <button type="button" onClick={() => props.onRefresh?.()}>refresh-history</button>
    </div>
  ),
}));
vi.mock("../../components/TaskBox", () => ({
  default: (props: {
    task?: string;
    running?: boolean;
    onTaskChange?: (value: string) => void;
    onRun?: () => void;
    onStop?: () => void;
    onFocusPlan?: () => void;
  }) => (
    <div data-testid="task-box">
      <input
        aria-label="task-input"
        value={props.task || ""}
        onChange={(event) => props.onTaskChange?.(event.target.value)}
      />
      <button type="button" onClick={props.onRun}>run-task</button>
      <button type="button" onClick={props.onStop}>stop-task</button>
      <button type="button" onClick={props.onFocusPlan}>focus-plan</button>
    </div>
  ),
}));
vi.mock("../../components/MetricsEditor", () => ({ default: () => <div data-testid="metrics-editor" /> }));
vi.mock("../../components/DatasetOverview", () => ({
  default: (props: { profile?: unknown }) => <div data-testid="dataset-overview">{JSON.stringify(props.profile)}</div>,
}));
vi.mock("../../components/CommentsPanel", () => ({
  default: () => <div data-testid="comments-panel" />,
}));
vi.mock("../../components/ConversationThread", () => ({  default: (props: {
    input?: string;
    onInputChange?: (value: string) => void;
    onSubmit?: () => void;
    onStop?: () => void;
  }) => (
    <div data-testid="conversation">
      <textarea
        aria-label="followup-input"
        value={props.input || ""}
        onChange={(event) => props.onInputChange?.(event.target.value)}
      />
      <button type="button" onClick={props.onSubmit}>submit-followup</button>
      <button type="button" onClick={props.onStop}>stop-followup</button>
    </div>
  ),
}));
vi.mock("../../components/PreviewModal", () => ({ default: () => <div data-testid="preview-modal" /> }));
vi.mock("../../components/DemoBrief", () => ({ default: () => <div data-testid="demo-brief" /> }));
vi.mock("../../components/SalesAudit", () => ({
  SALES_BRIEF_TASK: "核验任务",
  default: (props: { report?: string; onReloadSession?: (id: string) => void }) => (
    <div data-testid="sales-audit">
      {props.report}
      <button type="button" onClick={() => props.onReloadSession?.("s9")}>reload-audit</button>
    </div>
  ),
}));
vi.mock("../../components/ReportView", () => ({
  default: (props: {
    result?: { response?: string };
    onAuditReport?: (text: string) => void;
    onReplay?: () => void;
  }) => (
    <div data-testid="report-view">
      {props.result?.response}
      <button type="button" onClick={() => props.onAuditReport?.(props.result?.response || "")}>audit-report</button>
      <button type="button" onClick={() => props.onReplay?.()}>replay-report</button>
    </div>
  ),
}));
vi.mock("../../components/DataTable", () => ({ default: () => <div data-testid="data-table" /> }));
vi.mock("../../components/HelpPanel", () => ({
  default: (props: { onClose: () => void }) => (
    <div data-testid="help-panel"><button type="button" onClick={props.onClose}>close-help</button></div>
  ),
}));
vi.mock("../../components/SchedulePanel", () => ({
  default: (props: { onClose: () => void }) => (
    <div data-testid="schedule-panel"><button type="button" onClick={props.onClose}>close-schedules</button></div>
  ),
}));
vi.mock("../../components/CommandPalette", () => ({
  default: (props: {
    actions: { id: string; title: string }[];
    onAction: (action: unknown) => void;
    onClose: () => void;
    onSelectSession: (item: unknown) => void;
    sessions?: { id: string }[];
  }) => (
    <div data-testid="command-palette">
      {props.actions.map((action) => (
        <button key={action.id} type="button" onClick={() => props.onAction(action)}>{action.title}</button>
      ))}
      {(props.sessions || []).map((item) => (
        <button key={item.id} type="button" onClick={() => props.onSelectSession(item)}>palette-{item.id}</button>
      ))}
      <button type="button" onClick={props.onClose}>close-palette</button>
    </div>
  ),
}));vi.mock("../../components/ArtifactCenter", () => ({
  default: (props: {
    onAdoptDataset?: (name: string) => void;
    onDiscardDataset?: (name: string) => void;
    onRescan?: () => void;
  }) => (
    <div data-testid="artifact-center">
      <button type="button" onClick={() => props.onAdoptDataset?.("derived")}>adopt-dataset</button>
      <button type="button" onClick={() => props.onDiscardDataset?.("derived")}>discard-dataset</button>
      <button type="button" onClick={() => props.onRescan?.()}>rescan-artifacts</button>
    </div>
  ),
}));
vi.mock("../../components/PlanPanel", () => ({
  default: (props: {
    plan?: { id: string; title: string }[];
    currentNodeTitle?: string;
    toolTrace?: { name: string }[];
    awaitingApproval?: boolean;
    onApprovePlan?: (plan: unknown[]) => void;
    onCancelApproval?: () => void;
    onRerunFromStep?: (index: number) => void;
  }) => (
    <div data-testid="plan-panel">
      <span data-testid="plan-ids">{(props.plan || []).map((step) => step.id).join(",")}</span>
      <span data-testid="plan-node">{props.currentNodeTitle}</span>
      <span data-testid="plan-trace">{(props.toolTrace || []).map((item) => item.name).join(",")}</span>
      <button type="button" onClick={() => props.onApprovePlan?.(props.plan || [])}>approve-plan</button>
      <button type="button" onClick={() => props.onCancelApproval?.()}>cancel-plan</button>
      <button type="button" onClick={() => props.onRerunFromStep?.(0)}>rerun-step</button>
    </div>
  ),
}));

import App from "../../App";
import { useAnalysisStore, useAuthStore, useBusyStore, useChatStore, useConfigStore, useErrorStore, useHistoryStore, useKeyStore, useReasoningStore, useRetryStore, useSessionStore, useTimerStore, useToolTraceStore, useUIStore } from "../../store/useAppStore";
import { ApiError } from "../../utils/api";

const SETTINGS = {
  provider: "deepseek",
  model: "deepseek-chat",
  configured: false,
  thinking_enabled: false,
  reasoning_effort: "high",
  max_iterations: 24,
  max_plan_steps: 8,
  max_tool_calls_per_step: 6,
  storage_status: "ok",
  max_upload_bytes: 10 * 1024 * 1024,
};

const HISTORY = [
  { id: "s2", filename: "old.csv", analysis_status: "idle", has_result: false },
  { id: "s3", filename: "gone.csv", analysis_status: "idle", has_result: false },
];

function sessionPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "s1",
    filename: "sales.csv",
    preview: [{ region: "East", sales: 100 }],
    profile: {
      row_count: 1,
      column_count: 2,
      column_info: [
        { name: "region", dtype: "object", non_null: 1, unique: 1 },
        { name: "sales", dtype: "int64", non_null: 1, unique: 1 },
      ],
      rows: 1,
      columns: 2,
    },
    artifacts: [{ name: "柱状图_1.html", kind: "visualization", path: "/x", description: "" }],
    analysis_status: "idle",
    candidate_datasets: [{ name: "derived", rows: 3 }],
    ...overrides,
  };
}

function baseApi(url: string, options?: { method?: string; body?: string }) {
  if (url.startsWith("/api/auth")) return Promise.resolve({ required: false, authenticated: true });
  if (url.startsWith("/api/settings")) return Promise.resolve(SETTINGS);
  if (url.startsWith("/api/sessions?") || url === "/api/sessions") {
    return Promise.resolve({ sessions: HISTORY });
  }
  if (url === "/api/sessions/sample" || url.startsWith("/api/sessions/sample?")) {
    return Promise.resolve(sessionPayload());
  }
  if (url === "/api/sessions/s1") return Promise.resolve(sessionPayload());
  if (url === "/api/sessions/s2") {
    return Promise.resolve(
      sessionPayload({
        id: "s2",
        filename: "old.csv",
        kernel: {
          objective: "检查",
          plan: [{ id: "k1", title: "内核计划", instruction: "i", success_criteria: "s" }],
          plan_source: "model",
          completed_steps: [],
          current_node: "execute_step",
          status: "running",
          tool_trace: [{ call_id: "c1", name: "inspect_data", status: "done" }],
        },
      }),
    );
  }
  if (url === "/api/sessions/s3") {
    return Promise.reject(Object.assign(new Error("gone"), { status: 404 }));
  }
  if (url.includes("/datasets/adopt")) return Promise.resolve({});
  if (url.includes("/artifacts/rescan")) return Promise.resolve({});
  if (url.includes("/cancel")) return Promise.resolve({});
  return Promise.resolve({});
}

const useAppStoreForTest = () => useSessionStore.getState();

function resetStores() {  for (const store of [
    useAnalysisStore, useAuthStore, useBusyStore, useChatStore, useConfigStore,
    useErrorStore, useHistoryStore, useKeyStore, useReasoningStore, useRetryStore,
    useSessionStore, useTimerStore, useToolTraceStore, useUIStore,
  ] as { getInitialState: () => unknown; setState: (state: unknown, replace: true) => void }[]) {
    store.setState(store.getInitialState(), true);
  }
}

async function renderApp(handler: typeof baseApi = baseApi) {
  apiMock.mockReset();
  uploadMock.mockReset();
  apiMock.mockImplementation(handler);
  uploadMock.mockImplementation(() => Promise.resolve(sessionPayload()));
  const utils = render(<App />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  return utils;
}

async function loadSample() {
  await act(async () => {
    window.dispatchEvent(new CustomEvent("empty-workspace:load-sample", { detail: { kind: "sales" } }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  // jsdom 无 IntersectionObserver / canvas：与 App.smoke 保持同一套桩。
  class IntersectionObserverStub {
    callback: IntersectionObserverCallback;
    constructor(callback: IntersectionObserverCallback) {
      this.callback = callback;
    }
    observe(target: Element) {
      this.callback(
        [{ isIntersecting: true, target } as unknown as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", IntersectionObserverStub);
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))),
  );
  const noopCtx = new Proxy(
    {},
    {
      get: (_target, prop) => (prop === "canvas" ? undefined : () => noopCtx),
      set: () => true,
    },
  ) as unknown as CanvasRenderingContext2D;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(noopCtx);
  // SettingsPanel 使用原生 <dialog>.showModal/close，jsdom 未实现。
  if (!HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
  }
  resetStores();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("App 行为", () => {
  it("上传文件成功后进入会话；超限文件提前拦截", async () => {
    const { container } = await renderApp();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["region,sales\nEast,100\n"], "sales.csv", { type: "text/csv" });

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
      await Promise.resolve();
    });
    await waitFor(() => expect(uploadMock).toHaveBeenCalled());
    expect(uploadMock.mock.calls[0][0]).toBe("/api/sessions");
    expect(await screen.findByRole("heading", { name: "sales.csv" })).toBeInTheDocument();

    // 超过服务端上限：不发请求，直接提示
    await act(async () => {
      useConfigStore.setState({ settings: { ...SETTINGS, max_upload_bytes: 3 } });
    });
    uploadMock.mockClear();
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
      await Promise.resolve();
    });
    expect(uploadMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("超过");
    expect(useUIStore.getState().uploading).toBe(false);
  });

  it("加载示例走 sample 接口并切换到分析视图", async () => {
    await renderApp();
    await loadSample();
    expect(apiMock).toHaveBeenCalledWith("/api/sessions/sample?kind=sales", expect.objectContaining({ method: "POST" }));
    expect(await screen.findByRole("heading", { name: "sales.csv" })).toBeInTheDocument();
  });

  it("切换历史会话恢复内核计划与工具时间线", async () => {
    await renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "open-s2" }));
    expect(await screen.findByRole("heading", { name: "old.csv" })).toBeInTheDocument();
    expect(screen.getByTestId("plan-ids")).toHaveTextContent("k1");
    expect(screen.getByTestId("plan-node")).toHaveTextContent("执行步骤");
    expect(screen.getByTestId("plan-trace")).toHaveTextContent("inspect_data");
    expect(apiMock.mock.calls.some((call) => call[0] === "/api/sessions/s2")).toBe(true);
  });

  it("历史会话 404 时提示会话已清理并回到空态", async () => {
    const { container } = await renderApp((url, options) => {
      if (url === "/api/sessions/s3") {
        return Promise.reject(new ApiError("gone", 404));
      }
      return baseApi(url, options);
    });
    fireEvent.click(await screen.findByRole("button", { name: "open-s3" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("该历史会话已被服务端清理");
    expect(container.querySelector(".app-main.is-empty")).toBeTruthy();
  });

  it("命令面板动作：设置 / 定时任务 / 语言 / 帮助 / Tab", async () => {
    await renderApp();
    await loadSample();
    const openPalette = async () => {
      fireEvent.click(screen.getByRole("button", { name: /command palette|命令面板/ }));
      await screen.findByTestId("command-palette");
    };
    const inPalette = () => within(screen.getByTestId("command-palette"));

    await openPalette();
    fireEvent.click(inPalette().getByRole("button", { name: "打开模型设置" }));
    expect(useKeyStore.getState().keyOpen).toBe(true);

    await openPalette();
    fireEvent.click(inPalette().getByRole("button", { name: "定时任务" }));
    expect(await screen.findByTestId("schedule-panel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "close-schedules" }));

    await openPalette();
    fireEvent.click(inPalette().getByRole("button", { name: "切换语言" }));
    expect(useUIStore.getState().locale).toBe("en-US");
    // 英文模式下面板动作标题随 locale 重建
    await openPalette();
    fireEvent.click(inPalette().getByRole("button", { name: "Toggle language" }));
    expect(useUIStore.getState().locale).toBe("zh-CN");

    await openPalette();
    fireEvent.click(inPalette().getByRole("button", { name: "查看键盘快捷键" }));
    expect(await screen.findByTestId("help-panel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "close-help" }));

    // 切到产物 Tab：ArtifactCenter 渲染
    await openPalette();
    fireEvent.click(inPalette().getByRole("button", { name: "切换到产物视图" }));
    expect(await screen.findByTestId("artifact-center")).toBeInTheDocument();
  });

  it("错误横幅可展开/收起/关闭", async () => {
    const longMessage = "上传失败：".repeat(30);
    await renderApp((url, options) => {
      if (url === "/api/sessions/sample" || url.startsWith("/api/sessions/sample?")) {
        return Promise.reject(new Error(longMessage));
      }
      return baseApi(url, options);
    });
    await loadSample();
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("查看详情");
    fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭错误提示" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("恢复提示条：放弃恢复清空 retryOffer", async () => {
    await renderApp((url, options) => {
      if (url === "/api/sessions/sample" || url.startsWith("/api/sessions/sample?")) {
        return Promise.resolve(sessionPayload());
      }
      return baseApi(url, options);
    });
    await act(async () => {
      useRetryStore.setState({
        retryOffer: {
          task: "检查销售",
          reason: "cancelled",
          canResume: true,
          completed: [{ id: "inspect", title: "检查数据", status: "completed" }],
        },
      });
    });
    expect(await screen.findByText(/可以继续或重新运行/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放弃恢复" }));
    await waitFor(() => expect(useRetryStore.getState().retryOffer).toBeNull());
  });

  it("候选数据集采纳/忽略/重扫与失败提示", async () => {
    const { container } = await renderApp();
    await loadSample();
    fireEvent.click(screen.getByRole("tab", { name: /产物/ }));
    fireEvent.click(await screen.findByRole("button", { name: "adopt-dataset" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/sessions/s1/datasets/adopt",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(JSON.parse(apiMock.mock.calls.find((call) => String(call[0]).includes("adopt"))![1].body as string)).toEqual({ name: "derived" });

    fireEvent.click(screen.getByRole("button", { name: "discard-dataset" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/sessions/s1/datasets/adopt",
      expect.objectContaining({ method: "POST" }),
    ));

    fireEvent.click(screen.getByRole("button", { name: "rescan-artifacts" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/sessions/s1/artifacts/rescan",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(container.querySelector("#tabpanel-artifacts")).toBeTruthy();
  });

  it("候选数据集操作失败显示错误横幅", async () => {
    await renderApp((url, options) => {
      if (url.includes("/datasets/adopt")) return Promise.reject(new Error("写入失败"));
      return baseApi(url, options);
    });
    await loadSample();
    fireEvent.click(screen.getByRole("tab", { name: /产物/ }));
    fireEvent.click(await screen.findByRole("button", { name: "adopt-dataset" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("采纳候选数据集失败：写入失败");
  });

  it("Tab 切换与侧栏开关", async () => {
    const { container } = await renderApp();
    await loadSample();
    expect(screen.getByTestId("dataset-overview")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /数据/ }));
    expect(await screen.findByTestId("data-table")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /简报核验/ }));
    expect(screen.getByTestId("sales-audit")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /评论/ }));
    expect(container.querySelector("#tabpanel-comments")).toBeTruthy();

    const toggle = screen.getByRole("button", { name: "打开侧边栏" });
    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "关闭侧边栏" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(window, { key: "Escape" });
  });

  it("命令面板内选择历史会话", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "打开命令面板" }));
    fireEvent.click(screen.getByRole("button", { name: "palette-s2" }));
    expect(await screen.findByRole("heading", { name: "old.csv" })).toBeInTheDocument();
  });

  it("渲染结果时展示报告并支持送审", async () => {    await renderApp((url, options) => {
      if (url === "/api/sessions/s2") {
        return Promise.resolve(
          sessionPayload({
            id: "s2",
            filename: "old.csv",
            last_result: {
              response: "历史报告",
              artifacts: [],
              plan: [],
              completed_steps: [],
            },
          }),
        );
      }
      return baseApi(url, options);
    });
    fireEvent.click(await screen.findByRole("button", { name: "open-s2" }));
    expect(await screen.findByTestId("report-view")).toHaveTextContent("历史报告");
    fireEvent.click(screen.getByRole("button", { name: "audit-report" }));
    expect(await screen.findByTestId("sales-audit")).toHaveTextContent("历史报告");
  });

  it("启动分析：流式请求发出后 running 复位", async () => {
    const streamFetch = vi.fn((..._args: unknown[]) =>
      Promise.resolve(new Response(": keep-alive\n\n", { status: 200 })),
    );
    vi.stubGlobal("fetch", streamFetch);
    await renderApp();
    await loadSample();
    await act(async () => {
      fireEvent.change(screen.getByLabelText("task-input"), { target: { value: "检查销售" } });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "run-task" }));
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(streamFetch.mock.calls.some((call) => String(call[0]).includes("/analyze/stream"))).toBe(true),
    );
    await waitFor(() => expect(useAnalysisStore.getState().running).toBe(false));
  });

  it("停止分析：发送 cancel 请求并进入 stopping", async () => {
    await renderApp();
    await loadSample();
    await act(async () => {
      useAnalysisStore.setState({ running: true, task: "检查销售" });
    });
    fireEvent.click(screen.getByRole("button", { name: "stop-task" }));
    await waitFor(() =>
      expect(apiMock.mock.calls.some((call) => String(call[0]).includes("/cancel"))).toBe(true),
    );
  });

  it("审批计划与按步骤重跑都会重新发起流式分析", async () => {
    const streamFetch = vi.fn((..._args: unknown[]) =>
      Promise.resolve(new Response(": keep-alive\n\n", { status: 200 })),
    );
    vi.stubGlobal("fetch", streamFetch);
    await renderApp((url, options) => {
      if (url === "/api/sessions/s2") {
        return Promise.resolve(
          sessionPayload({
            id: "s2",
            filename: "old.csv",
            kernel: {
              objective: "检查",
              plan: [{ id: "k1", title: "内核计划", instruction: "i", success_criteria: "s" }],
              plan_source: "model",
              completed_steps: [{ id: "k1", title: "内核计划", status: "completed" }],
              current_node: "replan",
              status: "running",
              tool_trace: [],
            },
          }),
        );
      }
      return baseApi(url, options);
    });
    fireEvent.click(await screen.findByRole("button", { name: "open-s2" }));
    await screen.findByRole("heading", { name: "old.csv" });
    await act(async () => {
      useAnalysisStore.setState({ awaitingApproval: true, task: "检查销售" });
    });
    fireEvent.click(screen.getByRole("button", { name: "approve-plan" }));
    await waitFor(() =>
      expect(streamFetch.mock.calls.some((call) => String(call[0]).includes("/analyze/stream"))).toBe(true),
    );
    await waitFor(() => expect(useAnalysisStore.getState().running).toBe(false));

    streamFetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "rerun-step" }));
    await waitFor(() =>
      expect(streamFetch.mock.calls.some((call) => String(call[0]).includes("/analyze/stream"))).toBe(true),
    );
  });

  it("追问：提交后请求 chat 流", async () => {
    const streamFetch = vi.fn((..._args: unknown[]) =>
      Promise.resolve(new Response(": keep-alive\n\n", { status: 200 })),
    );
    vi.stubGlobal("fetch", streamFetch);
    await renderApp((url, options) => {
      if (url === "/api/sessions/s2") {
        return Promise.resolve(
          sessionPayload({
            id: "s2",
            filename: "old.csv",
            last_result: {
              response: "历史报告",
              artifacts: [],
              plan: [],
              completed_steps: [],
            },
          }),
        );
      }
      return baseApi(url, options);
    });
    fireEvent.click(await screen.findByRole("button", { name: "open-s2" }));
    await screen.findByTestId("conversation");
    await act(async () => {
      fireEvent.change(screen.getByLabelText("followup-input"), { target: { value: "继续深入" } });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "submit-followup" }));
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(streamFetch.mock.calls.some((call) => String(call[0]).includes("/chat/stream"))).toBe(true),
    );
  });

  // ---- 恢复提示条：retryAnalysis / resumeAnalysis 各分支 ----

  it("检查状态：轮询到完成后恢复结果并清空提示", async () => {
    let gets = 0;
    await renderApp((url, options) => {
      if (url === "/api/sessions/s1") {
        gets += 1;
        if (gets === 1) {
          return Promise.resolve({ ...sessionPayload(), analysis_status: "running", analysis_started_at: 1 });
        }
        return Promise.resolve(
          sessionPayload({
            analysis_status: "completed",
            last_result: { response: "恢复后的报告", artifacts: [], plan: [], completed_steps: [] },
          }),
        );
      }
      return baseApi(url, options);
    });
    await loadSample();
    await act(async () => {
      useRetryStore.setState({
        retryOffer: { task: "检查销售", reason: "cancelled", canResume: false },
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "检查状态" }));
    expect(await screen.findByTestId("report-view")).toHaveTextContent("恢复后的报告");
    await waitFor(() => expect(useRetryStore.getState().retryOffer).toBeNull());
    expect(gets).toBeGreaterThanOrEqual(2);
  });

  it("检查状态：已取消的分析给出可重跑提示", async () => {
    await renderApp((url, options) => {
      if (url === "/api/sessions/s1") {
        return Promise.resolve({ ...sessionPayload(), analysis_status: "cancelled" });
      }
      return baseApi(url, options);
    });
    await loadSample();
    await act(async () => {
      useRetryStore.setState({
        retryOffer: { task: "检查销售", reason: "network", canResume: false },
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "检查状态" }));
    await waitFor(() =>
      expect(useErrorStore.getState().error).toContain("原分析已经取消"),
    );
    expect(useRetryStore.getState().retryOffer?.reason).toBe("ready");
  });

  it("检查状态：404 视为会话被清理，网络错误提示稍后再试", async () => {
    const first = await renderApp((url, options) => {
      if (url === "/api/sessions/s1") return Promise.reject(new ApiError("gone", 404));
      return baseApi(url, options);
    });
    await loadSample();
    await act(async () => {
      useRetryStore.setState({
        retryOffer: { task: "检查销售", reason: "network", canResume: false },
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "检查状态" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("会话已失效");
    expect(useRetryStore.getState().retryOffer).toBeNull();
    first.unmount();

    // 网络错误分支：保留提示并提示稍后再试
    const second = await renderApp((url, options) => {
      if (url === "/api/sessions/s1") return Promise.reject(new Error("network down"));
      return baseApi(url, options);
    });
    await loadSample();
    await act(async () => {
      useRetryStore.setState({
        retryOffer: { task: "检查销售", reason: "network", canResume: false },
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "检查状态" }));
    await waitFor(() =>
      expect(useErrorStore.getState().error).toContain("暂时无法确认任务状态"),
    );
    second.unmount();
  });

  it("重新运行与继续分析都会发起流式请求", async () => {
    const streamFetch = vi.fn((..._args: unknown[]) =>
      Promise.resolve(new Response(": keep-alive\n\n", { status: 200 })),
    );
    vi.stubGlobal("fetch", streamFetch);
    await renderApp();
    await loadSample();

    await act(async () => {
      useRetryStore.setState({
        retryOffer: { task: "检查销售", reason: "ready", canResume: false },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新运行" }));
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(streamFetch.mock.calls.some((call) => String(call[0]).includes("/analyze/stream"))).toBe(true),
    );
    await waitFor(() => expect(useAnalysisStore.getState().running).toBe(false));

    streamFetch.mockClear();
    await act(async () => {
      useRetryStore.setState({
        retryOffer: {
          task: "检查销售",
          reason: "cancelled",
          canResume: true,
          plan: [{ id: "p1", title: "检查", instruction: "x", success_criteria: "y" }],
          completed: [{ id: "p1", title: "检查", status: "completed" }],
        },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "继续分析" }));
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(streamFetch.mock.calls.some((call) => String(call[0]).includes("/analyze/stream"))).toBe(true),
    );
  });

  it("重放上次分析写入会话与结果", async () => {
    await renderApp((url, options) => {
      if (url === "/api/sessions/s2/replay") {
        return Promise.resolve({ response: "重放报告", artifacts: [], plan: [], completed_steps: [] });
      }
      if (url === "/api/sessions/s2") {
        return Promise.resolve(
          sessionPayload({
            id: "s2",
            filename: "old.csv",
            last_result: { response: "历史报告", artifacts: [], plan: [], completed_steps: [] },
          }),
        );
      }
      return baseApi(url, options);
    });
    fireEvent.click(await screen.findByRole("button", { name: "open-s2" }));
    expect(await screen.findByTestId("report-view")).toHaveTextContent("历史报告");
    fireEvent.click(screen.getByRole("button", { name: "replay-report" }));
    await waitFor(() =>
      expect(apiMock.mock.calls.some((call) => String(call[0]).includes("/replay"))).toBe(true),
    );
    expect(await screen.findByTestId("report-view")).toHaveTextContent("重放报告");
  });

  it("核验面板重新载入示例会话；失败时提示", async () => {
    await renderApp((url, options) => {
      if (url === "/api/sessions/s9") {
        return Promise.resolve(sessionPayload({ id: "s9", filename: "audit.csv" }));
      }
      return baseApi(url, options);
    });
    await loadSample();
    await act(async () => {
      useAppStoreForTest().setActiveTab("audit");
    });
    fireEvent.click(screen.getAllByRole("button", { name: "reload-audit" })[0]);
    expect(await screen.findByRole("heading", { name: "audit.csv" })).toBeInTheDocument();

    const second = await renderApp((url, options) => {
      if (url === "/api/sessions/s9") return Promise.reject(new Error("加载失败"));
      return baseApi(url, options);
    });
    await loadSample();
    await act(async () => {
      useAppStoreForTest().setActiveTab("audit");
    });
    fireEvent.click(screen.getAllByRole("button", { name: "reload-audit" })[0]);
    await waitFor(() =>
      expect(
        screen.getAllByRole("alert").some((el) => el.textContent?.includes("无法载入示例会话")),
      ).toBe(true),
    );
    second.unmount();
  });

  it("取消计划审批与定位计划面板", async () => {
    await renderApp();
    await loadSample();
    await act(async () => {
      useAnalysisStore.setState({
        plan: [{ id: "p1", title: "检查", instruction: "x", success_criteria: "y" }],
        awaitingApproval: true,
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "cancel-plan" }));
    expect(useAnalysisStore.getState().awaitingApproval).toBe(false);
    expect(useAnalysisStore.getState().plan).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "focus-plan" }));
    expect(useAppStoreForTest().activeTab).toBe("analysis");
  });

  it("需要访问码时展示访问门禁", async () => {
    await renderApp((url, options) => {
      if (url.startsWith("/api/auth")) return Promise.resolve({ required: true, authenticated: false });
      return baseApi(url, options);
    });
    expect(await screen.findByTestId("access-gate")).toBeInTheDocument();
  });

  it("侧栏滑动手势关闭抽屉", async () => {
    const { container } = await renderApp();
    await loadSample();
    fireEvent.click(screen.getByRole("button", { name: "打开侧边栏" }));
    const sidebar = container.querySelector("aside") as HTMLElement;
    fireEvent.touchStart(sidebar, { touches: [{ clientX: 300, clientY: 20 }] });
    fireEvent.touchEnd(sidebar, { changedTouches: [{ clientX: 120, clientY: 24 }] });
    expect(screen.getByRole("button", { name: "打开侧边栏" })).toHaveAttribute("aria-expanded", "false");
  });

  it("上传取消与 AbortError 不显示为失败", async () => {
    const { container } = await renderApp();
    await act(async () => {
      useUIStore.setState({ uploading: true });
    });
    fireEvent.click(screen.getByRole("button", { name: "cancel-upload" }));

    uploadMock.mockRejectedValueOnce(new DOMException("Aborted", "AbortError"));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, {
        target: { files: [new File(["a,b\n1,2\n"], "abort.csv", { type: "text/csv" })] },
      });
      await Promise.resolve();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("已取消上传");
    expect(useUIStore.getState().uploading).toBe(false);
  });

  it("命令面板切换分析/数据/评论视图", async () => {
    await renderApp();
    await loadSample();
    const inPalette = () => within(screen.getByTestId("command-palette"));
    const open = async () => {
      fireEvent.click(screen.getByRole("button", { name: "打开命令面板" }));
      await screen.findByTestId("command-palette");
    };
    await open();
    fireEvent.click(inPalette().getByRole("button", { name: "切换到数据视图" }));
    expect(await screen.findByTestId("data-table")).toBeInTheDocument();
    await open();
    fireEvent.click(inPalette().getByRole("button", { name: "切换到评论视图" }));
    expect(useAppStoreForTest().activeTab).toBe("comments");
    await open();
    fireEvent.click(inPalette().getByRole("button", { name: "切换到分析视图" }));
    expect(useAppStoreForTest().activeTab).toBe("analysis");
  });

  it("忽略候选数据集与重扫产物失败时提示", async () => {
    await renderApp((url, options) => {
      if (url.includes("/datasets/adopt")) return Promise.reject(new Error("拒绝写入"));
      if (url.includes("/artifacts/rescan")) return Promise.reject(new Error("磁盘只读"));
      return baseApi(url, options);
    });
    await loadSample();
    fireEvent.click(screen.getByRole("tab", { name: /产物/ }));
    fireEvent.click(await screen.findByRole("button", { name: "discard-dataset" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("忽略候选数据集失败：拒绝写入");
    fireEvent.click(screen.getByRole("button", { name: "rescan-artifacts" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("扫描产物目录失败：磁盘只读");
  });
});
