import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// useArtifactPreview 行为测试：图表预览的完整用户路径。
// 覆盖：打开预览（正常 / 304 条件请求命中 LRU / 非自包含 HTML / HTTP 错误 /
// 请求中断）→ iframe onLoad 收尾与 8s 兜底超时 → chart-error postMessage
// 错误态 → 对比图加载（缓存命中 / 无效 HTML 静默忽略）→ PNG 导出（成功回传 /
// 无 iframe / 5s 超时）→ 图表内联编辑（成功刷新 session、失败提示、无上下文
// 直接返回）。全部请求通过 mock fetch / mock api 完成，不走真实网络。

const apiMock = vi.hoisted(() => vi.fn());

vi.mock("../../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../utils/api")>();
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

import useArtifactPreview from "../useArtifactPreview";
import { useUIStore } from "../../store/useUIStore";
import { useSessionStore } from "../../store/useSessionStore";
import { useErrorStore } from "../../store/useErrorStore";
import { API_URL, PREVIEW_CACHE_MAX } from "../../constants";
import type { Artifact, Session } from "../../types";

const VALID_HTML = [
  "<!doctype html><html><head><title>图</title></head><body>",
  '<div id="chart"></div><script>var chart = echarts.init(document.getElementById("chart"));</script>',
  "<!-- 补齐长度，让预览文档通过自包含校验（> 200 字符）",
  "============================================================ -->",
  "</body></html>",
].join("");

function chart(overrides: Partial<Artifact> = {}): Artifact {
  return {
    name: "趋势图.html",
    kind: "visualization",
    path: "/runs/s1/artifacts/趋势图.html",
    description: "区域趋势",
    preview_url: "/api/sessions/s1/artifacts/趋势图.html/preview",
    ...overrides,
  };
}

interface FakeResponseInit {
  ok?: boolean;
  status?: number;
  text?: string;
  etag?: string | null;
}

function htmlResponse(init: FakeResponseInit = {}): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    text: async () => init.text ?? VALID_HTML,
    json: async () => ({}),
    headers: {
      get: (name: string) => {
        if (name.toLowerCase() === "etag") return init.etag === undefined ? 'W/"v1"' : init.etag;
        return null;
      },
    },
  } as unknown as Response;
}

function sessionWith(artifacts: Artifact[], id = "s1"): Session {
  return {
    id,
    filename: "sales.csv",
    preview: [],
    profile: { row_count: 0, column_count: 0, column_info: [] },
    artifacts,
    analysis_status: "completed",
  };
}

function resetStores() {
  useUIStore.setState({
    previewItem: null,
    previewHtml: "",
    previewLoading: false,
    previewError: "",
  });
  useSessionStore.setState({ session: null });
  useErrorStore.setState({ error: "", errorExpanded: false });
}

let clickSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  apiMock.mockReset();
  resetStores();
  clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clickSpy.mockRestore();
});

describe("useArtifactPreview 打开预览", () => {
  it("没有 preview_url 的产物直接返回，不发请求", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(chart({ preview_url: undefined }));
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(useUIStore.getState().previewItem).toBeNull();
  });

  it("首次打开：校验后注入错误上报/主题桥接脚本，loading 由 iframe onLoad 收尾", async () => {
    const fetchMock = vi.fn(async () => htmlResponse());
    vi.stubGlobal("fetch", fetchMock);
    const item = chart();
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(item);
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}${item.preview_url}`);
    // 首次没有缓存，不应带 If-None-Match
    expect(init.headers).not.toHaveProperty("If-None-Match");

    expect(useUIStore.getState().previewItem).toEqual(item);
    expect(useUIStore.getState().previewError).toBe("");
    // 图表加载完成前保持 loading（由 iframe onLoad 关闭）
    expect(useUIStore.getState().previewLoading).toBe(true);
    const html = useUIStore.getState().previewHtml;
    expect(html).toContain("chart-error");
    expect(html).toContain("chart-theme-bridge");
    // 两个注入脚本只出现一次（幂等）
    expect(html.match(/chart-theme-bridge/g)).toHaveLength(1);

    act(() => result.current.onPreviewIframeLoaded());
    expect(useUIStore.getState().previewLoading).toBe(false);
  });

  it("二次打开带 ETag 条件请求，304 时复用缓存且不重新解析正文", async () => {
    const first = htmlResponse({ etag: '"abc"' });
    const second = htmlResponse({ status: 304, text: "should-not-be-read" });
    const secondText = vi.spyOn(second, "text");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    vi.stubGlobal("fetch", fetchMock);
    const item = chart();
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(item);
    });
    act(() => result.current.onPreviewIframeLoaded());
    act(() => result.current.closeArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(item);
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(init.headers).toMatchObject({ "If-None-Match": '"abc"' });
    expect(secondText).not.toHaveBeenCalled();
    expect(useUIStore.getState().previewHtml).toContain("chart-error");
    expect(useUIStore.getState().previewLoading).toBe(false);
  });

  it("304 但没有缓存时按无效文档处理并提示重新生成", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => htmlResponse({ status: 304, text: "" })),
    );
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(chart());
    });

    expect(useUIStore.getState().previewLoading).toBe(false);
    expect(useUIStore.getState().previewError).toContain("预览文档缺少内联图表库");
  });

  it("非自包含 HTML（残留相对路径脚本）被拒绝并提示重试", async () => {
    const bad = VALID_HTML.replace(
      "</body>",
      '<script src="plotly.min.js"></script></body>',
    );
    vi.stubGlobal("fetch", vi.fn(async () => htmlResponse({ text: bad })));
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(chart());
    });

    expect(useUIStore.getState().previewError).toBe(
      "图表加载失败：预览文档缺少内联图表库，请重新生成产物或刷新页面后重试。",
    );
    expect(useUIStore.getState().previewHtml).toBe("");
  });

  it("HTTP 404 且服务端返回 JSON detail 时透传原因", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        htmlResponse({ ok: false, status: 404, text: '{"detail":"产物已被清理"}' }),
      ),
    );
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(chart());
    });

    expect(useUIStore.getState().previewLoading).toBe(false);
    expect(useUIStore.getState().previewError).toBe("图表加载失败：产物已被清理");
  });

  it("请求被中断（AbortError）时不覆盖错误态；close 会中断在途请求", async () => {
    let capturedSignal: AbortSignal | undefined;
    let rejectFetch: ((err: unknown) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        capturedSignal = init?.signal ?? undefined;
        return new Promise((_resolve, reject) => {
          rejectFetch = reject;
        });
      }),
    );
    const { result } = renderHook(() => useArtifactPreview());

    let pending: Promise<void> | undefined;
    act(() => {
      pending = result.current.openArtifactPreview(chart());
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(useUIStore.getState().previewLoading).toBe(true);

    // 关闭预览会 abort 在途请求
    act(() => result.current.closeArtifactPreview());
    expect(capturedSignal?.aborted).toBe(true);

    const abortErr = new Error("aborted");
    abortErr.name = "AbortError";
    await act(async () => {
      rejectFetch?.(abortErr);
      await pending;
    });
    // AbortError 不产生错误提示，关闭后状态保持干净
    expect(useUIStore.getState().previewError).toBe("");
    expect(useUIStore.getState().previewItem).toBeNull();
    expect(useUIStore.getState().previewLoading).toBe(false);
  });

  it("iframe 8 秒未触发 onLoad 时兜底关闭 loading 并提示超时", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => htmlResponse()));
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(chart());
    });
    expect(useUIStore.getState().previewLoading).toBe(true);

    act(() => {
      vi.advanceTimersByTime(8000);
    });

    expect(useUIStore.getState().previewLoading).toBe(false);
    expect(useUIStore.getState().previewError).toBe(
      "图表加载超时，请检查网络或重新生成产物后重试。",
    );
  });
});

describe("useArtifactPreview 图表运行时错误与缓存", () => {
  it("chart-error 消息：截断错误、清掉该图缓存，下次打开重新下载", async () => {
    const fetchMock = vi.fn(async () => htmlResponse());
    vi.stubGlobal("fetch", fetchMock);
    const item = chart();
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(item);
    });
    act(() => result.current.onPreviewIframeLoaded());

    const longMessage = "x".repeat(500);
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "chart-error", message: longMessage } }),
      );
    });

    const state = useUIStore.getState();
    expect(state.previewLoading).toBe(false);
    expect(state.previewError).toContain("图表渲染出错");
    expect(state.previewError).toContain("x".repeat(200));
    expect(state.previewError).not.toContain("x".repeat(201));

    // 缓存被清除：再次打开应重新下载（不带 If-None-Match）
    act(() => result.current.closeArtifactPreview());
    await act(async () => {
      await result.current.openArtifactPreview(item);
    });
    const [, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(init.headers).not.toHaveProperty("If-None-Match");
  });

  it("没有预览项时的 chart-error 消息被忽略；非 chart-error 消息也不处理", async () => {
    renderHook(() => useArtifactPreview());
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "chart-error", message: "无主消息" } }),
      );
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "png-data", data: "data:image/png;base64,AA" } }),
      );
    });
    expect(useUIStore.getState().previewError).toBe("");
  });

  it("LRU 上限：超过 PREVIEW_CACHE_MAX 后最旧条目被淘汰，重新打开会重新下载", async () => {
    const fetchMock = vi.fn(async () => htmlResponse({ etag: '"e"' }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useArtifactPreview());

    const first = chart({ name: "a.html", preview_url: "/p/a.html" });
    for (let i = 0; i <= PREVIEW_CACHE_MAX; i += 1) {
      await act(async () => {
        await result.current.openArtifactPreview(
          chart({ name: `${i}.html`, preview_url: `/p/${i}.html` }),
        );
      });
      act(() => result.current.onPreviewIframeLoaded());
      act(() => result.current.closeArtifactPreview());
    }

    const callsBefore = fetchMock.mock.calls.length;
    await act(async () => {
      await result.current.openArtifactPreview(first);
    });
    const [, init] = fetchMock.mock.calls[callsBefore] as unknown as [string, RequestInit];
    // 最旧的 a.html 已被淘汰 ⇒ 无条件的全新请求
    expect(init.headers).not.toHaveProperty("If-None-Match");
  });

  it("已含 chart-error / chart-theme-bridge 标记的历史 HTML 不会被重复注入", async () => {
    const prepared = VALID_HTML.replace(
      "<head>",
      "<head><script>/*chart-error*/</script><script>/*chart-theme-bridge*/</script>",
    );
    vi.stubGlobal("fetch", vi.fn(async () => htmlResponse({ text: prepared, etag: null })));
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(chart());
    });

    expect(useUIStore.getState().previewHtml).toBe(prepared);
  });
});

describe("useArtifactPreview 对比图", () => {
  it("加载对比图成功并写入对比 HTML；关闭预览会清空对比状态", async () => {
    const fetchMock = vi.fn(async () => htmlResponse());
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useArtifactPreview());
    const item = chart({ name: "对比.html", preview_url: "/p/对比.html" });

    await act(async () => {
      await result.current.loadCompareChart(item);
    });

    expect(result.current.compareItem).toEqual(item);
    expect(result.current.compareLoading).toBe(false);
    expect(result.current.compareHtml).toContain("chart-theme-bridge");

    act(() => {
      useUIStore.setState({ previewItem: chart() });
      result.current.closeArtifactPreview();
    });
    expect(result.current.compareItem).toBeNull();
    expect(result.current.compareHtml).toBe("");
    expect(result.current.compareLoading).toBe(false);
  });

  it("对比图复用主图缓存：第二次 304 直接复用；无效 HTML / 网络错误静默忽略", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(htmlResponse({ etag: '"c"' }))
      .mockResolvedValueOnce(htmlResponse({ status: 304, text: "" }))
      .mockResolvedValueOnce(htmlResponse({ text: "short" }))
      .mockRejectedValueOnce(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useArtifactPreview());
    const item = chart({ name: "c.html", preview_url: "/p/c.html" });

    await act(async () => {
      await result.current.loadCompareChart(item);
    });
    expect(result.current.compareHtml).toContain("chart-error");

    // 304：复用缓存
    await act(async () => {
      await result.current.loadCompareChart(item);
    });
    expect(result.current.compareHtml).toContain("chart-error");
    expect(result.current.compareLoading).toBe(false);

    // 无效 HTML：不写入 compareHtml，但 loading 正常复位
    await act(async () => {
      await result.current.loadCompareChart(chart({ name: "bad.html", preview_url: "/p/bad.html" }));
    });
    expect(result.current.compareHtml).toBe("");
    expect(result.current.compareLoading).toBe(false);

    // 网络错误：静默忽略（不抛错、不阻塞主图）
    await act(async () => {
      await result.current.loadCompareChart(chart({ name: "err.html", preview_url: "/p/err.html" }));
    });
    expect(result.current.compareLoading).toBe(false);
  });

  it("对比图没有 preview_url 时直接返回", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.loadCompareChart(chart({ preview_url: undefined }));
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.compareItem).toBeNull();
  });
});

describe("useArtifactPreview PNG 导出", () => {
  function mountIframe(): HTMLIFrameElement {
    const wrap = document.createElement("div");
    wrap.className = "preview-frame";
    const iframe = document.createElement("iframe");
    wrap.appendChild(iframe);
    document.body.appendChild(wrap);
    return iframe;
  }

  it("收到 iframe 回传的 PNG base64 后触发下载并解除 loading", () => {
    const iframe = mountIframe();
    act(() => {
      useUIStore.setState({ previewItem: chart({ name: "区域趋势.html" }) });
    });
    const postSpy = vi.spyOn(iframe.contentWindow!, "postMessage");
    const { result } = renderHook(() => useArtifactPreview());

    act(() => {
      result.current.downloadPng();
    });
    expect(postSpy).toHaveBeenCalledWith({ type: "download-png" }, "*");
    expect(result.current.pngDownloading).toBe(true);

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "png-data", data: "data:image/png;base64,AAAA" },
          source: iframe.contentWindow,
        }),
      );
    });

    expect(result.current.pngDownloading).toBe(false);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const anchor = clickSpy.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe("区域趋势.png");
    expect(anchor.href).toBe("data:image/png;base64,AAAA");
    iframe.parentElement?.remove();
  });

  it("没有预览 iframe 时点击导出无副作用", () => {
    const { result } = renderHook(() => useArtifactPreview());
    act(() => {
      result.current.downloadPng();
    });
    expect(result.current.pngDownloading).toBe(false);
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it("不匹配的消息来源 / 非 PNG 数据不会被当成导出结果", () => {
    const iframe = mountIframe();
    act(() => {
      useUIStore.setState({ previewItem: chart() });
    });
    const { result } = renderHook(() => useArtifactPreview());
    act(() => {
      result.current.downloadPng();
    });
    act(() => {
      // source 不匹配
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "png-data", data: "data:image/png;base64,AAAA" },
          source: window,
        }),
      );
      // source 匹配但数据不是 PNG
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "png-data", data: "javascript:alert(1)" },
          source: iframe.contentWindow,
        }),
      );
    });
    expect(result.current.pngDownloading).toBe(true);
    expect(clickSpy).not.toHaveBeenCalled();
    iframe.parentElement?.remove();
  });

  it("5 秒未收到回传时解除 loading，避免按钮永久禁用", () => {
    vi.useFakeTimers();
    const iframe = mountIframe();
    act(() => {
      useUIStore.setState({ previewItem: chart() });
    });
    const { result } = renderHook(() => useArtifactPreview());
    act(() => {
      result.current.downloadPng();
    });
    expect(result.current.pngDownloading).toBe(true);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current.pngDownloading).toBe(false);
    iframe.parentElement?.remove();
  });
});

describe("useArtifactPreview 图表内联编辑", () => {
  it("保存成功后清除该图缓存、刷新 session 与 previewItem，并按用户改动决定是否发送颜色", async () => {
    const original = chart();
    const edited = { ...original, description: "改后的标题" };
    useSessionStore.setState({ session: sessionWith([original]) });
    act(() => {
      useUIStore.setState({ previewItem: original });
    });
    apiMock.mockResolvedValue(sessionWith([edited]));
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === "PUT" ? htmlResponse() : htmlResponse());
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useArtifactPreview());
    act(() => {
      result.current.setChartEditTitle("改后的标题");
    });

    await act(async () => {
      await result.current.editChart();
    });

    const putCall = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit | undefined)?.method === "PUT",
    ) as unknown as [string, RequestInit];
    expect(putCall[0]).toBe(`${API_URL}/api/sessions/s1/artifacts/趋势图.html/edit`);
    // 用户没动色块：请求不携带 color，避免把分组配色刷成品牌单色
    expect(JSON.parse(String(putCall[1].body))).toEqual({ title: "改后的标题" });

    expect(apiMock).toHaveBeenCalledWith("/api/sessions/s1");
    expect(useSessionStore.getState().session?.artifacts).toEqual([edited]);
    expect(useUIStore.getState().previewItem).toMatchObject({ description: "改后的标题" });
    expect(useUIStore.getState().previewLoading).toBe(true); // 重新打开预览由 iframe onLoad 收尾
    expect(result.current.chartEditOpen).toBe(false);
    expect(result.current.chartEditSaving).toBe(false);
  });

  it("用户主动改过主色时随请求发送 color", async () => {
    const original = chart();
    useSessionStore.setState({ session: sessionWith([original]) });
    act(() => {
      useUIStore.setState({ previewItem: original });
    });
    apiMock.mockResolvedValue(sessionWith([original]));
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => htmlResponse());
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useArtifactPreview());
    act(() => {
      result.current.setChartEditTitle("新标题");
      result.current.setChartEditColor("#AA0000");
      result.current.setChartEditColorTouched(true);
    });

    await act(async () => {
      await result.current.editChart();
    });

    const putCall = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit | undefined)?.method === "PUT",
    ) as unknown as [string, RequestInit];
    expect(JSON.parse(String(putCall[1].body))).toEqual({ title: "新标题", color: "#AA0000" });
  });

  it("服务端拒绝编辑时提示错误并复位保存态", async () => {
    const original = chart();
    useSessionStore.setState({ session: sessionWith([original]) });
    act(() => {
      useUIStore.setState({ previewItem: original });
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => htmlResponse({ ok: false, status: 500, text: "boom" })),
    );

    const { result } = renderHook(() => useArtifactPreview());
    await act(async () => {
      await result.current.editChart();
    });

    expect(useErrorStore.getState().error).toBe("图表编辑失败：图表编辑失败");
    expect(result.current.chartEditSaving).toBe(false);
  });

  it("没有会话或预览项时直接返回，不发请求", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.editChart();
    });
    expect(fetchMock).not.toHaveBeenCalled();

    act(() => {
      useSessionStore.setState({ session: sessionWith([]) });
    });
    await act(async () => {
      await result.current.editChart();
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("打开预览时用产物描述初始化编辑表单，关闭时复位", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => htmlResponse()));
    const original = chart({ description: "区域趋势对比" });
    const { result } = renderHook(() => useArtifactPreview());

    await act(async () => {
      await result.current.openArtifactPreview(original);
    });
    expect(result.current.chartEditTitle).toBe("区域趋势对比");
    expect(result.current.chartEditColor).toBe("#245C55");

    act(() => {
      result.current.setChartEditOpen(true);
      result.current.setChartEditColorTouched(true);
      result.current.closeArtifactPreview();
    });
    // 关闭预览本身不清编辑草稿（下次打开由 effect 重置）
    expect(result.current.chartEditOpen).toBe(true);

    await act(async () => {
      await result.current.openArtifactPreview(chart({ name: "无描述.html", description: "" }));
    });
    expect(result.current.chartEditTitle).toBe("无描述.html");
    expect(result.current.chartEditOpen).toBe(false);
  });
});
