import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// App 级无障碍：跳过导航、侧栏 aria-expanded、体验简报载入后的焦点落点。
const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

beforeEach(() => {
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
});

describe("App 无障碍主流程", () => {
  it(
    "跳过导航直达主内容、侧栏 aria-expanded 正确、体验简报载入后焦点进入核验面板",
    async () => {
      apiMock.mockImplementation((url: string) => {
        if (url.startsWith("/api/auth")) {
          return Promise.resolve({ required: false, authenticated: true });
        }
        if (url.startsWith("/api/settings")) {
          return Promise.resolve({
            provider: "deepseek",
            model: "deepseek-chat",
            configured: false,
            thinking_enabled: false,
            reasoning_effort: "medium",
            max_iterations: 24,
            max_plan_steps: 8,
            max_tool_calls_per_step: 6,
            langsmith_tracing: true,
            langsmith_status: "missing_key",
            storage_status: "ok",
            max_upload_bytes: 52_428_800,
          });
        }
        if (url === "/api/sessions/sample?audit=true" || url === "/api/sessions/sample?kind=sales_audit") {
          return Promise.resolve({
            id: "sample-audit",
            filename: "sample_sales_audit.csv",
            preview: [],
            profile: { rows: 21, columns: 8 },
            artifacts: [],
            analysis_status: "idle",
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
      await screen.findByText("数据台");

      // 1) 跳过导航：链接可聚焦，激活后主内容获得焦点。
      const skip = screen.getByRole("link", { name: "跳到主内容" });
      expect(skip).toHaveAttribute("href", "#main-content");
      const main = screen.getByRole("main");
      expect(main).toHaveAttribute("id", "main-content");
      fireEvent.click(skip);
      expect(main).toHaveFocus();

      // 2) 顶栏 icon-only 按钮都有可读名称。
      expect(screen.getByRole("button", { name: "打开命令面板" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "查看键盘快捷键" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "切换主题" })).toBeInTheDocument();

      // 侧栏折叠按钮：aria-expanded / aria-controls 随状态同步。
      const openToggle = screen.getByRole("button", { name: "打开侧边栏" });
      expect(openToggle).toHaveAttribute("aria-expanded", "false");
      expect(openToggle).toHaveAttribute("aria-controls", "app-sidebar");
      fireEvent.click(openToggle);
      const closeToggle = screen.getByRole("button", { name: "关闭侧边栏" });
      expect(closeToggle).toHaveAttribute("aria-expanded", "true");
      fireEvent.click(closeToggle);
      expect(
        screen.getByRole("button", { name: "打开侧边栏" }),
      ).toHaveAttribute("aria-expanded", "false");

      // 3) 体验简报核验按钮是原生可聚焦按钮（Tab/Enter 可达），激活后
      //    自动切到「简报核验」Tab，焦点被送到该面板而不是丢到 body。
      const auditButton = screen.getByRole("button", { name: "体验简报核验" });
      expect(auditButton).not.toBeDisabled();
      expect(auditButton).toHaveAttribute("aria-describedby", "empty-audit-hint");
      expect(auditButton.tabIndex).toBe(0);
      fireEvent.click(auditButton);

      expect(
        await screen.findByRole("tab", { name: "简报核验", selected: true }),
      ).toBeInTheDocument();
      const auditPanel = screen.getByRole("tabpanel", { name: "简报核验" });
      await waitFor(() => expect(auditPanel).toHaveFocus());

      // 4) 载入结果通过 role=status 的 live region 播报。
      const liveMessage = screen.getByText(/体验简报已载入，已切换到简报核验视图/);
      expect(liveMessage.closest('[role="status"]')).not.toBeNull();
    },
    90000,
  );
});
