import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ReportView from "../ReportView";
import {
  useAnalysisStore,
  useAuthStore,
  useSessionStore,
} from "../../store/useAppStore";
import type { AnalysisResult, Session } from "../../types";

// App 级接线测试要 mock 请求层；ReportView 单测本身不发起网络请求。
const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

const REPORT_TEXT = [
  "# 销售分析报告",
  "",
  "销售额合计：17224.00 元",
  "销量合计：462 件",
  "订单数：462 笔",
].join("\n");

function makeResult(overrides: Partial<AnalysisResult> = {}): AnalysisResult {
  return {
    response: REPORT_TEXT,
    artifacts: [],
    plan: [],
    completed_steps: [],
    dataset_profile: {
      source: "sales.csv",
      rows: 21,
      columns: 8,
      row_count: 21,
      column_count: 8,
      column_info: [],
    },
    ...overrides,
  };
}

function resetStores() {
  useAuthStore.setState({
    authRequired: false,
    authenticated: false,
    authReady: false,
  });
  useSessionStore.setState({
    session: null,
    activeTab: "analysis",
    lastActiveTab: "analysis",
  });
  useAnalysisStore.setState({
    task: "",
    plan: [],
    completed: [],
    result: null,
    running: false,
    awaitingApproval: false,
    pendingObjective: "",
    stepProgress: null,
  });
}

beforeEach(() => {
  // jsdom 无 IntersectionObserver：stub 为"立即可见"（ReportView 目录与
  // App 内动效组件依赖）。
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
  // EmptyWorkspace 的 DotField 用 canvas 2d 画点阵，jsdom 无实现。
  const noopCtx = new Proxy(
    {},
    {
      get: (_target, prop) => {
        if (prop === "canvas") return undefined;
        return () => noopCtx;
      },
      set: () => true,
    },
  ) as unknown as CanvasRenderingContext2D;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(noopCtx);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  apiMock.mockReset();
  resetStores();
});

describe("ReportView 送审核验", () => {
  it("点击「送审并核验」时把报告正文交给 onAuditReport", () => {
    const onAuditReport = vi.fn();
    render(<ReportView result={makeResult()} onAuditReport={onAuditReport} />);

    fireEvent.click(screen.getByRole("button", { name: "送审并核验" }));

    expect(onAuditReport).toHaveBeenCalledTimes(1);
    expect(onAuditReport).toHaveBeenCalledWith(REPORT_TEXT);
  });

  it("展示 dataset_profile 中的真实来源文件名与行数", () => {
    render(<ReportView result={makeResult()} />);

    const badge = screen.getByText(/来源：/);
    expect(badge).toHaveTextContent("来源：sales.csv · 21 行");
  });

  it("dataset_profile 带 sha256 时展示真实指纹前缀", () => {
    const sha = "a".repeat(64);
    render(
      <ReportView
        result={makeResult({
          dataset_profile: {
            source: "sales.csv",
            rows: 21,
            row_count: 21,
            column_count: 8,
            column_info: [],
            sha256: sha,
          },
        })}
      />,
    );

    const badge = screen.getByText(/来源：/);
    expect(badge).toHaveTextContent(`SHA-256 ${sha.slice(0, 8)}…`);
    expect(badge).toHaveAttribute("title", `数据集 SHA-256：${sha}`);
  });

  it("没有来源字段时不编造，只显示会话数据集兜底", () => {
    render(<ReportView result={makeResult({ dataset_profile: undefined })} />);

    expect(screen.getByText("来源：当前会话数据集")).toBeInTheDocument();
  });

  it("流式生成期间不显示送审按钮，避免把半截报告送去核验", () => {
    const onAuditReport = vi.fn();
    render(<ReportView result={makeResult()} streaming onAuditReport={onAuditReport} />);

    expect(screen.queryByRole("button", { name: "送审并核验" })).not.toBeInTheDocument();
  });
});

describe("App 报告送审闭环", () => {
  it("点击送审后切到简报核验 Tab，并把报告正文注入 SalesAudit", async () => {
    const session: Session = {
      id: "s-report",
      filename: "sales.csv",
      preview: [],
      profile: {
        row_count: 21,
        column_count: 8,
        column_info: [],
        rows: 21,
        columns: 8,
      },
      artifacts: [],
      analysis_status: "completed",
    };

    useAuthStore.setState({
      authRequired: false,
      authenticated: true,
      authReady: true,
    });
    useSessionStore.setState({
      session,
      activeTab: "analysis",
      lastActiveTab: "analysis",
    });
    useAnalysisStore.setState({
      result: makeResult(),
      running: false,
      plan: [],
      completed: [],
    });

    apiMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/auth")) {
        return Promise.resolve({ required: false, authenticated: true });
      }
      if (url.startsWith("/api/settings")) {
        return Promise.resolve({
          provider: "deepseek",
          model: "deepseek-chat",
          configured: true,
          thinking_enabled: false,
          reasoning_effort: "medium",
          max_iterations: 24,
          max_plan_steps: 8,
          max_tool_calls_per_step: 6,
          langsmith_tracing: false,
          langsmith_status: "disabled",
          storage_status: "ok",
          max_upload_bytes: 52_428_800,
        });
      }
      if (url.startsWith("/api/sessions")) {
        return Promise.resolve({ sessions: [] });
      }
      return Promise.resolve({});
    });

    const { default: App } = await import("../../App");
    render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const sendButton = await screen.findByRole("button", { name: "送审并核验" });
    fireEvent.click(sendButton);

    // 1) 核验 Tab 被选中
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "简报核验" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );

    // 2) report prop 确实注入 SalesAudit：出现「使用分析报告草稿」按钮，
    //    且点击后用报告正文填充草稿（提取逻辑由 SalesAudit 自己负责）。
    const useDraft = await screen.findByRole("button", { name: "使用分析报告草稿" });
    fireEvent.click(useDraft);
    expect(screen.getByRole<HTMLTextAreaElement>("textbox", { name: "简报草稿" }))
      .toHaveValue(REPORT_TEXT);
  });
});
