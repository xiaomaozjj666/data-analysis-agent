import { useRef, type RefObject } from "react";
import { useAppStore } from "../store/useAppStore";
import { api, ApiError, describeApiError, requestHeaders } from "../utils/api";
import { consumeSSEStream } from "../utils/sse";
import { SSE_EVENT_TYPES } from "../utils/sse-events";
import { notifyAnalysisDone } from "../utils/notify";
import { API_URL } from "../constants";
import type { AnalysisResult, CompletedStep, PlanStep, RetryOffer, Session } from "../types";

interface UseAnalysisRunnerDeps {
  // 会话失效（404）时由 App.tsx 提供的重置回调。startAnalysis 捕获到 404
  // 时调用它清空前端状态。它是 function declaration（hoisted），在 hook
  // 调用时已存在；其内部引用的 analysisController / chatControllerRef /
  // retryController 在它被实际调用时均已赋值，故无 TDZ 风险。
  handleSessionLost: (message?: string) => void;
  // 重试轮询控制器：stopAnalysis 需要中断它，与 analysisController 一起 abort。
  retryController: RefObject<AbortController | null>;
  // SSE 断线自动恢复：连接中断（idle 超时 / 中途网络错误）时后台分析
  // 很可能仍在运行，自动触发 App.tsx 的 retryAnalysis 状态轮询恢复结果，
  // 而不是等用户手动点"检查状态"。retryAnalysis 也是 function
  // declaration（hoisted），同 handleSessionLost 无 TDZ 风险。
  autoRecover?: (offer: RetryOffer) => void;
}

interface UseAnalysisRunnerResult {
  startAnalysis: (
    nextTask?: string,
    resumeFrom?: { plan: PlanStep[]; completed_steps: CompletedStep[] } | null,
    planOnly?: boolean,
  ) => Promise<void>;
  stopAnalysis: () => Promise<void>;
  // 共享 ref：App.tsx 的 selectSession / uploadFile / useTimer / retryAnalysis
  // 以及 JSX（runningSessionIdRef）需要读取这些 ref，故从 hook 导出。
  analysisController: RefObject<AbortController | null>;
  startedAtRef: RefObject<number | null>;
  runningSessionIdRef: RefObject<string | null>;
  lastTaskRef: RefObject<string>;
}

// 分析运行器 hook：提取自 App.tsx 的 startAnalysis / stopAnalysis 与相关 ref。
// 行为与原 App.tsx 完全一致——startAnalysis / stopAnalysis 保持为普通函数
// （每次渲染重建，闭包始终读取最新 store 值），与原 function 声明语义相同。
// 相关 ref（控制器、计时、缓冲等）在此创建并按需导出给 App.tsx 共享。
function useAnalysisRunner(deps: UseAnalysisRunnerDeps): UseAnalysisRunnerResult {
  const { handleSessionLost, retryController, autoRecover } = deps;

  // 所有 UI 状态从 Zustand store 获取，与原 App.tsx 同源。
  const {
    session, task, plan, completed, running, stopping,
    setRunning, setError, setResult, setPlan, setCompleted,
    setCurrentNodeTitle, setRetryOffer, setAwaitingApproval, setStepProgress,
    setPendingObjective, setElapsedSeconds, setToolTrace, setReasoning,
    setReasoningStreaming, setUsage, setFollowUps, setTask, setSession,
    setStopping, setRetryChecking,
  } = useAppStore();

  const analysisController = useRef<AbortController | null>(null);
  // 分析开始时间戳（秒）。startAnalysis 时用客户端时间立即赋值，
  // 避免 useEffect 依赖 session.analysis_started_at —— 该字段只在
  // 后端 set_running() 后存在，前端 session 对象在 SSE 期间不会刷新，
  // 依赖它会导致 setInterval 永远不启动，计时停在 0。
  const startedAtRef = useRef<number | null>(null);
  // 正在运行的 SSE 所属 session id。用户切换到历史会话时这个 ref 仍是原 session，
  // SSE 帧到达时若 currentSession.id !== runningSessionId，说明用户在查看历史，
  // 不应覆盖 plan/completed/result 等 UI 状态。
  const runningSessionIdRef = useRef<string | null>(null);
  const cancelRequested = useRef(false);
  const lastTaskRef = useRef("");
  // 流式报告节流缓冲：report_chunk 每个 token 都直接 setResult 会触发
  // ReactMarkdown 全量重解析 AST，长报告（5000+ 字）在低端设备卡顿。
  // 改为缓冲 chunks，80ms 批量刷新一次（每秒约 12 次，人眼感知流畅）。
  const reportBufferRef = useRef("");
  const reportFlushTimerRef = useRef<number | null>(null);

  async function stopAnalysis() {
    if (!session || !running || stopping) return;
    cancelRequested.current = true;
    setStopping(true);
    const cancelRequest = api(`/api/sessions/${session.id}/cancel`, {
      method: "POST",
      timeoutMs: 10000,
    }).catch(() => null);
    // 同时中断 SSE 流和 retry 轮询，确保用户点击停止后所有后台请求都结束。
    analysisController.current?.abort();
    retryController.current?.abort();
    await cancelRequest;
    setRunning(false);
    setStopping(false);
    setRetryChecking(false);
    setCurrentNodeTitle("");
    setRetryOffer(null);
    setError("分析已停止。如果模型正在响应，后端会在本轮结束后安全退出，已完成步骤不会丢失。");
  }

  async function startAnalysis(
    nextTask: string = task,
    resumeFrom: { plan: PlanStep[]; completed_steps: CompletedStep[] } | null = null,
    planOnly: boolean = false,
  ) {
    if (!session || !nextTask.trim() || running) return;
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch { /* noop */ }
    }
    setRunning(true);
    setError("");
    // 断点续跑时不清空 plan/completed，让用户看到已完成的步骤保持绿色对勾状态；
    // 全新分析时才清空。
    if (!resumeFrom) {
      setResult(null);
      setPlan([]);
      setCompleted([]);
    } else {
      // 续跑时保留已有 plan 和 completed，只清空 result（旧报告不再适用）
      setResult(null);
    }
    setCurrentNodeTitle("");
    setRetryOffer(null);
    // 重置计划审批与步骤进度态：新一轮分析从干净状态开始
    setAwaitingApproval(false);
    setStepProgress(null);
    setPendingObjective("");
    // 立即用客户端时间戳启动计时。setInterval 会每秒刷新 elapsedSeconds。
    // complete 帧后用后端返回的精确 elapsed_seconds 覆盖一次，消除客户端
    // 与服务端时钟漂移带来的误差（通常 < 1 秒）。
    startedAtRef.current = Date.now() / 1000;
    // 记录正在运行的 session id，SSE 帧到达时据此判断是否仍在前台查看该会话。
    // 用户切换到历史会话后，runningSessionIdRef 与 session.id 不一致，
    // SSE 处理器跳过 UI 覆盖，避免历史视图被运行中的分析数据覆盖。
    runningSessionIdRef.current = session.id;
    setElapsedSeconds(0);
    // 清空上次的工具调用时间线和报告，为新分析腾出空间
    setToolTrace([]);
    setResult(null);
    // 重置流式报告节流缓冲：清除上一轮可能残留的 pending flush 和缓冲内容
    if (reportFlushTimerRef.current != null) {
      window.clearTimeout(reportFlushTimerRef.current);
      reportFlushTimerRef.current = null;
    }
    reportBufferRef.current = "";
    // 重置 reasoning / usage：新一轮分析的思考过程和用量从 0 开始累计
    setReasoning("");
    setReasoningStreaming(false);
    setUsage(null);
    // 新分析开始时清空追问历史，避免上轮的追问残留混淆当前分析
    setFollowUps([]);
    // 任务已提交运行，清空输入框，避免用户以为还没发起分析。
    // lastTaskRef 仍保留实际任务文本，供 retry 与日志追踪使用。
    setTask("");
    lastTaskRef.current = nextTask;
    const controller = new AbortController();
    analysisController.current = controller;
    cancelRequested.current = false;
    let idleTimeout: number | null = null;
    let completedPayload: AnalysisResult | null = null;
    let sawEvent = false;
    const resetIdleTimeout = () => {
      if (idleTimeout != null) window.clearTimeout(idleTimeout);
      idleTimeout = window.setTimeout(() => controller.abort(), 180000);
    };
    resetIdleTimeout();
    // 节流刷新：将缓冲区内容批量写入 state，避免每个 token 都触发 ReactMarkdown 重解析。
    // 定义在 try 之前，以便 complete/cancelled/error/finally 都能调用。
    const flushReportBuffer = () => {
      if (reportFlushTimerRef.current != null) {
        window.clearTimeout(reportFlushTimerRef.current);
        reportFlushTimerRef.current = null;
      }
      if (reportBufferRef.current) {
        const buffered = reportBufferRef.current;
        reportBufferRef.current = "";
        setResult((prev) => prev
          ? { ...prev, response: buffered }
          : { response: buffered, artifacts: [], plan, completed_steps: completed }
        );
      }
    };
    try {
      const response = await fetch(`${API_URL}/api/sessions/${session.id}/analyze/stream`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ task: nextTask, resume_from: resumeFrom, plan_only: planOnly }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new ApiError(describeApiError(payload, response.status), response.status);
      }
      // SSE 事件分发：buffer 拆分 / event+data 提取 / JSON 解析由
      // consumeSSEStream 统一处理，这里只声明各事件的业务逻辑。
      // 跨事件状态（completedPayload / sawEvent）通过闭包维护。
      // error 事件抛出的异常会被 consumeSSEStream 重新抛出，落到下面的 catch。
      await consumeSSEStream(response, {
        [SSE_EVENT_TYPES.STARTED]: () => {
          if (session.id === runningSessionIdRef.current) setCurrentNodeTitle("后端已接收任务");
        },
        [SSE_EVENT_TYPES.PROGRESS]: (data) => {
          if (session.id === runningSessionIdRef.current) setCurrentNodeTitle(data.title || "正在分析");
        },
        [SSE_EVENT_TYPES.VALIDATE_DATASET]: () => {
          if (session.id === runningSessionIdRef.current) setCurrentNodeTitle("正在检查数据集结构");
        },
        [SSE_EVENT_TYPES.PLAN_ANALYSIS]: (data) => {
          if (session.id === runningSessionIdRef.current) {
            setPlan(data.plan || []);
            setCurrentNodeTitle("正在规划分析步骤");
          }
        },
        [SSE_EVENT_TYPES.PLAN_READY]: (data) => {
          // plan_only=true 时后端在 plan_analysis 后结束流并发送 plan_ready，
          // 前端进入待审阅状态，等待用户编辑并批准计划再执行。
          if (session.id === runningSessionIdRef.current) {
            setPlan(data.plan || []);
            setPendingObjective(data.objective || "");
            setAwaitingApproval(true);
            setCurrentNodeTitle("计划已生成，等待审阅");
            setRunning(false);
          }
        },
        [SSE_EVENT_TYPES.STEP_PROGRESS]: (data) => {
          // 当前步骤的执行进度（百分比 / 工具调用数 / 提示文案）；
          // step_index/total_steps 为复合进度上下文，PlanPanel 据此渲染
          // "步骤 2/4 · 第 3 次工具调用"，旧后端无此字段时隐藏前缀。
          if (session.id === runningSessionIdRef.current) {
            setStepProgress({
              progress: data.progress || 0,
              toolCalls: data.tool_calls || 0,
              message: data.message || "",
              stepIndex: data.step_index || 0,
              totalSteps: data.total_steps || 0,
            });
          }
        },
        [SSE_EVENT_TYPES.EXECUTE_STEP]: () => {
          if (session.id === runningSessionIdRef.current) {
            setCurrentNodeTitle((current) => current || "正在执行分析步骤");
          }
        },
        [SSE_EVENT_TYPES.REPLAN]: (data) => {
          if (session.id === runningSessionIdRef.current) {
            setCompleted(data.completed_steps || []);
            setCurrentNodeTitle("正在审查进度并重规划");
          }
        },
        [SSE_EVENT_TYPES.THINKING_CHUNK]: (data) => {
          // DeepSeek reasoning_content：流式思考过程。开始接收时打开 streaming 标记，
          // ReportView 顶部的 ReasoningBlock 会自动展开；接收完后由 report_chunk / complete
          // 阶段自然关闭 streaming。思考过程让用户看到 Agent 的推理链路，减少"黑盒等待"焦虑。
          if (session.id === runningSessionIdRef.current) {
            if (!data.chunk) return;
            setReasoningStreaming(true);
            setReasoning((prev) => prev + (data.chunk || ""));
          }
        },
        [SSE_EVENT_TYPES.FINALIZE]: () => {
          if (session.id === runningSessionIdRef.current) {
            setCurrentNodeTitle("正在汇总最终报告");
            // finalize 阶段开始输出报告正文，思考过程已结束，关闭 streaming
            setReasoningStreaming(false);
            // 创建空壳 result，让 ReportView 立即显示"正在生成报告…"占位，
            // 后续 report_chunk 事件会逐字追加 response，实现流式打字效果。
            setResult((prev) => prev || { response: "", artifacts: [], plan, completed_steps: completed });
          }
        },
        [SSE_EVENT_TYPES.REPORT_CHUNK]: (data) => {
          // 流式报告：逐字追加，用户看着报告逐字写出，而不是等 30-60 秒看完整报告。
          // 节流：不直接 setResult（每个 token 触发 ReactMarkdown 全量重解析 AST），
          // 而是写入 ref 缓冲，80ms 批量刷新一次。长报告（5000+ 字）在低端设备更流畅。
          if (session.id === runningSessionIdRef.current) {
            reportBufferRef.current += (data.chunk || "");
            if (reportFlushTimerRef.current == null) {
              reportFlushTimerRef.current = window.setTimeout(() => {
                reportFlushTimerRef.current = null;
                if (reportBufferRef.current) {
                  const buffered = reportBufferRef.current;
                  reportBufferRef.current = "";
                  setResult((prev) => prev
                    ? { ...prev, response: buffered }
                    : { response: buffered, artifacts: [], plan, completed_steps: completed }
                  );
                }
              }, 80);
            }
          }
        },
        [SSE_EVENT_TYPES.TOOL_CALL]: (data) => {
          // 工具调用开始：追加到时间线，让用户看到 ReAct 内部正在做什么
          if (session.id === runningSessionIdRef.current) {
            setToolTrace((prev) => [...prev, {
              call_id: data.call_id,
              name: data.name,
              input_preview: data.input_preview,
              status: "running",
              started_at: data.started_at,
            }]);
          }
        },
        [SSE_EVENT_TYPES.TOOL_RESULT]: (data) => {
          // 工具调用结束：更新对应 call_id 的状态和耗时
          if (session.id === runningSessionIdRef.current) {
            setToolTrace((prev) => prev.map((item) => item.call_id === data.call_id
              ? { ...item, status: "done", output_preview: data.output_preview, duration_ms: data.duration_ms }
              : item
            ));
          }
        },
        [SSE_EVENT_TYPES.COMPLETE]: (data) => {
          // complete 帧仍需记录 completedPayload，以便结束后 setRunning(false)
          // 和刷新 history，让历史列表反映新状态（即使用户已切到历史会话）。
          completedPayload = data;
          if (session.id === runningSessionIdRef.current) {
            // 清除节流定时器并丢弃缓冲：complete 帧的 data 是权威最终结果，
            // pending flush 不得覆盖它。
            if (reportFlushTimerRef.current != null) {
              window.clearTimeout(reportFlushTimerRef.current);
              reportFlushTimerRef.current = null;
            }
            reportBufferRef.current = "";
            setResult(data);
            setPlan(data.plan || []);
            setCompleted(data.completed_steps || []);
            // 乐观更新 artifacts，refresh 失败时仍能看到产物。
            setSession((current) => (current ? { ...current, artifacts: data.artifacts || [] } : current));
            setCurrentNodeTitle("");
            // 思考过程与用量收尾：complete 帧可能携带最终 reasoning / usage。
            setReasoningStreaming(false);
            if (data.reasoning) setReasoning(data.reasoning || "");
            if (data.usage) setUsage(data.usage || null);
            // 步骤进度归位：分析完成后不再显示步骤内进度条
            setStepProgress(null);
          }
          notifyAnalysisDone("分析已完成", nextTask || "数据分析任务已完成");
        },
        [SSE_EVENT_TYPES.CANCELLED]: (data) => {
          // 仅在用户未通过 stopAnalysis 主动取消时显示后端取消消息，避免重复 setError。
          if (!cancelRequested.current && session.id === runningSessionIdRef.current) {
            setError(data.message || "分析已取消。");
          }
          // 立即刷新缓冲：让用户看到取消前已生成的报告内容
          flushReportBuffer();
          // 取消时退出审批模式，避免 UI 卡在待审阅状态
          setAwaitingApproval(false);
          setStepProgress(null);
          // 断点续跑：取消时若有已完成步骤，提供"继续分析"入口
          if (completed.length > 0) {
            setRetryOffer({ task: nextTask, reason: "cancelled", canResume: true, plan, completed });
          }
        },
        [SSE_EVENT_TYPES.ERROR]: (data) => {
          // 立即刷新缓冲：让用户看到出错前已生成的报告内容
          flushReportBuffer();
          // 出错时退出审批模式，避免 UI 卡在待审阅状态
          setAwaitingApproval(false);
          setStepProgress(null);
          notifyAnalysisDone("分析失败", data.message || "数据分析任务执行出错");
          throw new Error(data.message || "分析失败");
        },
        // heartbeat: 仅用于保活，重置 idle timer 即可；不更新 UI。
      }, {
        onChunk: resetIdleTimeout,
        onEvent: () => { sawEvent = true; },
      });
      if (completedPayload) {
        // 仅在用户仍在查看运行 session 时才 refresh + 更新 UI；用户切换到
        // 历史会话后不需要把当前 session 的最新状态刷到 UI（历史会话有自己的数据）。
        const stillViewingRunningSession = session.id === runningSessionIdRef.current;
        try {
          const refreshed = await api<Session & { analysis_started_at?: number | null }>(`/api/sessions/${session.id}`);
          if (stillViewingRunningSession) {
            setSession(refreshed);
            // 用服务端精确的 started_at / elapsed_seconds 校正客户端估算，
            // 消除时钟漂移带来的 1 秒以内误差。
            if (refreshed.analysis_started_at) {
              startedAtRef.current = refreshed.analysis_started_at;
            }
            setElapsedSeconds(refreshed.elapsed_seconds ?? null);
          }
        } catch {
          // refresh 失败时保留 complete 帧的乐观更新，不阻塞用户。
        }
      }
    } catch (err) {
      const error = err as Error;
      // 通用：取消/失败时若有已完成步骤，附加 canResume 标记让重试栏显示"继续分析"
      const resumePayload: RetryOffer | null = completed.length > 0
        ? { task: nextTask, canResume: true, plan, completed }
        : null;
      if (error.name === "AbortError" && cancelRequested.current) {
        setError("分析已取消，已完成的步骤不会继续扩展。");
        if (resumePayload) setRetryOffer({ ...resumePayload, reason: "cancelled" });
      } else if (error.name === "AbortError") {
        // idle timeout：长时间未收到事件。后台分析很可能仍在运行（单次 LLM
        // 调用可能超长），自动触发状态轮询恢复，而非等用户手动点检查。
        const offer: RetryOffer = resumePayload ? { ...resumePayload, reason: "idle" } : { task: nextTask, reason: "idle" };
        setRetryOffer(offer);
        if (autoRecover) {
          setError("长时间未收到分析进度，正在自动检查后台任务状态…");
          // setTimeout(0)：让 finally 块先完成 running/nodeTitle 等收尾清理，
          // 再启动恢复轮询，避免轮询内的状态赋值被 finally 覆盖。
          window.setTimeout(() => autoRecover(offer), 0);
        } else {
          setError("长时间未收到分析进度，连接已断开。");
        }
      } else if (!sawEvent && error.name === "TypeError") {
        // fetch 网络层错误（DNS/CORS/离线），尚未收到任何 SSE 帧：
        // 后端大概率没收到任务，不自动轮询，保留手动重试入口。
        setError(`无法连接分析服务：${error.message}`);
        setRetryOffer(resumePayload ? { ...resumePayload, reason: "network" } : { task: nextTask, reason: "network" });
      } else if (error.name === "TypeError") {
        // 中途网络错误（已收到过 SSE 帧后断线）：后台任务仍在运行，
        // 自动触发状态轮询恢复，网络恢复后无需用户介入即可拿到结果。
        const offer: RetryOffer = resumePayload ? { ...resumePayload, reason: "network" } : { task: nextTask, reason: "network" };
        setRetryOffer(offer);
        if (autoRecover) {
          setError("与服务器的连接中断，正在自动检查后台任务状态…");
          window.setTimeout(() => autoRecover(offer), 0);
        } else {
          setError(`与服务器的连接中断：${error.message}`);
        }
      } else if (error instanceof ApiError && error.status === 404) {
        // 重运行时服务端 session 已被清理，引导用户重新上传。
        handleSessionLost("会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。");
      } else {
        setError(error.message);
        setRetryOffer(resumePayload ? { ...resumePayload, reason: "error" } : { task: nextTask, reason: "error" });
      }
    } finally {
      if (idleTimeout != null) window.clearTimeout(idleTimeout);
      // 安全网：客户端 abort（如 idle timeout / stopAnalysis）可能未触发
      // complete/cancelled/error 事件，此处 flush 残留缓冲确保已生成的
      // 报告内容不丢失。complete 已将缓冲清空，此处为空操作，不会覆盖。
      flushReportBuffer();
      if (analysisController.current === controller) analysisController.current = null;
      setRunning(false);
      setCurrentNodeTitle("");
      // 分析结束（无论成功/取消/失败）都关闭 reasoning streaming，
      // 避免下次进入会话时 ReasoningBlock 仍显示流式光标。
      setReasoningStreaming(false);
    }
  }

  return { startAnalysis, stopAnalysis, analysisController, startedAtRef, runningSessionIdRef, lastTaskRef };
}

export default useAnalysisRunner;
