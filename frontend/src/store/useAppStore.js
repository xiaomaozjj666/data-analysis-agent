import { create } from "zustand";

// 按 useState 迁移自 App.jsx，state 按功能分组为 slices。
// 所有 setter 行为与原 useState 一致：直接赋值或 functional updater 均支持。
// setError 保留原有包装逻辑——设置非空 error 时把 errorExpanded 重置为 false。
export const useAppStore = create((set, get) => ({
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
  setSession: (updater) =>
    set((state) => ({
      session: typeof updater === "function" ? updater(state.session) : updater,
    })),
  setActiveTab: (tab) => set({ activeTab: tab }),

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
