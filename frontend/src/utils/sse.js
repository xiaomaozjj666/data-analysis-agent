// 安全解析 SSE 事件的 data 字段。服务端偶尔会推送畸形 JSON（如被代理截断、
// chunked 编码错误），若直接 JSON.parse 抛 SyntaxError 会中断整个 SSE 流，
// 导致后续事件全部丢失、分析卡死。这里包一层 try/catch，解析失败时跳过该
// 事件并 console.warn 记录原始文本供调试，保证流的健壮性。
function parseSSEData(dataText) {
  try {
    return JSON.parse(dataText);
  } catch (err) {
    console.warn("SSE 事件 JSON 解析失败，已跳过该事件：", err, dataText?.slice(0, 200));
    return null;
  }
}

// SSE 事件流读取器：统一处理 response.body 的流式读取、buffer 拆分、
// event/data 提取和 JSON 解析。消除 startAnalysis 和 startFollowUp 中
// 重复的 SSE 解析逻辑（buffer 拆分、行过滤、event/data 提取、parseSSEData
// 调用），调用方只需提供事件名到处理函数的映射。
//
// 解析规则（与原 startAnalysis/startFollowUp 一致）：
//   - 按 "\n\n" 拆分事件块，最后一块留作 buffer 等待下次拼接
//   - 过滤空行和 ":" 开头的 SSE 注释行（如 ": keep-alive"）
//   - event 行：slice(6).trim()  （"event:" 长 6 字符）
//   - data 行：slice(5).trim()   （"data:" 长 5 字符）
//   - 跳过无 event 或无 data 的块
//   - 通过 parseSSEData 安全解析 JSON，解析失败则跳过该事件
//
// 事件分发：handlers[eventName](data)。若 handlers 中无对应 eventName 则忽略。
// 跨事件状态（如 startAnalysis 的 completedPayload）由调用方通过闭包维护。
//
// @param {Response} response - fetch 返回的 Response 对象
// @param {Object} handlers - 事件处理映射 { eventName: (data) => void }
// @param {Object} options
//   - onChunk?: () => void     每次 reader.read() 返回非 done 时调用（用于重置 idle timer）
//   - onEvent?: (event) => void 每个 event+dataText 齐全的事件（解析前）调用（用于 sawEvent 标记）
//   - onDone?: () => void      流正常结束时调用
//   - signal?: AbortSignal     若提供且已 aborted，AbortError 会被静默吞掉
//   - onError?: (err) => void  捕获异常；未提供则重新抛出
async function consumeSSEStream(response, handlers, options = {}) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (!done && options.onChunk) options.onChunk();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        // 过滤空行和 SSE 注释行（": keep-alive"），避免被当作无 event 的块。
        const lines = block.split("\n").filter((line) => line.length > 0 && !line.startsWith(":"));
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const dataText = lines.find((line) => line.startsWith("data:"))?.slice(5).trim();
        if (!event || !dataText) continue;
        if (options.onEvent) options.onEvent(event);
        const data = parseSSEData(dataText);
        if (!data) continue;
        const handler = handlers[event];
        if (handler) handler(data);
      }
      if (done) break;
    }
    if (options.onDone) options.onDone();
  } catch (err) {
    if (err.name === "AbortError" && options.signal?.aborted) return;
    if (options.onError) options.onError(err);
    else throw err;
  } finally {
    try { reader.releaseLock(); } catch (_) { /* noop */ }
  }
}

export { parseSSEData, consumeSSEStream };
