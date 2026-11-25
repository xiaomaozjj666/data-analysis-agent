import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

// wait(300) 在批量下载里只用于节流（浏览器并发下载限制），
// 用同步 resolve 替换避免每个用例白等 300ms×N，语义不变。
const waitMock = vi.hoisted(() => vi.fn(async () => {}));
vi.mock("../../utils/format", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../utils/format")>()),
  wait: waitMock,
}));

import useDownloads from "../useDownloads";
import { useErrorStore } from "../../store/useErrorStore";
import { API_URL } from "../../constants";
import type { Artifact, HistorySessionItem } from "../../types";

// useDownloads 行为测试：单产物下载 / 批量下载（部分失败汇总）/ 会话导出。
// 覆盖成功路径、HTTP 错误（JSON detail 与整页文本两种 payload）、
// AbortError 超时文案，以及导出期间的 exportingSessionId 生命周期。

interface FakeResponseInit {
  ok?: boolean;
  status?: number;
  contentType?: string;
  body?: unknown;
  text?: string;
}

function fakeResponse(init: FakeResponseInit = {}): Response {
  const contentType = init.contentType ?? "application/json";
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    headers: {
      get: (name: string) => (name.toLowerCase() === "content-type" ? contentType : null),
    },
    json: async () => init.body ?? {},
    text: async () => init.text ?? "",
    blob: async () => new Blob(["payload"], { type: "application/octet-stream" }),
  } as unknown as Response;
}

function artifact(name: string): Artifact {
  return { name, kind: "chart", path: `/tmp/${name}`, description: "", download_url: `/dl/${name}` };
}

function sessionItem(): HistorySessionItem {
  return { id: "s1", filename: "sales", analysis_status: "completed" };
}

let clicked: HTMLAnchorElement[] = [];
const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
  this: HTMLAnchorElement,
) {
  clicked.push(this);
});

beforeEach(() => {
  useErrorStore.setState({ error: "", errorExpanded: false });
  clicked = [];
  clickSpy.mockClear();
  waitMock.mockClear();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(() => "blob:mock-url"),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: vi.fn() });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useDownloads - downloadArtifact", () => {
  it("成功后触发浏览器下载并回收 object URL", async () => {
    const fetchMock = vi.fn(async () => fakeResponse());
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.downloadArtifact(artifact("chart.html"));
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((fetchMock.mock.calls[0] as unknown[])[0]).toBe(`${API_URL}/dl/chart.html`);
    expect(clicked).toHaveLength(1);
    expect(clicked[0].download).toBe("chart.html");
    expect(clicked[0].href).toBe("blob:mock-url");
    expect(URL.revokeObjectURL as unknown as Mock).toHaveBeenCalledWith("blob:mock-url");
    expect(useErrorStore.getState().error).toBe("");
  });

  it("HTTP 失败且服务端返回 JSON detail 时透传 detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => fakeResponse({ ok: false, status: 404, body: { detail: "产物不存在" } })),
    );

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.downloadArtifact(artifact("missing.html"));
    });

    expect(useErrorStore.getState().error).toBe("下载失败：产物不存在");
    expect(clicked).toHaveLength(0);
  });

  it("HTTP 失败且服务端返回整页文本时截断为可读消息", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        fakeResponse({ ok: false, status: 502, contentType: "text/html", text: "<html>Bad Gateway</html>" }),
      ),
    );

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.downloadArtifact(artifact("a.html"));
    });

    expect(useErrorStore.getState().error).toBe("下载失败：<html>Bad Gateway</html>");
  });

  it("请求被 abort 时给出超时文案而不是 AbortError 原文", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const err = new Error("The operation was aborted.");
        err.name = "AbortError";
        throw err;
      }),
    );

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.downloadArtifact(artifact("slow.html"));
    });

    expect(useErrorStore.getState().error).toBe("下载失败：下载超时，请稍后重试。");
  });
});

describe("useDownloads - batchDownload", () => {
  it("空列表不发请求", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.batchDownload([]);
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(useErrorStore.getState().error).toBe("");
  });

  it("全部成功时不打扰用户", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => fakeResponse()));

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.batchDownload([artifact("a.html"), artifact("b.html")]);
    });

    expect(clicked).toHaveLength(2);
    expect(waitMock).toHaveBeenCalledTimes(2);
    expect(useErrorStore.getState().error).toBe("");
  });

  it("部分失败时汇总成功/失败数量并列出失败文件名", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        String(url).endsWith("/b.html") ? fakeResponse({ ok: false, status: 500 }) : fakeResponse(),
      ),
    );

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.batchDownload([artifact("a.html"), artifact("b.html")]);
    });

    expect(useErrorStore.getState().error).toBe(
      "批量下载完成：成功 1 个，失败 1 个（b.html）。可在产物中心单独重新下载失败项。",
    );
  });

  it("失败超过 3 个时预览只列前三个并给出总数", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => fakeResponse({ ok: false, status: 500 })));

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.batchDownload([
        artifact("a.html"),
        artifact("b.html"),
        artifact("c.html"),
        artifact("d.html"),
        artifact("e.html"),
      ]);
    });

    expect(useErrorStore.getState().error).toBe(
      "批量下载完成：成功 0 个，失败 5 个（a.html、b.html、c.html 等 5 个文件）。可在产物中心单独重新下载失败项。",
    );
  });
});

describe("useDownloads - exportSession", () => {
  it("导出期间暴露 exportingSessionId，完成后复位并下载 ZIP", async () => {
    let resolveFetch: (value: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const fetchMock = vi.fn(() => pending);
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useDownloads());
    let inflight: Promise<void> = Promise.resolve();
    act(() => {
      inflight = result.current.exportSession(sessionItem());
    });

    expect(result.current.exportingSessionId).toBe("s1");
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_URL}/api/sessions/s1/export`,
      expect.objectContaining({}),
    );

    await act(async () => {
      resolveFetch(fakeResponse());
      await inflight;
    });

    expect(result.current.exportingSessionId).toBeNull();
    expect(clicked).toHaveLength(1);
    expect(clicked[0].download).toBe("sales_s1.zip");
  });

  it("导出失败时提示错误并复位 exportingSessionId", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        fakeResponse({ ok: false, status: 500, contentType: "application/json", body: { detail: "打包失败" } }),
      ),
    );

    const { result } = renderHook(() => useDownloads());
    await act(async () => {
      await result.current.exportSession(sessionItem());
    });

    expect(result.current.exportingSessionId).toBeNull();
    expect(useErrorStore.getState().error).toBe("导出会话失败：打包失败");
    expect(clicked).toHaveLength(0);
  });
});
