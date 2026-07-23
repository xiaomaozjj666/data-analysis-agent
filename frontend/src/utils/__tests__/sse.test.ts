/// <reference types="vitest/globals" />
import { consumeSSEStream, parseSSEData } from "../sse";
import { dispatchSSEEvent, SSE_EVENT_TYPES, type SSEEventHandlers } from "../sse-events";

// Build a mock Response whose body.getReader() emits the given string chunks in
// order. consumeSSEStream only touches response.body.getReader(), so this mock
// is sufficient and avoids depending on the real Response/ReadableStream.
function makeResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const reader = {
    read: async () => {
      if (i < chunks.length) {
        return { value: encoder.encode(chunks[i++]), done: false };
      }
      return { value: undefined, done: true };
    },
    releaseLock: () => {},
  };
  return { body: { getReader: () => reader } } as unknown as Response;
}

describe("SSE_EVENT_TYPES", () => {
  it("exposes all expected wire-format event names", () => {
    expect(SSE_EVENT_TYPES.STARTED).toBe("started");
    expect(SSE_EVENT_TYPES.PROGRESS).toBe("progress");
    expect(SSE_EVENT_TYPES.VALIDATE_DATASET).toBe("validate_dataset");
    expect(SSE_EVENT_TYPES.PLAN_ANALYSIS).toBe("plan_analysis");
    expect(SSE_EVENT_TYPES.PLAN_READY).toBe("plan_ready");
    expect(SSE_EVENT_TYPES.STEP_PROGRESS).toBe("step_progress");
    expect(SSE_EVENT_TYPES.EXECUTE_STEP).toBe("execute_step");
    expect(SSE_EVENT_TYPES.REPLAN).toBe("replan");
    expect(SSE_EVENT_TYPES.THINKING_CHUNK).toBe("thinking_chunk");
    expect(SSE_EVENT_TYPES.FINALIZE).toBe("finalize");
    expect(SSE_EVENT_TYPES.REPORT_CHUNK).toBe("report_chunk");
    expect(SSE_EVENT_TYPES.TOOL_CALL).toBe("tool_call");
    expect(SSE_EVENT_TYPES.TOOL_RESULT).toBe("tool_result");
    expect(SSE_EVENT_TYPES.COMPLETE).toBe("complete");
    expect(SSE_EVENT_TYPES.CANCELLED).toBe("cancelled");
    expect(SSE_EVENT_TYPES.ERROR).toBe("error");
    expect(SSE_EVENT_TYPES.HEARTBEAT).toBe("heartbeat");
    expect(SSE_EVENT_TYPES.CHAT_CHUNK).toBe("chat_chunk");
    expect(SSE_EVENT_TYPES.CHAT_DONE).toBe("chat_done");
  });
});

describe("parseSSEData", () => {
  it("parses a valid JSON object", () => {
    expect(parseSSEData('{"a":1}')).toEqual({ a: 1 });
  });
  it("parses a valid JSON array", () => {
    expect(parseSSEData("[1,2,3]")).toEqual([1, 2, 3]);
  });
  it("returns null for invalid JSON", () => {
    expect(parseSSEData("{invalid}")).toBeNull();
  });
  it("returns null for an empty string", () => {
    expect(parseSSEData("")).toBeNull();
  });
});

describe("dispatchSSEEvent", () => {
  it("dispatches to the matching handler with the data", () => {
    const handlers: SSEEventHandlers = {
      progress: vi.fn(),
    };
    dispatchSSEEvent("progress", { title: "loading" }, handlers);
    expect(handlers.progress).toHaveBeenCalledWith({ title: "loading" });
  });

  it("ignores events that have no handler without throwing", () => {
    const handlers: SSEEventHandlers = {
      error: vi.fn(),
    };
    expect(() => dispatchSSEEvent("nonexistent_event", { foo: 1 }, handlers)).not.toThrow();
    expect(handlers.error).not.toHaveBeenCalled();
  });

  it("does not call unrelated handlers", () => {
    const progress = vi.fn();
    const handlers: SSEEventHandlers = { progress, complete: vi.fn() };
    dispatchSSEEvent("progress", { title: "x" }, handlers);
    expect(progress).toHaveBeenCalledTimes(1);
    expect(handlers.complete).not.toHaveBeenCalled();
  });
});

describe("consumeSSEStream", () => {
  it("parses multiple SSE events and dispatches each to its handler", async () => {
    const stream = [
      'event: started\ndata: {}\n\n',
      'event: progress\ndata: {"title":"loading"}\n\n',
      'event: complete\ndata: {"response":"done","artifacts":[],"plan":[],"completed_steps":[]}\n\n',
    ].join("");
    const response = makeResponse([stream]);
    const started = vi.fn();
    const progress = vi.fn();
    const complete = vi.fn();
    await consumeSSEStream(response, { started, progress, complete });
    expect(started).toHaveBeenCalledTimes(1);
    expect(started).toHaveBeenCalledWith({});
    expect(progress).toHaveBeenCalledTimes(1);
    expect(progress).toHaveBeenCalledWith({ title: "loading" });
    expect(complete).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenCalledWith({
      response: "done",
      artifacts: [],
      plan: [],
      completed_steps: [],
    });
  });

  it("invokes onChunk for each data chunk and onDone at the end", async () => {
    const response = makeResponse([
      'event: started\ndata: {}\n\n',
      'event: complete\ndata: {"response":"x","artifacts":[],"plan":[],"completed_steps":[]}\n\n',
    ]);
    const onChunk = vi.fn();
    const onDone = vi.fn();
    await consumeSSEStream(response, {}, { onChunk, onDone });
    // 2 data reads; the final done read does not trigger onChunk.
    expect(onChunk).toHaveBeenCalledTimes(2);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("invokes onEvent with each parsed event name", async () => {
    const response = makeResponse([
      'event: started\ndata: {}\n\n',
      'event: heartbeat\ndata: {}\n\n',
    ]);
    const onEvent = vi.fn();
    await consumeSSEStream(response, {}, { onEvent });
    expect(onEvent).toHaveBeenCalledWith("started");
    expect(onEvent).toHaveBeenCalledWith("heartbeat");
    expect(onEvent).toHaveBeenCalledTimes(2);
  });

  it("ignores SSE comment lines (starting with :)", async () => {
    const stream = ': keep-alive\nevent: started\ndata: {}\n\n';
    const response = makeResponse([stream]);
    const started = vi.fn();
    await consumeSSEStream(response, { started });
    expect(started).toHaveBeenCalledTimes(1);
    expect(started).toHaveBeenCalledWith({});
  });

  it("skips events with malformed JSON data but keeps parsing subsequent events", async () => {
    const stream = [
      'event: progress\ndata: {not valid json}\n\n',
      'event: complete\ndata: {"response":"ok","artifacts":[],"plan":[],"completed_steps":[]}\n\n',
    ].join("");
    const response = makeResponse([stream]);
    const progress = vi.fn();
    const complete = vi.fn();
    await consumeSSEStream(response, { progress, complete });
    expect(progress).not.toHaveBeenCalled();
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it("skips blocks missing an event or data line", async () => {
    const stream = [
      'data: {"orphan":"data"}\n\n',   // no event line
      'event: started\n\n',             // no data line
      'event: complete\ndata: {"response":"ok","artifacts":[],"plan":[],"completed_steps":[]}\n\n',
    ].join("");
    const response = makeResponse([stream]);
    const started = vi.fn();
    const complete = vi.fn();
    await consumeSSEStream(response, { started, complete });
    expect(started).not.toHaveBeenCalled();
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it("skips events with an empty data field", async () => {
    const stream = 'event: started\ndata:\n\n';
    const response = makeResponse([stream]);
    const started = vi.fn();
    await consumeSSEStream(response, { started });
    expect(started).not.toHaveBeenCalled();
  });

  it("reassembles an event split across multiple chunks", async () => {
    const response = makeResponse([
      'event: progress\ndata: {"title":"',   // partial
      'loading"}\n\n',                          // remainder completes the event
    ]);
    const progress = vi.fn();
    await consumeSSEStream(response, { progress });
    expect(progress).toHaveBeenCalledTimes(1);
    expect(progress).toHaveBeenCalledWith({ title: "loading" });
  });

  it("uses only the first data: line when multiple are present", async () => {
    const stream = 'event: progress\ndata: {"title":"first"}\ndata: {"title":"second"}\n\n';
    const response = makeResponse([stream]);
    const progress = vi.fn();
    await consumeSSEStream(response, { progress });
    expect(progress).toHaveBeenCalledTimes(1);
    expect(progress).toHaveBeenCalledWith({ title: "first" });
  });

  it("returns early without calling onDone when the response has no body", async () => {
    const onDone = vi.fn();
    await consumeSSEStream({} as Response, {}, { onDone });
    expect(onDone).not.toHaveBeenCalled();
  });
});
