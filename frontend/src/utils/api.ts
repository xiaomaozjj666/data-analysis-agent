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

export { api, requestHeaders, describeApiError, ApiError };
