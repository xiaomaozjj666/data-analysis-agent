import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ReportView from "../ReportView";
import { useErrorStore } from "../../store/useAppStore";
import type { AnalysisResult } from "../../types";

// ReportView 阅读辅助与导出路径测试（与 ReportView.test.tsx 互补）。
// 覆盖：复制反馈（成功/失败/自动复位）、目录生成与跳转、核验附录折叠、
// 流式占位/光标、按配方重跑、来源徽章字段校验，以及导出菜单的 Markdown /
// 自包含 HTML / 打印 / Office / 数据画像仪表盘 / 溯源 JSON 全部路径
// （含服务端失败提示与无会话时的菜单裁剪）。导出统一用 Blob 替身捕获内容，
// 不产生真实下载或网络请求。

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
      column_count: 8,
      column_info: [],
      row_count: 21,
    },
    ...overrides,
  };
}

function stubObjectUrl() {
  const blobs: Blob[] = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn((blob: Blob) => {
      blobs.push(blob);
      return `blob:mock-${blobs.length}`;
    }),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(),
  });
  const clicks: HTMLAnchorElement[] = [];
  const clickSpy = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this);
    });
  return { blobs, clicks, clickSpy };
}

function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    writable: true,
    value: { writeText: vi.fn(writeText) },
  });
}

// 导出成功后菜单自动关闭，失败时保持打开（便于重试）——按需打开，避免
// 在已打开时再点一次触发把菜单切回关闭。
function openExportMenu() {
  if (!screen.queryByRole("menu")) {
    fireEvent.click(screen.getByRole("button", { name: "导出" }));
  }
}

beforeEach(() => {
  useErrorStore.setState({ error: "", errorExpanded: false });
  // jsdom 无 IntersectionObserver：stub 为"立即可见"（目录高亮依赖）
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
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ReportView 阅读辅助", () => {
  it("复制成功显示「已复制」并在 1.8s 后复位，复制内容为报告原文", async () => {
    vi.useFakeTimers();
    const written: string[] = [];
    stubClipboard(async (text) => {
      written.push(text);
    });
    render(<ReportView result={makeResult()} />);

    fireEvent.click(screen.getByRole("button", { name: "复制报告全文" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(written).toEqual([REPORT_TEXT]);
    expect(screen.getByText("已复制")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1800);
    });
    expect(screen.getByRole("button", { name: "复制报告全文" })).toBeInTheDocument();
    expect(screen.queryByText("已复制")).not.toBeInTheDocument();
  });

  it("复制失败显示「复制失败」并在 3s 后复位，不假装成功", async () => {
    vi.useFakeTimers();
    stubClipboard(async () => {
      throw new Error("clipboard blocked");
    });
    render(<ReportView result={makeResult()} />);

    fireEvent.click(screen.getByRole("button", { name: "复制报告全文" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("复制失败")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.queryByText("复制失败")).not.toBeInTheDocument();
  });

  it("超过 3 个标题时生成目录，点击条目展开报告并滚动到对应标题", async () => {
    const scrollSpy = vi.fn();
    const originalScroll = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollSpy;
    try {
      const response = [
        "# 经营分析",
        "",
        "## 数据概览",
        "内容一",
        "## 区域对比",
        "内容二",
        "## 异常诊断",
        "内容三",
        "## 结论建议",
        "内容四",
      ].join("\n");
      render(<ReportView result={makeResult({ response })} />);

      const toc = await screen.findByRole("navigation", { name: "报告目录" });
      expect(toc).toHaveTextContent("数据概览");
      expect(toc).toHaveTextContent("结论建议");
      // 3 个及以下标题不显示目录（本用例同时验证阈值）
      expect(screen.getByRole("button", { name: /展开完整报告/ })).toHaveAttribute(
        "aria-expanded",
        "false",
      );

      fireEvent.click(screen.getByRole("button", { name: "区域对比" }));
      expect(screen.getByRole("button", { name: /收起报告/ })).toHaveAttribute(
        "aria-expanded",
        "true",
      );
      await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    } finally {
      Element.prototype.scrollIntoView = originalScroll;
    }
  });

  it("核验附录折叠在 details 中，正文不混入附录数字", () => {
    const response = [
      "# 经营分析",
      "",
      "正文结论。",
      "",
      "## 核验附录（工具算出的数字）",
      "",
      "- 销售额合计 17224",
    ].join("\n");
    render(<ReportView result={makeResult({ response })} />);

    expect(
      screen.getByText("核验附录（工具算出的数字，不是模型现编的）"),
    ).toBeInTheDocument();
  });

  it("流式期间显示占位/光标并禁用复制与导出，结束后恢复", () => {
    const { rerender } = render(
      <ReportView result={makeResult({ response: "" })} streaming />,
    );
    expect(screen.getByText("正在生成报告…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制报告全文" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "导出" })).toBeDisabled();

    rerender(<ReportView result={makeResult({ response: "已经写了一段" })} streaming />);
    expect(document.querySelector(".report-cursor")).toBeTruthy();
    expect(screen.queryByText("正在生成报告…")).not.toBeInTheDocument();

    rerender(<ReportView result={makeResult({ response: "已经写了一段" })} />);
    expect(document.querySelector(".report-cursor")).toBeNull();
    expect(screen.getByRole("button", { name: "导出" })).toBeEnabled();
  });

  it("按配方重跑：点击回调 onReplay，重跑中禁用并显示进度文案", () => {
    const onReplay = vi.fn();
    const { rerender } = render(<ReportView result={makeResult()} onReplay={onReplay} />);
    fireEvent.click(screen.getByRole("button", { name: "按配方重跑" }));
    expect(onReplay).toHaveBeenCalledTimes(1);

    rerender(<ReportView result={makeResult()} onReplay={onReplay} replayBusy />);
    expect(screen.getByRole("button", { name: /重跑中…/ })).toBeDisabled();
  });

  it("来源徽章只展示合法字段：非法 SHA 与负数行数不编造", () => {
    render(
      <ReportView
        result={makeResult({
          dataset_profile: {
            source: "   ",
            rows: -1,
            row_count: 0,
            column_count: 8,
            column_info: [],
            sha256: "not-a-sha",
          },
        })}
      />,
    );

    const badge = screen.getByText(/来源：/);
    expect(badge).toHaveTextContent("来源：当前会话数据集");
    expect(badge.textContent).not.toContain("SHA-256");
    expect(badge).not.toHaveAttribute("title");
  });
});

describe("ReportView 导出菜单", () => {
  it("Markdown 导出下载 .md 文件并关闭菜单", async () => {
    const { blobs, clicks } = stubObjectUrl();
    render(<ReportView result={makeResult()} />);

    openExportMenu();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: /Markdown/ }));

    expect(clicks).toHaveLength(1);
    expect(clicks[0].download).toMatch(/^分析报告_\d{4}-\d{2}-\d{2}\.md$/);
    expect(await blobs[0].text()).toBe(REPORT_TEXT);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("HTML 导出内联样式并正确转换标题/表格/列表/引用/代码/链接", async () => {
    const { blobs, clicks } = stubObjectUrl();
    const response = [
      "# 导出测试",
      "",
      "## 表格",
      "",
      "| 区域 | 销售额 |",
      "| --- | --- |",
      "| 华东 | 100 |",
      "",
      "## 列表",
      "",
      "- 项目一",
      "- 项目二",
      "",
      "1. 第一步",
      "2. 第二步",
      "",
      "> 引用文本",
      "",
      "```js",
      "const a = 1 < 2;",
      "```",
      "",
      "行内 `code` 与 **粗体** ~~删除~~ *斜体*。",
      "",
      "[安全链接](https://example.com) 和 [危险链接](javascript:alert(1))",
      "",
      "---",
      "",
      "段落文字。",
    ].join("\n");
    render(<ReportView result={makeResult({ response })} />);
    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /HTML/ }));

    const html = await blobs[0].text();
    expect(clicks[0].download).toBe("导出测试.html");
    expect(html).toContain("<!DOCTYPE html>");
    expect(html).toContain("<title>导出测试</title>");
    expect(html).toContain("<h1>导出测试</h1>");
    expect(html).toContain("<h2>表格</h2>");
    expect(html).toContain("<th>区域</th>");
    expect(html).toContain("<td>华东</td>");
    expect(html).toContain("<ul><li>项目一</li>");
    expect(html).toContain("<ol><li>第一步</li>");
    expect(html).toContain("<blockquote>引用文本</blockquote>");
    expect(html).toContain('<code class="language-js">const a = 1 &lt; 2;</code>');
    expect(html).toContain("<code>code</code>");
    expect(html).toContain("<strong>粗体</strong>");
    expect(html).toContain("<del>删除</del>");
    expect(html).toContain("<em>斜体</em>");
    // 非安全协议被替换为 #，避免导出文件里的 XSS 向量
    expect(html).toContain('<a href="https://example.com">安全链接</a>');
    expect(html).toContain('<a href="#">危险链接</a>');
    expect(html).toContain("<hr />");
    expect(html).toContain("<p>段落文字。</p>");
  });

  it("打印被浏览器拦截时提示允许弹窗，而不是静默失败", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    stubObjectUrl();
    render(<ReportView result={makeResult()} />);

    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /打印/ }));

    expect(openSpy).toHaveBeenCalled();
    expect(useErrorStore.getState().error).toBe(
      "无法打开打印窗口，请允许浏览器弹窗后重试，或选择 HTML 导出。",
    );
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("打印窗口打开后写入自包含 HTML 并触发打印（load 未触发时 600ms 兜底）", () => {
    vi.useFakeTimers();
    try {
      const fakeWindow = {
        document: { open: vi.fn(), write: vi.fn(), close: vi.fn() },
        focus: vi.fn(),
        print: vi.fn(),
        addEventListener: vi.fn(),
      };
      vi.spyOn(window, "open").mockReturnValue(fakeWindow as unknown as Window);
      stubObjectUrl();
      render(<ReportView result={makeResult()} />);

      openExportMenu();
      fireEvent.click(screen.getByRole("menuitem", { name: /打印/ }));

      expect(fakeWindow.document.write).toHaveBeenCalledWith(
        expect.stringContaining("<h1>销售分析报告</h1>"),
      );
      expect(fakeWindow.print).not.toHaveBeenCalled();

      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(fakeWindow.focus).toHaveBeenCalled();
      expect(fakeWindow.print).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("有会话时提供 Office / 仪表盘 / 溯源导出，成功下载并复位状态", async () => {
    const { blobs, clicks } = stubObjectUrl();
    const fetchMock = vi.fn(
      async (_url: string) => new Response(new Blob(["binary"]), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReportView result={makeResult()} sessionId="s-export" />);

    openExportMenu();
    expect(screen.getByRole("menuitem", { name: /Word/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /PPT/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "PDF (.pdf)" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("menuitem", { name: /Word/ }));
    await waitFor(() => expect(clicks).toHaveLength(1), { timeout: 5000 });
    expect(clicks[0].download).toBe("分析报告.docx");

    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /PPT/ }));
    await waitFor(() => expect(clicks).toHaveLength(2), { timeout: 5000 });
    expect(clicks[1].download).toBe("分析汇报.pptx");

    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "PDF (.pdf)" }));
    await waitFor(() => expect(clicks).toHaveLength(3), { timeout: 5000 });
    expect(clicks[2].download).toBe("分析报告.pdf");

    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /数据画像仪表盘/ }));
    await waitFor(() => expect(clicks).toHaveLength(4), { timeout: 5000 });
    expect(clicks[3].download).toMatch(/^数据画像仪表盘_\d{4}-\d{2}-\d{2}\.html$/);

    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /分析溯源/ }));
    await waitFor(() => expect(clicks).toHaveLength(5), { timeout: 5000 });
    expect(clicks[4].download).toMatch(/^分析溯源_\d{4}-\d{2}-\d{2}\.json$/);

    // 每个导出各自请求对应后端端点
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls).toEqual([
      expect.stringContaining("/api/sessions/s-export/report.docx"),
      expect.stringContaining("/api/sessions/s-export/report.pptx"),
      expect.stringContaining("/api/sessions/s-export/report.pdf"),
      expect.stringContaining("/api/sessions/s-export/dashboard"),
      expect.stringContaining("/api/sessions/s-export/provenance/export"),
    ]);
    expect(blobs).toHaveLength(5);
    expect(useErrorStore.getState().error).toBe("");
  });

  it("仪表盘/溯源/Office 导出失败时把服务端原因写进错误提示", async () => {
    stubObjectUrl();
    const responses: Record<string, Response> = {
      dashboard: new Response(JSON.stringify({ detail: "没有导出权限" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
      provenance: new Response("<html>Bad Gateway</html>", {
        status: 502,
        headers: { "content-type": "text/html" },
      }),
      office: new Response(JSON.stringify({ detail: "报告尚未完成" }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    };
    let current = "dashboard";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => responses[current]),
    );
    render(<ReportView result={makeResult()} sessionId="s-export" />);

    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /数据画像仪表盘/ }));
    await waitFor(() =>
      expect(useErrorStore.getState().error).toBe("导出仪表盘失败：没有导出权限"),
    );

    current = "provenance";
    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /分析溯源/ }));
    await waitFor(() =>
      expect(useErrorStore.getState().error).toBe("导出溯源失败：<html>Bad Gateway</html>"),
    );

    current = "office";
    openExportMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Word/ }));
    await waitFor(() => expect(useErrorStore.getState().error).toBe("导出失败：报告尚未完成"));
  });

  it("没有会话时不展示 Office/仪表盘/溯源菜单项（后端无处可取）", () => {
    stubObjectUrl();
    render(<ReportView result={makeResult()} sessionId={null} />);
    openExportMenu();

    expect(screen.queryByRole("menuitem", { name: /Word/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /数据画像仪表盘/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /分析溯源/ })).not.toBeInTheDocument();
    // Markdown / HTML / 打印是纯前端能力，无会话也保留
    expect(screen.getByRole("menuitem", { name: /Markdown/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /HTML/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /打印/ })).toBeInTheDocument();
  });
});
