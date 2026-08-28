import "@testing-library/jest-dom";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// App 渲染冒烟测试：鉴权引导 → 主壳（侧栏 + 空工作台）完整挂载。
// 本轮把 App.tsx 的历史同步 / 会话操作 / 任务输入区 / Tab 导航拆分到
// 多个 hook 与组件后，需要一个端到端渲染测试兜住 hook 顺序与接线错误
//（此前只有 tsc / build，抓不住运行时的 hook 时序问题）。

// mock api 请求层（App 与 useAuthBootstrap/useHistorySync 共用同一模块）
const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

// EmptyWorkspace 的 DotField 用 canvas 2d 画点阵，jsdom 无 canvas 实现
// （getContext 返回 null），用 no-op 代理兜底。
beforeEach(() => {
  // jsdom 无 IntersectionObserver：stub 为"立即可见"。
  // framer-motion 的 whileInView（EmptyWorkspace）与自研 useInView 均依赖。
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
  // 兜底 fetch：正常路径全部走 mock 的 api()，若有遗漏调用方也不至于
  // 产生未处理 rejection
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({}), { status: 200 })),
    ),
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

describe("App 渲染冒烟", () => {
  it("鉴权免登时完整挂载主壳：侧栏 wordmark + 空工作台", async () => {
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
          langsmith_tracing: false,
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
    // 冲刷鉴权引导的 promise 链（auth → settings → authReady）
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // 鉴权就绪后：侧栏 wordmark 与空工作台标题出现
    expect(await screen.findByText("数据台")).toBeInTheDocument();
    // SplitText/ShinyText 会把标题拆成单字 span，用 textContent 匹配 h2
    expect(
      screen.getByText((_content, el) => el?.tagName === "H2" && el.textContent === "从一份数据开始"),
    ).toBeInTheDocument();
    // 历史列表拉取已被触发（鉴权通过后 fetchHistory）
    expect(apiMock).toHaveBeenCalledWith("/api/sessions?limit=30");
  });
});
