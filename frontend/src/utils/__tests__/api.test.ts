import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  api,
  describeApiError,
  fetchJsonWithTimeout,
  requestHeaders,
  uploadWithProgress,
} from "../api";

type ProgressHandler = (event: Partial<ProgressEvent> & { lengthComputable: boolean }) => void;

class FakeXHR {
  static instances: FakeXHR[] = [];
  upload: { onprogress: ProgressHandler | null } = { onprogress: null };
  status = 0;
  responseText = "";
  headers: Record<string, string> = {};
  sent: FormData | null = null;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  aborted = false;

  constructor() {
    FakeXHR.instances.push(this);
  }

  open() {}
  setRequestHeader(key: string, value: string) {
    this.headers[key] = value;
  }
  getResponseHeader(name: string) {
    return this.headers[name.toLowerCase()] ?? null;
  }
  send(form: FormData) {
    this.sent = form;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  respond(status: number, body: string, contentType = "application/json") {
    this.status = status;
    this.responseText = body;
    this.headers["content-type"] = contentType;
    this.onload?.();
  }
  fail() {
    this.onerror?.();
  }
}

function latestXhr(): FakeXHR {
  const instance = FakeXHR.instances[FakeXHR.instances.length - 1];
  if (!instance) throw new Error("没有创建 XHR 实例");
  return instance;
}

beforeEach(() => {
  FakeXHR.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("requestHeaders / describeApiError", () => {
  it("存在访问令牌时附加 X-App-Token", () => {
    window.localStorage.setItem("data-desk-access-token", "token-1");
    expect(requestHeaders({ "X-Custom": "1" })).toEqual({
      "X-Custom": "1",
      "X-App-Token": "token-1",
    });
  });

  it("无令牌时只保留原 headers", () => {
    expect(requestHeaders()).toEqual({});
  });

  it("错误描述覆盖空值 / 字符串截断 / detail / message / error / 无法序列化", () => {
    expect(describeApiError(null, 500)).toBe("请求失败 (500)");
    expect(describeApiError("", 502)).toBe("请求失败 (502)");
    expect(describeApiError("x".repeat(300), 500)).toHaveLength(201);
    expect(describeApiError({ detail: "明细" }, 400)).toBe("明细");
    expect(describeApiError({ message: "消息" }, 400)).toBe("消息");
    expect(describeApiError({ error: "错误" }, 400)).toBe("错误");
    expect(describeApiError({ foo: "bar" }, 400)).toBe('{"foo":"bar"}');
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(describeApiError(circular, 418)).toBe("请求失败 (418)");
    expect(describeApiError(42, 400)).toBe("42");
  });
});

describe("api", () => {
  it("解析 JSON 响应", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        ),
      ),
    );
    await expect(api<{ ok: boolean }>("/x")).resolves.toEqual({ ok: true });
  });

  it("非 JSON 响应按文本返回", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("plain", { status: 200, headers: { "content-type": "text/plain" } }))),
    );
    await expect(api("/x")).resolves.toBe("plain");
  });

  it("非 2xx 抛 ApiError 并保留 Retry-After", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail: "太频繁" }), {
            status: 429,
            headers: { "content-type": "application/json", "retry-after": "3" },
          }),
        ),
      ),
    );
    await expect(api("/x")).rejects.toMatchObject({
      name: "ApiError",
      status: 429,
      message: "太频繁",
      retryAfter: 3,
    });
  });

  it("用户主动取消时原样抛出 AbortError", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const pending = api("/x", { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("内部超时被翻译成友好提示", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const assertion = expect(api("/x", { timeoutMs: 500 })).rejects.toThrow(
      "连接服务超时，请检查网络后重试。",
    );
    vi.advanceTimersByTime(500);
    await assertion;
  });

  it("网络错误原样向上抛", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("network down"))));
    await expect(api("/x")).rejects.toThrow("network down");
  });
});

describe("uploadWithProgress", () => {
  const form = () => new FormData();

  it("成功上传解析 JSON 并回调进度", async () => {
    const onProgress = vi.fn();
    const pending = uploadWithProgress<{ id: string }>("/upload", form(), { onProgress });
    const xhr = latestXhr();
    expect(xhr.headers["X-App-Token"]).toBeUndefined();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 });
    expect(onProgress).toHaveBeenCalledWith(50);
    // 无法计算长度时忽略进度
    xhr.upload.onprogress?.({ lengthComputable: false, loaded: 1, total: 0 });
    expect(onProgress).toHaveBeenCalledTimes(1);
    xhr.respond(201, JSON.stringify({ id: "s1" }));
    await expect(pending).resolves.toEqual({ id: "s1" });
  });

  it("非 JSON 成功响应按文本返回，非法 JSON 保留原文", async () => {
    const pending = uploadWithProgress("/upload", form());
    latestXhr().respond(200, "{broken", "application/json");
    await expect(pending).resolves.toBe("{broken");

    const textPending = uploadWithProgress("/upload", form());
    latestXhr().respond(200, "ok", "text/plain");
    await expect(textPending).resolves.toBe("ok");
  });

  it("非 2xx 抛 ApiError", async () => {
    const pending = uploadWithProgress("/upload", form());
    latestXhr().respond(413, JSON.stringify({ detail: "文件过大" }), "application/json");
    await expect(pending).rejects.toMatchObject({ name: "ApiError", status: 413, message: "文件过大" });
  });

  it("网络错误抛 TypeError", async () => {
    const pending = uploadWithProgress("/upload", form());
    latestXhr().fail();
    await expect(pending).rejects.toBeInstanceOf(TypeError);
  });

  it("传入已取消的 signal 立即拒绝，不发送请求", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(
      uploadWithProgress("/upload", form(), { signal: controller.signal }),
    ).rejects.toMatchObject({ name: "AbortError" });
    // 只创建了 XHR 实例，但没有真正发送
    expect(latestXhr().sent).toBeNull();
  });

  it("上传途中取消会中断 XHR 并抛 AbortError", async () => {
    const controller = new AbortController();
    const pending = uploadWithProgress("/upload", form(), { signal: controller.signal });
    const xhr = latestXhr();
    controller.abort();
    expect(xhr.aborted).toBe(true);
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("fetchJsonWithTimeout", () => {
  it("成功返回 JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ thumbnail: "x" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        ),
      ),
    );
    await expect(fetchJsonWithTimeout("/thumb")).resolves.toEqual({ thumbnail: "x" });
  });

  it("非 2xx 抛 ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("丢失", { status: 404 }))),
    );
    await expect(fetchJsonWithTimeout("/thumb")).rejects.toBeInstanceOf(ApiError);
  });

  it("超时后以 AbortError 退出，避免永久挂起", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const assertion = expect(fetchJsonWithTimeout("/thumb", 100)).rejects.toMatchObject({
      name: "AbortError",
    });
    vi.advanceTimersByTime(100);
    await assertion;
  });
});
