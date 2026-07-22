import { create } from "zustand";
import type {
  AnalysisResult,
  Artifact,
  CompletedStep,
  FollowUpMessage,
  HistorySessionItem,
  PlanStep,
  RetryOffer,
  Session,
  Settings,
  TokenUsage,
  ToolTraceItem,
} from "../types";

// setValue/updater 联合类型：与原 useState 行为一致，setter 既接受直接值
// 也接受 functional updater。
type Updater<T> = T | ((prev: T) => T);

// 按 useState 迁移自 App.jsx，state 按功能分组为 slices。
// 所有 setter 行为与原 useState 一致：直接赋值或 functional updater 均支持。
// setError 保留原有包装逻辑——设置非空 error 时把 errorExpanded 重置为 false。
interface AppStoreState {
  // === 认证 slice ===
  authRequired: boolean;
  authenticated: boolean;
  authReady: boolean;
  setAuthRequired: (v: boolean) => void;
  setAuthenticated: (v: boolean) => void;
  setAuthReady: (v: boolean) => void;

  // === 配置 slice ===
  settings: Settings | null;
  apiKey: string;
  effort: string;
  thinking: boolean;
  setSettings: (v: Settings | null) => void;
  setApiKey: (v: string) => void;
  setEffort: (v: string) => void;
  setThinking: (v: boolean) => void;

  // === 会话 slice ===
  session: Session | null;
  activeTab: "analysis" | "data" | "artifacts";
  // lastActiveTab：跨会话切换时持久化用户最后查看的 Tab，
  // 切换历史会话后恢复而非每次回到"分析"，减少重复点击。
  // selectSession 中带 fallback：目标会话无 preview 数据时仍回退到 analysis。
  lastActiveTab: "analysis" | "data" | "artifacts";
  setSession: (updater: Updater<Session | null>) => void;
  setActiveTab: (tab: "analysis" | "data" | "artifacts") => void;
  setLastActiveTab: (tab: "analysis" | "data" | "artifacts") => void;

  // === 分析任务 slice ===
  task: string;
  plan: PlanStep[];
  completed: CompletedStep[];
  result: AnalysisResult | null;
  running: boolean;
  setTask: (v: string) => void;
  setPlan: (v: PlanStep[]) => void;
  setCompleted: (v: CompletedStep[]) => void;
  setResult: (updater: Updater<AnalysisResult | null>) => void;
  setRunning: (v: boolean) => void;

  // === 计划审批 slice ===
  // plan_only=true 时后端在 plan_analysis 后结束流并推送 plan_ready 事件，
  // 前端进入 awaitingApproval 模式：PlanPanel 显示编辑/删除/重排控件，
  // 用户审批后调用 startAnalysis(editedPlan, completed_steps: []) 继续执行。
  // stepProgress：执行中后端推送的当前步骤进度（百分比 / 工具调用数 / 提示）。
  awaitingApproval: boolean;
  pendingObjective: string;
  stepProgress: { progress: number; toolCalls: number; message: string } | null;
  setAwaitingApproval: (v: boolean) => void;
  setPendingObjective: (v: string) => void;
  setStepProgress: (v: { progress: number; toolCalls: number; message: string } | null) => void;

  // === UI slice ===
  uploading: boolean;
  previewItem: Artifact | null;
  previewHtml: string;
  previewLoading: boolean;
  previewError: string;
  currentNodeTitle: string;
  setUploading: (v: boolean) => void;
  setPreviewItem: (v: Artifact | null) => void;
  setPreviewHtml: (v: string) => void;
  setPreviewLoading: (v: boolean) => void;
  setPreviewError: (v: string) => void;
  setCurrentNodeTitle: (updater: Updater<string>) => void;

  // === 错误 slice ===
  // setError 包装逻辑：msg 非空时重置 errorExpanded=false，与原 useCallback 行为一致。
  error: string;
  errorExpanded: boolean;
  setError: (msg: string) => void;
  setErrorExpanded: (updater: Updater<boolean>) => void;

  // === Key UI slice ===
  showKey: boolean;
  keyOpen: boolean;
  setShowKey: (updater: Updater<boolean>) => void;
  setKeyOpen: (updater: Updater<boolean>) => void;

  // === Tool trace slice ===
  // 工具调用时间线：tool_call 追加，tool_result 按 call_id 更新。
  toolTrace: ToolTraceItem[];
  setToolTrace: (updater: Updater<ToolTraceItem[]>) => void;

  // === 多轮对话 slice ===
  // followUps: [{role, content, streaming?, tools?, error?}]
  followUps: FollowUpMessage[];
  followUpInput: string;
  chatRunning: boolean;
  setFollowUps: (updater: Updater<FollowUpMessage[]>) => void;
  setFollowUpInput: (v: string) => void;
  setChatRunning: (v: boolean) => void;

  // === 重试 slice ===
  retryOffer: RetryOffer | null;
  retryChecking: boolean;
  setRetryOffer: (v: RetryOffer | null) => void;
  setRetryChecking: (v: boolean) => void;

  // === Busy slice ===
  // savingSettings / stopping：防止连点发多请求
  savingSettings: boolean;
  stopping: boolean;
  setSavingSettings: (v: boolean) => void;
  setStopping: (v: boolean) => void;

  // === 历史 slice ===
  history: HistorySessionItem[];
  historyLoading: boolean;
  historyError: boolean;
  historyExpanded: boolean;
  switchingSessionId: string | null;
  setHistory: (v: HistorySessionItem[]) => void;
  setHistoryLoading: (v: boolean) => void;
  setHistoryError: (v: boolean) => void;
  setHistoryExpanded: (updater: Updater<boolean>) => void;
  setSwitchingSessionId: (v: string | null) => void;

  // === 计时 slice ===
  // running 时由 setInterval 每秒刷新；非 running 时由 session.elapsed_seconds 计算一次性赋值。
  elapsedSeconds: number | null;
  setElapsedSeconds: (v: number | null) => void;

  // === Reasoning / Usage slice ===
  // DeepSeek reasoning_content 流式累积；usage 在 complete / chat_done 终态携带。
  reasoning: string;
  reasoningStreaming: boolean;
  usage: TokenUsage | null;
  setReasoning: (updater: Updater<string>) => void;
  setReasoningStreaming: (v: boolean) => void;
  setUsage: (v: TokenUsage | null) => void;

  // === 命令面板 / 帮助 slice ===
  // Cmd+K 弹层 + ? 快捷键帮助弹层
  commandOpen: boolean;
  commandQuery: string;
  helpOpen: boolean;
  setCommandOpen: (updater: Updater<boolean>) => void;
  setCommandQuery: (v: string) => void;
  setHelpOpen: (updater: Updater<boolean>) => void;
}

export const useAppStore = create<AppStoreState>((set) => ({
  // === 认证 slice ===
  authRequired: false,
  authenticated: false,
  authReady: false,
  setAuthRequired: (v) => set({ authRequired: v }),
  setAuthenticated: (v) => set({ authenticated: v }),
  setAuthReady: (v) => set({ authReady: v }),

  // === 配置 slice ===
  settings: null,
  apiKey: "",
  effort: "high",
  thinking: true,
  setSettings: (v) => set({ settings: v }),
  setApiKey: (v) => set({ apiKey: v }),
  setEffort: (v) => set({ effort: v }),
  setThinking: (v) => set({ thinking: v }),

  // === 会话 slice ===
  session: null,
  activeTab: "analysis",
  lastActiveTab: "analysis",
  setSession: (updater) =>
    set((state) => ({
      session: typeof updater === "function" ? updater(state.session) : updater,
    })),
  setActiveTab: (tab) => set({ activeTab: tab }),
  setLastActiveTab: (tab) => set({ lastActiveTab: tab }),

  // === 分析任务 slice ===
  task: "",
  plan: [],
  completed: [],
  result: null,
  running: false,
  setTask: (v) => set({ task: v }),
  setPlan: (v) => set({ plan: v }),
  setCompleted: (v) => set({ completed: v }),
  setResult: (updater) =>
    set((state) => ({
      result: typeof updater === "function" ? updater(state.result) : updater,
    })),
  setRunning: (v) => set({ running: v }),

  // === 计划审批 slice ===
  awaitingApproval: false,
  pendingObjective: "",
  stepProgress: null,
  setAwaitingApproval: (v) => set({ awaitingApproval: v }),
  setPendingObjective: (v) => set({ pendingObjective: v }),
  setStepProgress: (v) => set({ stepProgress: v }),

  // === UI slice ===
  uploading: false,
  previewItem: null,
  previewHtml: "",
  previewLoading: false,
  previewError: "",
  currentNodeTitle: "",
  setUploading: (v) => set({ uploading: v }),
  setPreviewItem: (v) => set({ previewItem: v }),
  setPreviewHtml: (v) => set({ previewHtml: v }),
  setPreviewLoading: (v) => set({ previewLoading: v }),
  setPreviewError: (v) => set({ previewError: v }),
  setCurrentNodeTitle: (updater) =>
    set((state) => ({
      currentNodeTitle:
        typeof updater === "function" ? updater(state.currentNodeTitle) : updater,
    })),

  // === 错误 slice ===
  // setError 包装逻辑：msg 非空时重置 errorExpanded=false，与原 useCallback 行为一致。
  error: "",
  errorExpanded: false,
  setError: (msg) =>
    set((state) =>
      msg
        ? { error: msg, errorExpanded: false }
        : { error: msg || "" }
    ),
  setErrorExpanded: (updater) =>
    set((state) => ({
      errorExpanded:
        typeof updater === "function" ? updater(state.errorExpanded) : updater,
    })),

  // === Key UI slice ===
  showKey: false,
  keyOpen: false,
  setShowKey: (updater) =>
    set((state) => ({
      showKey: typeof updater === "function" ? updater(state.showKey) : updater,
    })),
  setKeyOpen: (updater) =>
    set((state) => ({
      keyOpen: typeof updater === "function" ? updater(state.keyOpen) : updater,
    })),

  // === Tool trace slice ===
  // 工具调用时间线：tool_call 追加，tool_result 按 call_id 更新。
  toolTrace: [],
  setToolTrace: (updater) =>
    set((state) => ({
      toolTrace: typeof updater === "function" ? updater(state.toolTrace) : updater,
    })),

  // === 多轮对话 slice ===
  // followUps: [{role, content, streaming?, tools?, error?}]
  followUps: [],
  followUpInput: "",
  chatRunning: false,
  setFollowUps: (updater) =>
    set((state) => ({
      followUps: typeof updater === "function" ? updater(state.followUps) : updater,
    })),
  setFollowUpInput: (v) => set({ followUpInput: v }),
  setChatRunning: (v) => set({ chatRunning: v }),

  // === 重试 slice ===
  retryOffer: null,
  retryChecking: false,
  setRetryOffer: (v) => set({ retryOffer: v }),
  setRetryChecking: (v) => set({ retryChecking: v }),

  // === Busy slice ===
  // savingSettings / stopping：防止连点发多请求
  savingSettings: false,
  stopping: false,
  setSavingSettings: (v) => set({ savingSettings: v }),
  setStopping: (v) => set({ stopping: v }),

  // === 历史 slice ===
  history: [],
  historyLoading: false,
  historyError: false,
  historyExpanded: false,
  switchingSessionId: null,
  setHistory: (v) => set({ history: v }),
  setHistoryLoading: (v) => set({ historyLoading: v }),
  setHistoryError: (v) => set({ historyError: v }),
  setHistoryExpanded: (updater) =>
    set((state) => ({
      historyExpanded:
        typeof updater === "function" ? updater(state.historyExpanded) : updater,
    })),
  setSwitchingSessionId: (v) => set({ switchingSessionId: v }),

  // === 计时 slice ===
  // running 时由 setInterval 每秒刷新；非 running 时由 session.elapsed_seconds 计算一次性赋值。
  elapsedSeconds: null,
  setElapsedSeconds: (v) => set({ elapsedSeconds: v }),

  // === Reasoning / Usage slice ===
  // DeepSeek reasoning_content 流式累积；usage 在 complete / chat_done 终态携带。
  reasoning: "",
  reasoningStreaming: false,
  usage: null,
  setReasoning: (updater) =>
    set((state) => ({
      reasoning: typeof updater === "function" ? updater(state.reasoning) : updater,
    })),
  setReasoningStreaming: (v) => set({ reasoningStreaming: v }),
  setUsage: (v) => set({ usage: v }),

  // === 命令面板 / 帮助 slice ===
  // Cmd+K 弹层 + ? 快捷键帮助弹层
  commandOpen: false,
  commandQuery: "",
  helpOpen: false,
  setCommandOpen: (updater) =>
    set((state) => ({
      commandOpen:
        typeof updater === "function" ? updater(state.commandOpen) : updater,
    })),
  setCommandQuery: (v) => set({ commandQuery: v }),
  setHelpOpen: (updater) =>
    set((state) => ({
      helpOpen:
        typeof updater === "function" ? updater(state.helpOpen) : updater,
    })),
}));
