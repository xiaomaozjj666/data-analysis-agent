import { API_URL, ACCESS_TOKEN_KEY } from "../constants";

type HeadersInit = Record<string, string>;

interface ApiOptions extends RequestInit {
  timeoutMs?: number;
  signal?: AbortSignal;
}

interface ErrorPayload {
  detail?: string;
  message?: string;
  error?: string;
}

function requestHeaders(headers: HeadersInit = {}): HeadersInit {
  const token = window.localStorage.getItem(ACCESS_TOKEN_KEY);
  return {
    ...headers,
    ...(token ? { "X-App-Token": token } : {}),
  };
}

function describeApiError(payload: unknown, status: number): string {
  if (payload == null || payload === "") return `请求失败 (${status})`;
  if (typeof payload === "string") {
    // 服务端返回整页 HTML 时截断到 200 字符，避免错误消息变成一长坨标签。
    const trimmed = payload.length > 200 ? `${payload.slice(0, 200)}…` : payload;
    return trimmed;
  }
  if (typeof payload === "object") {
    const obj = payload as ErrorPayload;
    if (typeof obj.detail === "string" && obj.detail) return obj.detail;
    // FastAPI HTTPException 默认 {detail: ...}，但也可能嵌套其他字段。
    const fallback = obj.message || obj.error;
    if (typeof fallback === "string" && fallback) return fallback;
    try {
      return JSON.stringify(payload);
    } catch {
      return `请求失败 (${status})`;
    }
  }
  return String(payload);
}

// 自定义错误类保留 HTTP status，让上层能区分 404（会话失效）等场景，
// 而不是去解析 error.message 字符串。retryAfter 来自 429 响应的
// Retry-After 头，供「快速起草」展示需要等待的秒数。
class ApiError extends Error {
  status: number;
  retryAfter?: number;
  constructor(message: string, status: number, retryAfter?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    if (retryAfter !== undefined) this.retryAfter = retryAfter;
  }
}

async function api<T = unknown>(path: string, options: ApiOptions = {}): Promise<T> {
  const { timeoutMs = 30000, signal: providedSignal, ...fetchOptions } = options;
  // 无论调用方是否传入取消信号，都保留内部超时：轮询请求带着用户信号调用时，
  // 若服务端半开连接（进程被冻住/网络黑洞），没有超时就会永久 pending，
  // 前端会一直停在"检查中"。外部中止通过 forwardAbort 转接到内部 controller，
  // 靠 timedOut 标记区分"超时"与"用户主动取消"。
  const controller = new AbortController();
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const forwardAbort = () => controller.abort();
  if (providedSignal) {
    if (providedSignal.aborted) controller.abort();
    else providedSignal.addEventListener("abort", forwardAbort, { once: true });
  }
  try {
    const response = await fetch(`${API_URL}${path}`, {
      ...fetchOptions,
      headers: requestHeaders(fetchOptions.headers as HeadersInit | undefined),
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload: unknown = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const retryAfter = Number(response.headers.get("retry-after"));
      throw new ApiError(
        describeApiError(payload, response.status),
        response.status,
        Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : undefined,
      );
    }
    return payload as T;
  } catch (err) {
    const error = err as Error;
    if (error.name === "AbortError") {
      // 用户取消优先于超时：即使两者几乎同时发生，也保持 AbortError 语义，
      // 让上层把它当成"主动停止"而不是网络故障。
      if (providedSignal?.aborted) throw error;
      // 内部超时取消：给通用超时提示，不硬编码部署平台名称
      if (timedOut) throw new Error("连接服务超时，请检查网络后重试。");
      throw error;
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    // 必须移除监听，避免长生命周期 signal 上堆积回调（每次请求一个闭包）。
    providedSignal?.removeEventListener("abort", forwardAbort);
  }
}

// 带进度回调的文件上传：fetch 不支持上传进度，改用 XHR 的 upload.onprogress。
// 行为与 api() 对齐：自动带 X-App-Token、非 2xx 抛 ApiError（复用 describeApiError）、
// 支持 AbortSignal 取消（抛标准 AbortError，调用方可用 error.name 判断）。
// 不设 Content-Type，由浏览器为 FormData 自动生成 multipart boundary。
interface UploadOptions {
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

function uploadWithProgress<T = unknown>(path: string, form: FormData, options: UploadOptions = {}): Promise<T> {
  const { onProgress, signal } = options;
  return new Promise<T>((resolve, reject) => {
    // 已取消的请求不需要（也不应该）创建 XHR：浏览器会立刻触发一次 abort，
    // 白白占用连接池槽位。
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_URL}${path}`);
    const headers = requestHeaders();
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);
    const onAbort = () => xhr.abort();
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
      let payload: unknown = xhr.responseText;
      const contentType = xhr.getResponseHeader("content-type") || "";
      if (contentType.includes("application/json")) {
        try { payload = JSON.parse(xhr.responseText); } catch { /* 保留原始文本 */ }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as T);
      } else {
        reject(new ApiError(describeApiError(payload, xhr.status), xhr.status));
      }
    };
    xhr.onerror = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
      reject(new TypeError("网络错误，上传失败，请检查连接后重试。"));
    };
    xhr.onabort = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
      reject(new DOMException("Aborted", "AbortError"));
    };
    xhr.send(form);
  });
}

// 缩略图等轻量 JSON 拉取：带超时兜底。普通 fetch 在服务端中断/连接
// 挂起时 response.json() 可能永久 pending（实测：后端重启时缩略图链路
// 永远卡死且无任何可见状态），超时转 AbortError 让调用方走失败回退。
async function fetchJsonWithTimeout<T = unknown>(path: string, timeoutMs = 10_000): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_URL}${path}`, {
      headers: requestHeaders(),
      signal: controller.signal,
    });
    if (!response.ok) throw new ApiError(describeApiError(await response.text(), response.status), response.status);
    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

export { api, requestHeaders, describeApiError, ApiError, uploadWithProgress, fetchJsonWithTimeout };
