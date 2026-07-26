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
// 而不是去解析 error.message 字符串。
class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function api<T = unknown>(path: string, options: ApiOptions = {}): Promise<T> {
  const { timeoutMs = 30000, signal: providedSignal, ...fetchOptions } = options;
  const controller = providedSignal ? null : new AbortController();
  // 标记是否为内部超时触发的 abort，用于区分用户主动取消（providedSignal.aborted）
  let timedOut = false;
  const timeout = controller ? window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs) : null;
  try {
    const response = await fetch(`${API_URL}${path}`, {
      ...fetchOptions,
      headers: requestHeaders(fetchOptions.headers as HeadersInit | undefined),
      signal: providedSignal || (controller?.signal),
    });
    const contentType = response.headers.get("content-type") || "";
    const payload: unknown = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) throw new ApiError(describeApiError(payload, response.status), response.status);
    return payload as T;
  } catch (err) {
    const error = err as Error;
    if (error.name === "AbortError") {
      // 用户通过 providedSignal 主动取消（如停止轮询）：直接抛 AbortError，不替换消息
      if (providedSignal?.aborted) throw error;
      // 内部超时取消：给通用超时提示，不硬编码部署平台名称
      throw new Error("连接服务超时，请检查网络后重试。");
    }
    throw error;
  } finally {
    if (timeout) window.clearTimeout(timeout);
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
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_URL}${path}`);
    const headers = requestHeaders();
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);
    const onAbort = () => xhr.abort();
    if (signal) {
      if (signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
      signal.addEventListener("abort", onAbort, { once: true });
    }
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

export { api, requestHeaders, describeApiError, ApiError, uploadWithProgress };
