// SSE 事件分发的单一事实来源（single source of truth）。
//
// 之前 App.tsx 的 startAnalysis / startFollowUp 在传给 consumeSSEStream 的
// handlers 对象里硬编码了事件名字符串，并且每个 handler 内部用 `as` 断言
// 把宽泛的 data 收窄成具体形状。这里集中维护：
//   1. SSE_EVENT_TYPES —— 事件名常量，调用方用计算属性键引用，避免散落字符串；
//   2. SSEEventPayload —— 每个事件名对应的 data 负载形状，取代各 handler 的 `as` 断言；
//   3. SSEEventHandlers —— 由 SSEEventPayload 推导的 handler 映射类型，每个 handler 可选；
//   4. dispatchSSEEvent —— 类型安全的分发器，consumeSSEStream 内部统一调用。
//
// SSE_EVENT_TYPES 的值集合必须与 types.ts 的 SSEEventType 联合保持一致：
// SSEEventHandlers 基于 SSEEventName 映射，新增常量但漏写 SSEEventPayload 条目
// 会在编译期报错，从而强制两处同步。

import type {
  AnalysisResult,
  Artifact,
  CompletedStep,
  PlanStep,
  TokenUsage,
} from "../types";

// 事件名常量：键是稳定的代码标识符，值是后端 SSE 流上的 wire-format 事件名。
// 用 as const 让每个值的类型收窄为字面量，便于 SSEEventName 推导与计算属性键。
export const SSE_EVENT_TYPES = {
  STARTED: "started",
  PROGRESS: "progress",
  VALIDATE_DATASET: "validate_dataset",
  PLAN_ANALYSIS: "plan_analysis",
  PLAN_READY: "plan_ready",
  STEP_PROGRESS: "step_progress",
  EXECUTE_STEP: "execute_step",
  REPLAN: "replan",
  THINKING_CHUNK: "thinking_chunk",
  FINALIZE: "finalize",
  REPORT_CHUNK: "report_chunk",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  COMPLETE: "complete",
  CANCELLED: "cancelled",
  ERROR: "error",
  HEARTBEAT: "heartbeat",
  CHAT_CHUNK: "chat_chunk",
  CHAT_DONE: "chat_done",
} as const;

// wire-format 事件名联合（由常量推导，避免与 SSE_EVENT_TYPES 漂移）。
export type SSEEventName = (typeof SSE_EVENT_TYPES)[keyof typeof SSE_EVENT_TYPES];

// 每个事件名对应的 data 负载形状。取代 handler 内部的 `as` 断言：
// dispatchSSEEvent 会按 event 名把 data 以对应类型交给 handler。
// 无负载的事件用 Record<string, never>，handler 可写成 () => void。
export interface SSEEventPayload {
  started: Record<string, never>;
  progress: { title?: string };
  validate_dataset: Record<string, never>;
  plan_analysis: { plan?: PlanStep[] };
  plan_ready: { plan?: PlanStep[]; objective?: string };
  step_progress: { progress?: number; tool_calls?: number; message?: string };
  execute_step: Record<string, never>;
  replan: { completed_steps?: CompletedStep[] };
  thinking_chunk: { chunk?: string };
  finalize: Record<string, never>;
  report_chunk: { chunk?: string };
  tool_call: { call_id: string; name: string; input_preview?: string; started_at?: number };
  tool_result: { call_id: string; output_preview?: string; duration_ms?: number };
  complete: AnalysisResult;
  cancelled: { message?: string };
  error: { message?: string };
  heartbeat: Record<string, never>;
  chat_chunk: { chunk?: string };
  chat_done: { response?: string; reasoning?: string; usage?: TokenUsage; artifacts?: Artifact[] };
}

// 事件处理器映射：每个事件名对应一个可选 handler，data 类型由 SSEEventPayload 推导。
// 消费端只需提供一个 SSEEventHandlers 对象，分发逻辑集中在 dispatchSSEEvent。
export type SSEEventHandlers = {
  [K in SSEEventName]?: (data: SSEEventPayload[K]) => void;
};

// 类型安全的事件分发器。
// event 是运行时字符串（来自 SSE 流解析），无法在类型层收窄到 SSEEventName，
// 因此内部做一次索引访问——这是唯一不可避免的边界断言，集中在此处后，
// 所有调用方都不再需要手写 `as` 或字符串字面量。
export function dispatchSSEEvent(
  event: string,
  data: unknown,
  handlers: SSEEventHandlers,
): void {
  const handler = (handlers as Record<string, ((data: unknown) => void) | undefined>)[event];
  if (handler) handler(data);
}
