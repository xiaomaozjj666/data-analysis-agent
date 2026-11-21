import {
  AlertTriangle,
  ChevronRight,
  Command,
  FilePlus2,
  FileSpreadsheet,
  Keyboard,
  LoaderCircle,
  Menu,
  Moon,
  Play,
  RefreshCw,
  Sun,
  Upload,
  X,
} from "lucide-react";
import React, {
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { AccessGate, Metric } from "./components/AccessGate";
import ArtifactCenter from "./components/ArtifactCenter";
import CommentsPanel from "./components/CommentsPanel";
import ConversationThread from "./components/ConversationThread";
import DatasetOverview from "./components/DatasetOverview";
import DemoBrief from "./components/DemoBrief";
import MetricsEditor from "./components/MetricsEditor";
import EmptyWorkspace from "./components/EmptyWorkspace";
import HistoryPanel from "./components/HistoryPanel";
import PlanPanel from "./components/PlanPanel";
import PreviewModal from "./components/PreviewModal";
import SalesAudit, { SALES_BRIEF_TASK } from "./components/SalesAudit";
import SettingsPanel from "./components/SettingsPanel";
import TaskBox from "./components/TaskBox";
import WorkspaceTabs from "./components/WorkspaceTabs";
// React Bits 风格动效（手写、零新增依赖）：品牌渐变 / 指标数字滚动 /
// 主 CTA 流光描边 / 数据概览滚动入场，均兼容暗色与 prefers-reduced-motion。
import ClickSpark from "./components/rb/ClickSpark";
import CountUp from "./components/rb/CountUp";
import GlareHover from "./components/rb/GlareHover";
import GradientText from "./components/rb/GradientText";
import Reveal from "./components/rb/Reveal";
import {
  ACTIVE_ANALYSIS_STATES,
  COMMAND_ACTIONS,
  MAX_UPLOAD_BYTES_CLIENT,
} from "./constants";
import useAnalysisRunner from "./hooks/useAnalysisRunner";
import useArtifactPreview from "./hooks/useArtifactPreview";
import useAuthBootstrap from "./hooks/useAuthBootstrap";
import useChatRunner from "./hooks/useChatRunner";
import useDownloads from "./hooks/useDownloads";
import useHistorySync from "./hooks/useHistorySync";
import useScrollProgress from "./hooks/useScrollProgress";
import useSessionMutations from "./hooks/useSessionMutations";
import useShortcuts from "./hooks/useShortcuts";
import useTabPersistence from "./hooks/useTabPersistence";
import useTheme from "./hooks/useTheme";
import useTimer from "./hooks/useTimer";
import { useAnalysisStore, useAppStore, useSessionStore } from "./store/useAppStore";
import type {
  AnalysisResult,
  CommandAction,
  DatasetProfile,
  HistorySessionItem,
  PlanStep,
  RetryOffer,
  Session,
} from "./types";
import { api, ApiError, uploadWithProgress } from "./utils/api";
import { wait } from "./utils/format";
import {
  restoreCompletedAnalysis,
  restoreFollowUps,
} from "./utils/sessionRestore";
// 代码分割/懒加载（#16）：重型组件延迟加载，减小首次 bundle 体积。
// - CommandPalette / HelpPanel：弹层，仅在用户触发（Cmd+K / ?）时显示
// - ReportView：含 ReactMarkdown，仅在分析完成后渲染（result 非空）
// - DataTable：含搜索/排序逻辑，仅在切到"数据" Tab 时渲染
const DataTable = React.lazy(() => import("./components/DataTable"));
const ReportView = React.lazy(() => import("./components/ReportView"));
const CommandPalette = React.lazy(() => import("./components/CommandPalette"));
const HelpPanel = React.lazy(() => import("./components/HelpPanel"));
const LANGSMITH_STATUS = {
  configured: { label: "追踪已配置", dot: "online" },
  missing_key: { label: "未配置（可选）", dot: "" },
  key_unconfirmed: { label: "待核对", dot: "" },
  local: { label: "仅本地追踪", dot: "" },
  disabled: { label: "追踪关闭", dot: "" },
  unknown: { label: "状态未知", dot: "" },
};
// Suspense 懒加载占位：补齐 fallback={null} 的加载态（FIND-UX-001~004），
// 复用 skeleton-shimmer 动画，不改组件 props 语义、不新增用户可见功能。
const LazyFallback = () => (
  <div
    className="lazy-suspense-fallback"
    role="status"
    aria-busy="true"
    aria-label="加载中"
  />
);

// /api/sessions GET 列表响应的解析已随 fetchHistory 移入 useHistorySync。

function App() {
  // 所有 UI 状态从 Zustand store 获取（见 src/store/useAppStore.js）。
  // store 按功能分组为 slices：认证 / 配置 / 会话 / 分析任务 / UI / 错误 /
  // Key / Tool trace / 多轮对话 / 重试 / Busy / 历史 / 计时 / Reasoning / 命令面板。
  // setError 保留原 useCallback 包装逻辑：msg 非空时重置 errorExpanded=false。
  // setter 既接受直接值也接受 functional updater，与原 useState 行为一致。
  const {
    // 认证
    authRequired,
    authenticated,
    authReady,
    setAuthRequired,
    setAuthenticated,
    setAuthReady,
    // 配置（apiKey/effort/thinking 的编辑逻辑已移至 SettingsPanel；
    // setSettings/setEffort/setThinking 仍供 useAuthBootstrap 初始化）
    settings,
    setSettings,
    setEffort,
    setThinking,
    // 会话
    session,
    activeTab,
    setSession,
    setActiveTab,
    lastActiveTab,
    setLastActiveTab,
    // 分析任务
    task,
    plan,
    planSource,
    replanReason,
    completed,
    result,
    running,
    setTask,
    setPlan,
    setCompleted,
    setResult,
    setRunning,
    // 计划审批：plan_only 流程的待审阅状态、步骤进度
    awaitingApproval,
    pendingObjective,
    stepProgress,
    setAwaitingApproval,
    setPendingObjective,
    setStepProgress,
    // UI（previewHtml/previewLoading/previewError 及其 setter 已交由
    // useArtifactPreview / PreviewModal 消费，App 不再直接读写）
    uploading,
    uploadProgress,
    previewItem,
    currentNodeTitle,
    setUploading,
    setUploadProgress,
    setCurrentNodeTitle,
    // 错误
    error,
    errorExpanded,
    setError,
    setErrorExpanded,
    // Key UI（showKey 已移至 SettingsPanel；keyOpen/setKeyOpen 供快捷键与命令面板使用）
    keyOpen,
    setKeyOpen,
    // 工具调用时间线（toolTrace）：ReAct 执行器内部每次工具调用实时推送，
    // 让用户看到"正在读取数据→正在清洗→正在生成图表"的过程
    toolTrace,
    setToolTrace,
    // 多轮对话：followUps 存储报告之后的追问消息对（user+assistant），
    // 让用户基于已有分析结果继续提问，不必每次都触发完整 plan→execute→finalize。
    // 结构：[{role, content, streaming?, tools?, error?}]
    followUps,
    followUpInput,
    chatRunning,
    setFollowUps,
    setFollowUpInput,
    setChatRunning,
    // 重试
    retryOffer,
    retryChecking,
    setRetryOffer,
    setRetryChecking,
    // Busy：停止分析的 busy 状态（保存设置的 busy 已移至 SettingsPanel）
    stopping,
    setStopping,
    // 历史（historyError 区分"没数据"和"加载失败"，避免用户误以为数据丢失）。
    // setHistory/setHistoryLoading/setHistoryError 已随 fetchHistory 移入
    // useHistorySync，这里只读列表状态和展开开关。
    history,
    historyLoading,
    historyError,
    historyExpanded,
    switchingSessionId,
    setHistoryExpanded,
    setSwitchingSessionId,
    // 计时：running 时由 setInterval 每秒刷新；非 running 时由
    // session.elapsed_seconds / completed - started 计算一次性赋值
    elapsedSeconds,
    setElapsedSeconds,
    // Reasoning（DeepSeek reasoning_content）：分析/追问时后端推送 thinking_chunk，
    // 累积到这里让 ReportView / ConversationBubble 展示思考过程。
    // 主分析的 reasoning 放在 App 级别（单条），追问的 reasoning 内嵌在每条 assistant 气泡上。
    // Token 用量（usage）：complete / chat_done 事件携带，展示在报告/气泡底部。
    reasoning,
    reasoningStreaming,
    usage,
    setReasoning,
    setReasoningStreaming,
    setUsage,
    // 命令面板（Cmd+K）与快捷键帮助（?）弹层状态
    commandOpen,
    commandQuery,
    helpOpen,
    setCommandOpen,
    setCommandQuery,
    setHelpOpen,
  } = useAppStore();

  const traceStatus = settings?.langsmith_status
    ?? (settings ? settings.langsmith_tracing ? "configured" : "disabled" : "unknown");
  const traceIndicator = LANGSMITH_STATUS[traceStatus] ?? LANGSMITH_STATUS.unknown;

  // refs 保持局部状态：useRef 不迁移到 store（控制器、缓存、闭包内
  // 读取的最新值不需要触发重渲染）。
  // 分析 / 追问 / 预览相关 ref 已随逻辑提取至 useAnalysisRunner /
  // useChatRunner / useArtifactPreview，这里仅保留 App.tsx 仍直接使用的 ref。
  const fileInput = useRef<HTMLInputElement>(null);
  const auditDemoSession = useRef<string | null>(null);
  const taskInput = useRef<HTMLTextAreaElement>(null);
  const retryController = useRef<AbortController | null>(null);
  // 上传取消控制器：大文件上传过程中用户可点"取消上传"中断 XHR
  const uploadControllerRef = useRef<AbortController | null>(null);
  // 按 sessionId 保存草稿：切换历史会话时不丢失当前正在输入的任务
  const taskDraftsRef = useRef<Record<string, string>>({});

  // 主题（light/dark）：useTheme 内部读取 localStorage，无则跟随系统。
  const { theme, toggle: toggleTheme, setTheme } = useTheme();

  // === 分析 / 追问 / 产物预览逻辑提取至独立 hook ===
  // useAnalysisRunner：startAnalysis / stopAnalysis 及 startedAtRef /
  // runningSessionIdRef / lastTaskRef / analysisController 等共享 ref。
  // handleSessionLost 为 function declaration（hoisted），此处即可引用；
  // 其内部读取的 analysisController / chatControllerRef / retryController
  // 在它被实际调用时均已赋值，无 TDZ 风险。
  const {
    startAnalysis,
    stopAnalysis,
    analysisController,
    startedAtRef,
    runningSessionIdRef,
    lastTaskRef,
  } = useAnalysisRunner({
    handleSessionLost,
    retryController,
    autoRecover: retryAnalysis,
  });
  // useChatRunner：startFollowUp / stopFollowUp 及 chatControllerRef /
  // followUpInputRef（供 handleSessionLost / deleteSession / ConversationThread 共享）。
  const { startFollowUp, stopFollowUp, chatControllerRef, followUpInputRef } =
    useChatRunner();
  // useArtifactPreview：图表预览模态、对比/全屏/PNG 导出、图表内联编辑。
  // 完整返回值整体传给 PreviewModal，App 仅直接使用开/关两个回调。
  const artifactPreview = useArtifactPreview();
  const { openArtifactPreview, closeArtifactPreview } = artifactPreview;
  // useDownloads：单产物下载 / 批量下载 / 会话导出 ZIP（exportingSessionId
  // 供 HistoryPanel 在对应条目上展示导出 loading）。
  const { downloadArtifact, batchDownload, exportSession, exportingSessionId } =
    useDownloads();

  const [replayBusy, setReplayBusy] = useState(false);
  const replayLastAnalysis = useCallback(async () => {
    if (!session || replayBusy || running) return;
    setReplayBusy(true);
    setActiveTab("analysis");
    if (lastTaskRef.current) setTask(lastTaskRef.current);
    try {
      const payload = await api<AnalysisResult>(`/api/sessions/${session.id}/replay`, {
        method: "POST",
        timeoutMs: 300000,
      });
      if (useSessionStore.getState().session?.id !== session.id) return;
      setSession((current) => current ? { ...current, last_result: payload } : current);
      setResult(payload);
      if (payload.plan) setPlan(payload.plan);
      if (payload.completed_steps) setCompleted(payload.completed_steps);
    } catch (err) {
      window.alert(`按配方重跑失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setReplayBusy(false);
    }
  }, [session, replayBusy, running, setResult, setSession, setPlan, setCompleted, setActiveTab, setTask, lastTaskRef]);

  const focusPlanPanel = useCallback(() => {
    setActiveTab("analysis");
    window.requestAnimationFrame(() => {
      document.querySelector(".plan-panel")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }, [setActiveTab]);

  // 移动端侧边栏抽屉开关：桌面端 sidebar 常驻，平板/手机折叠为抽屉。
  // sidebarToggleRef 用于 Esc 关闭抽屉后把焦点还给触发按钮。
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const sidebarToggleRef = useRef<HTMLButtonElement>(null);

  // 体验简报载入后，把焦点送到「简报核验」Tab 面板，让键盘用户从
  // 空状态一键进入核验流程（而不是落在被卸载的上传按钮上）。
  const auditPanelRef = useRef<HTMLElement>(null);
  const focusAuditOnLoad = useRef(false);
  // 读屏用的全局状态播报（polite）：载入示例、切换核验视图等异步状态。
  const [liveMessage, setLiveMessage] = useState("");

  // 分析报告 → 简报核验：保存用户点击「送审并核验」时上送的正文。
  // SalesAudit 自己负责从报告里提取草稿（复用其「使用分析报告草稿」按钮），
  // 这里只做 Tab 切换与把正文注入 report prop，不重复实现提取逻辑。
  const [auditReport, setAuditReport] = useState<string | null>(null);
  const sendReportToAudit = useCallback(
    (reportText: string) => {
      setAuditReport(reportText);
      // 复用已有焦点移交逻辑：Tab 切换后把焦点送到核验面板，键盘用户不迷路。
      focusAuditOnLoad.current = true;
      setActiveTab("audit");
      setLiveMessage("已将分析报告送入简报核验，请在草稿工作台点击「使用分析报告草稿」。");
    },
    [setActiveTab],
  );

  useEffect(() => {
    if (!sidebarOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setSidebarOpen(false);
      // 抽屉关闭后焦点不能留在已隐藏的侧栏里，交还给触发按钮。
      window.requestAnimationFrame(() => sidebarToggleRef.current?.focus());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sidebarOpen]);

  useEffect(() => {
    if (!focusAuditOnLoad.current || activeTab !== "audit" || !session) return;
    focusAuditOnLoad.current = false;
    const frame = window.requestAnimationFrame(() => {
      auditPanelRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeTab, session]);

  // 移动端手势关闭抽屉（#4）：在抽屉上向左滑动即可收起。
  // 记录 touchstart 坐标，touchend 时判断为"水平向左滑动为主且超过阈值"才关闭，
  // 避免与列表纵向滚动冲突。桌面端 sidebar 常驻，这些处理器不会触发。
  const sidebarTouchX = useRef<number | null>(null);
  const sidebarTouchY = useRef<number | null>(null);
  const onSidebarTouchStart = (e: React.TouchEvent) => {
    if (!sidebarOpen) return;
    const t = e.touches[0];
    sidebarTouchX.current = t.clientX;
    sidebarTouchY.current = t.clientY;
  };
  const onSidebarTouchEnd = (e: React.TouchEvent) => {
    if (sidebarTouchX.current == null || sidebarTouchY.current == null) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - sidebarTouchX.current;
    const dy = t.clientY - sidebarTouchY.current;
    // 向左滑动（dx 为负）超过 70px，且横向位移明显大于纵向（1.5 倍）
    // → 判定为关闭手势；比之前的 55px/1 倍更严，避免斜向滚动列表时误关。
    if (dx < -70 && Math.abs(dx) > Math.abs(dy) * 1.5) setSidebarOpen(false);
    sidebarTouchX.current = null;
    sidebarTouchY.current = null;
  };

  // Scroll 进度指示器：顶栏底部品牌色进度条。
  useScrollProgress();

  // Tab 持久化：activeTab 变化时同步到 lastActiveTab（提取至 useTabPersistence）。
  useTabPersistence(activeTab, setLastActiveTab);

  // 历史会话列表同步：fetchHistory + 分析结束刷新 + 自动轮询（提取至 useHistorySync）。
  // 必须在 useAuthBootstrap 之前调用：引导流程鉴权就绪后会调用 fetchHistory。
  const { fetchHistory } = useHistorySync({
    authReady,
    authRequired,
    authenticated,
    running,
  });

  // 认证引导 + 鉴权就绪后拉取历史（提取至 useAuthBootstrap）。
  useAuthBootstrap({
    setAuthRequired,
    setAuthenticated,
    setAuthReady,
    setSettings,
    setEffort,
    setThinking,
    setError,
    fetchHistory,
    authReady,
    authRequired,
    authenticated,
  });

  // 切换到历史会话：拉取完整 session payload，并恢复 result/plan/completed。
  // 失败时（404 等）按 handleSessionLost 处理，避免遗留半残状态。
  // 注意：running 时不覆盖 startedAtRef —— 当前 SSE 流仍在后台跑原分析，
  // 计时应继续基于原分析的 started_at，否则会跳到新会话的 started_at
  // 导致"已耗时"突然变成一个不相关的数字。
  const selectSession = async (item: HistorySessionItem) => {
    if (!item?.id || item.id === session?.id || switchingSessionId) return;
    // 保存当前会话的草稿，切换后还能恢复（用户输入了一半任务还没运行）
    if (session?.id) taskDraftsRef.current[session.id] = task;
    setError("");
    closeArtifactPreview();
    setSidebarOpen(false);
    setSwitchingSessionId(item.id);
    try {
      const latest = await api<Session>(`/api/sessions/${item.id}`);
      setSession(latest);
      // 恢复目标会话的草稿（若有），否则清空
      setTask(taskDraftsRef.current[item.id] || "");
      setPlan([]);
      setCompleted([]);
      setResult(null);
      setCurrentNodeTitle("");
      setRetryOffer(null);
      setAuditReport(null);
      // 切换会话时清理审批/进度态，避免新会话复用旧审批 UI
      setAwaitingApproval(false);
      setPendingObjective("");
      setStepProgress(null);
      restoreCompletedAnalysis(latest, { setResult, setPlan, setCompleted });
      restoreFollowUps(latest, setFollowUps);
      // 历史列表可能不返回 demo 元数据（只给轻量摘要），此时以完整会话里的
      // demo 为准。只有销售核验示例才进入路演；regional_policy 等演示仍走分析页。
      const demo = item.demo ?? latest.demo;
      const openAuditDemo = demo?.open_audit === true && demo?.kind === "sales_audit";
      auditDemoSession.current = openAuditDemo ? item.id : null;
      if (!running) {
        startedAtRef.current =
          (latest as Session & { analysis_started_at?: number | null })
            .analysis_started_at ?? null;
        setElapsedSeconds(latest.elapsed_seconds ?? null);
      }
      // 恢复上次查看的 Tab：有 preview 数据时恢复 lastActiveTab（如数据/产物），
      // 无数据时回退 analysis，避免切到空白的数据 Tab。
      if (openAuditDemo) setActiveTab("audit");
      else setActiveTab(latest.preview?.length ? lastActiveTab : "analysis");
      fetchHistory();
    } catch (err) {
      const error = err as Error & { status?: number };
      if (error instanceof ApiError && error.status === 404) {
        handleSessionLost(
          "该历史会话已被服务端清理，请选择其他会话或重新上传数据。",
        );
      } else {
        setError(`无法打开历史会话：${error.message}`);
      }
    } finally {
      setSwitchingSessionId(null);
    }
  };

  // 会话增删改：导入 / 删除 / 重命名（提取至 useSessionMutations）。
  // 在 selectSession 定义之后调用：导入成功后要复用它跳转新会话。
  const { importSession, deleteSession, renameSession } = useSessionMutations({
    selectSession,
    fetchHistory,
    retryController,
    analysisController,
    chatControllerRef,
  });

  // 实时耗时：running 时持续刷新 elapsed = now - startedAtRef（提取至 useTimer）。
  useTimer(running, startedAtRef, setElapsedSeconds);

  // 命令面板动作执行器：根据 action.id 路由到具体操作。
  // 用 useCallback 保持身份稳定，作为 props 传入 CommandPalette 时不会触发重渲染。
  const runCommandAction = useCallback(
    (action: CommandAction) => {
      switch (action?.id) {
        case "new-analysis":
          fileInput.current?.click();
          break;
        case "toggle-theme":
          toggleTheme();
          break;
        case "open-settings":
          setKeyOpen(true);
          break;
        case "tab-analysis":
          setActiveTab("analysis");
          break;
        case "tab-data":
          setActiveTab("data");
          break;
        case "tab-artifacts":
          setActiveTab("artifacts");
          break;
        case "tab-comments":
          setActiveTab("comments");
          break;
        case "show-help":
          setHelpOpen(true);
          break;
        default:
          break;
      }
      setCommandOpen(false);
      setCommandQuery("");
    },
    [toggleTheme],
  );

  // 消息编辑重发：截断 index 之后的所有 followUps，把新文本作为新追问重发。
  // 参考 ChatGPT/Claude 的"编辑并重新发送"交互——保留历史上下文的同时重置分支。
  const handleEditFollowUp = useCallback(
    (index: number, newContent: string) => {
      setFollowUps((prev) => prev.slice(0, index));
      setFollowUpInput(newContent);
      // 让输入框立即获得焦点，用户可直接 Cmd+Enter 发送
      window.setTimeout(() => followUpInputRef.current?.focus(), 0);
    },
    [],
  );

  // useCallback：downloadArtifact 作为 props 传给 React.memo(ArtifactCenter)。
  // 若每次渲染都创建新函数，memo 比较失败，ArtifactCenter 仍然每次重渲染。
  // useCallback 让函数身份稳定，memo 才能真正跳过无关重渲染。
  // openArtifactPreview / closeArtifactPreview 已提取至 useArtifactPreview。
  // 键盘快捷键：Esc 关闭预览 + 全局 Cmd+K/Cmd+B/Cmd+./?/T/1-3（提取至 useShortcuts）。
  // closeArtifactPreview / stopAnalysis / stopFollowUp 由 useArtifactPreview /
  // useAnalysisRunner / useChatRunner 返回（hook 在上方已调用），此处可直接引用。
  useShortcuts({
    previewItem,
    keyOpen,
    closeArtifactPreview,
    running,
    chatRunning,
    stopAnalysis,
    stopFollowUp,
    toggleTheme,
    setCommandOpen,
    setHistoryExpanded,
    setHelpOpen,
    setActiveTab,
  });

  // loadCompareChart / downloadPng 已提取至 useArtifactPreview；
  // downloadArtifact / batchDownload / exportSession 已提取至 useDownloads；
  // importSession / deleteSession / renameSession 已提取至 useSessionMutations。

  // ---- 候选数据集显式采纳（修复"分析工具静默改主数据"） ----
  // 采纳/忽略/重扫后都重新拉取会话，让主数据预览、行数与候选横幅同步刷新。
  const [adoptBusy, setAdoptBusy] = useState(false);
  const [rescanBusy, setRescanBusy] = useState(false);
  const refreshCurrentSession = useCallback(async () => {
    if (!session?.id) return;
    const latest = await api<Session>(`/api/sessions/${session.id}`);
    setSession(latest);
  }, [session?.id]);

  const adoptCandidateDataset = useCallback(
    async (name: string) => {
      if (!session?.id) return;
      setAdoptBusy(true);
      try {
        await api(`/api/sessions/${session.id}/datasets/adopt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        await refreshCurrentSession();
      } catch (err) {
        setError(`采纳候选数据集失败：${(err as Error).message}`);
      } finally {
        setAdoptBusy(false);
      }
    },
    [session?.id, refreshCurrentSession],
  );

  const discardCandidateDataset = useCallback(
    async (name: string) => {
      if (!session?.id) return;
      setAdoptBusy(true);
      try {
        await api(`/api/sessions/${session.id}/datasets/adopt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, discard: true }),
        });
        await refreshCurrentSession();
      } catch (err) {
        setError(`忽略候选数据集失败：${(err as Error).message}`);
      } finally {
        setAdoptBusy(false);
      }
    },
    [session?.id, refreshCurrentSession],
  );

  const rescanArtifacts = useCallback(async () => {
    if (!session?.id) return;
    setRescanBusy(true);
    try {
      await api(`/api/sessions/${session.id}/artifacts/rescan`, { method: "POST" });
      await refreshCurrentSession();
    } catch (err) {
      setError(`扫描产物目录失败：${(err as Error).message}`);
    } finally {
      setRescanBusy(false);
    }
  }, [session?.id, refreshCurrentSession]);

  // editChart 及"打开预览时初始化编辑表单"的 useEffect 已提取至 useArtifactPreview。
  // 连接测试 testConnection / 保存设置 saveSettings 已随设置面板迁至 SettingsPanel。

  async function uploadFile(file: File | undefined | null) {
    if (!file) return;
    // 客户端文件大小校验：优先用服务端下发的 max_upload_bytes（/api/settings），
    // 旧后端无此字段时回退本地默认值。提前检查可以避免上传 100MB+
    // 才得到 422，浪费用户带宽和等待时间。
    const maxUploadBytes =
      settings?.max_upload_bytes ?? MAX_UPLOAD_BYTES_CLIENT;
    if (file.size > maxUploadBytes) {
      const mb = Math.round(maxUploadBytes / (1024 * 1024));
      setError(
        `文件 ${file.name} 超过 ${mb}MB 上传上限，请拆分或精简后再上传。`,
      );
      if (fileInput.current) fileInput.current.value = "";
      return;
    }
    setUploading(true);
    setUploadProgress(0);
    setError("");
    // 上传新数据集时清理上一个会话的残留 UI 状态，避免预览/重试/进度泄漏到新会话。
    closeArtifactPreview();
    setPlan([]);
    setCompleted([]);
    setResult(null);
    setTask("");
    setAuditReport(null);
    setActiveTab("analysis");
    setCurrentNodeTitle("");
    setRetryOffer(null);
    const form = new FormData();
    form.append("file", file);
    // XHR 上传：支持真实进度（upload.onprogress）与中途取消，
    // 大文件不再只有一个无限转圈的"正在读取"。
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    try {
      const value = await uploadWithProgress<Session>("/api/sessions", form, {
        onProgress: setUploadProgress,
        signal: controller.signal,
      });
      setSession(value);
      startedAtRef.current =
        (value as Session & { analysis_started_at?: number | null })
          .analysis_started_at ?? null;
      setElapsedSeconds(value.elapsed_seconds ?? null);
      setFollowUps([]);
      fetchHistory();
    } catch (err) {
      const error = err as Error;
      if (error.name === "AbortError") {
        setError(`已取消上传 ${file.name}。`);
      } else {
        setError(error.message);
      }
    } finally {
      if (uploadControllerRef.current === controller)
        uploadControllerRef.current = null;
      setUploading(false);
      setUploadProgress(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  // 取消进行中的上传：中断 XHR，uploadFile 的 catch 分支给出轻提示。
  const cancelUpload = useCallback(() => {
    uploadControllerRef.current?.abort();
  }, []);

  // EmptyWorkspace 派发的两个自定义事件：
  //  - empty-workspace:load-sample —— 用户点击"加载示例数据体验"，后端用内置样例建会话
  //  - empty-workspace:extra-files  —— 多文件拖入时，首个文件已走主上传路径，
  //    其余文件在此顺序创建独立会话（数据分析常需对比多表）
  useEffect(() => {
    const onLoadSample = async (event: Event) => {
      const detail = (event as CustomEvent<{ auditDemo?: boolean; kind?: string }>).detail;
      const auditDemo = detail?.auditDemo === true;
      const kind = detail?.kind;
      setLiveMessage(auditDemo ? "正在载入体验简报…" : "正在打开示例…");
      if (auditDemo) focusAuditOnLoad.current = true;
      setUploading(true);
      setError("");
      closeArtifactPreview();
      setPlan([]);
      setCompleted([]);
      setResult(null);
      setTask("");
      setAuditReport(null);
      setActiveTab("analysis");
      setCurrentNodeTitle("");
      setRetryOffer(null);
      try {
        const path = kind
          ? `/api/sessions/sample?kind=${encodeURIComponent(kind)}`
          : auditDemo
            ? "/api/sessions/sample?audit=true"
            : "/api/sessions/sample";
        const value = await api<Session>(path, { method: "POST" });
        auditDemoSession.current = auditDemo ? value.id : null;
        setSession(value);
        if (auditDemo) {
          setActiveTab("audit");
          setLiveMessage("体验简报已载入，已切换到简报核验视图。可用 Tab 进入草稿工作台开始核验。");
        } else {
          setLiveMessage("示例数据已载入，已切换到分析视图。");
          window.setTimeout(() => {
            document.getElementById("tabpanel-analysis")?.focus();
          }, 200);
        }
        startedAtRef.current =
          (value as Session & { analysis_started_at?: number | null })
            .analysis_started_at ?? null;
        setElapsedSeconds(value.elapsed_seconds ?? null);
        setFollowUps([]);
        fetchHistory();
      } catch (err) {
        setError((err as Error).message);
        setLiveMessage("示例载入失败，请查看错误提示。");
        if (auditDemo) focusAuditOnLoad.current = false;
      } finally {
        setUploading(false);
      }
    };
    const onExtraFiles = async (event: Event) => {
      const files = (event as CustomEvent).detail?.files as File[] | undefined;
      if (!files?.length) return;
      // 顺序上传：每个文件独立成会话，避免并发挤占 slot
      for (const file of files) await uploadFile(file);
    };
    window.addEventListener(
      "empty-workspace:load-sample",
      onLoadSample as EventListener,
    );
    window.addEventListener(
      "empty-workspace:extra-files",
      onExtraFiles as EventListener,
    );
    return () => {
      window.removeEventListener(
        "empty-workspace:load-sample",
        onLoadSample as EventListener,
      );
      window.removeEventListener(
        "empty-workspace:extra-files",
        onExtraFiles as EventListener,
      );
    };
  }, []);

  // restoreCompletedAnalysis / restoreFollowUps 已提取至 utils/sessionRestore.ts
  //（纯函数，selectSession 与 retryAnalysis 共用）。

  // 会话失效（404）时清空前端状态，引导用户回到上传界面。
  // Render 免费实例重启会清空 /tmp，session 数据不可恢复，与其让用户
  // 反复点"检查状态"得到 404，不如明确告知并重置。
  function handleSessionLost(message?: string) {
    setSession(null);
    setResult(null);
    setAuditReport(null);
    setPlan([]);
    setCompleted([]);
    setCurrentNodeTitle("");
    setRetryOffer(null);
    setFollowUps([]);
    retryController.current?.abort();
    analysisController.current?.abort();
    chatControllerRef.current?.abort();
    setError(message || "会话已失效，请重新上传数据集后再开始分析。");
  }

  // 核验页会话失效后的「重新载入示例」：SalesAudit 负责创建示例会话，
  // 这里只负责把新会话切为当前会话并清理旧会话残留的分析状态。
  async function reloadAuditSession(id: string) {
    if (!id) return;
    closeArtifactPreview();
    setError("");
    setLiveMessage("正在重新载入体验简报…");
    focusAuditOnLoad.current = true;
    try {
      const latest = await api<Session>(`/api/sessions/${id}`);
      auditDemoSession.current = id;
      setSession(latest);
      setTask("");
      setPlan([]);
      setCompleted([]);
      setResult(null);
      setAuditReport(null);
      setCurrentNodeTitle("");
      setRetryOffer(null);
      setFollowUps([]);
      setAwaitingApproval(false);
      setPendingObjective("");
      setStepProgress(null);
      startedAtRef.current =
        (latest as Session & { analysis_started_at?: number | null })
          .analysis_started_at ?? null;
      setElapsedSeconds(latest.elapsed_seconds ?? null);
      setActiveTab("audit");
      setLiveMessage("体验简报已重新载入，已切换到简报核验视图。");
      fetchHistory();
    } catch (err) {
      focusAuditOnLoad.current = false;
      setError(`无法载入示例会话：${(err as Error).message}`);
    }
  }

  // offerOverride：SSE 断线自动恢复（useAnalysisRunner 的 autoRecover）直接
  // 传入刚构造的 offer——此时 store 的 retryOffer 虽已 set，但本函数闭包
  // 捕获的仍是上一次渲染的旧值（可能为 null），显式传参避开闭包陷阱。
  async function retryAnalysis(offerOverride?: RetryOffer) {
    const offer = offerOverride ?? retryOffer;
    if (!offer) return;
    const retryTask = offer.task;
    if (offer.reason === "ready") {
      setRetryOffer(null);
      startAnalysis(retryTask);
      return;
    }

    if (!session) return;
    // 轮询期间使用独立的 AbortController，让 stopAnalysis 能中断轮询。
    retryController.current?.abort();
    const controller = new AbortController();
    retryController.current = controller;

    setRetryChecking(true);
    try {
      // 单次查询用短超时（8s），让网络故障快速暴露而不是卡 45s。
      let latest = await api<Session & { analysis_started_at?: number | null }>(
        `/api/sessions/${session.id}`,
        { timeoutMs: 8000, signal: controller.signal },
      );
      setSession(latest);

      if (ACTIVE_ANALYSIS_STATES.has(latest.analysis_status)) {
        setRunning(true);
        setCurrentNodeTitle("原分析仍在后台运行，正在等待结果");
        setError("连接已恢复，原分析仍在运行；不会重复提交任务。");
        // 进入轮询前立即同步 startedAtRef，让 setInterval 用服务端的
        // started_at 开始计时（避免用客户端的 Date.now() 把已运行的
        // 几分钟全部算成"刚开始"）。
        startedAtRef.current = latest.analysis_started_at ?? Date.now() / 1000;
        setElapsedSeconds(latest.elapsed_seconds ?? 0);
        const deadline = Date.now() + 5 * 60 * 1000;
        while (
          ACTIVE_ANALYSIS_STATES.has(latest.analysis_status) &&
          Date.now() < deadline
        ) {
          await wait(3000);
          if (controller.signal.aborted) break;
          latest = await api<Session & { analysis_started_at?: number | null }>(
            `/api/sessions/${session.id}`,
            { timeoutMs: 8000, signal: controller.signal },
          );
          setSession(latest);
          // 每 3 秒由后端返回的 elapsed_seconds 同步一次，比 setInterval
          // 的客户端估算更准（客户端时钟漂移、tab 后台限流都会影响）。
          setElapsedSeconds(latest.elapsed_seconds ?? null);
        }
      }

      setRunning(false);
      setCurrentNodeTitle("");
      startedAtRef.current = latest.analysis_started_at ?? null;
      setElapsedSeconds(latest.elapsed_seconds ?? null);
      if (controller.signal.aborted) {
        // 用户主动取消轮询，不修改 error（stopAnalysis 已设过消息）。
        return;
      }
      if (
        latest.analysis_status === "completed" &&
        restoreCompletedAnalysis(latest, { setResult, setPlan, setCompleted })
      ) {
        setError("");
        setRetryOffer(null);
      } else if (ACTIVE_ANALYSIS_STATES.has(latest.analysis_status)) {
        // 5 分钟 deadline 到了但后端仍在 running——可能是 Render free 实例被
        // 暂停、worker 死亡或 LLM 调用超长。提供"强制重新运行"让用户能
        // 中断旧任务重跑，而不是无限等待。
        setError(
          "原分析长时间未结束（可能服务被暂停或任务卡住）。可以选择强制重新运行，将提交新的分析任务。",
        );
        setRetryOffer({ task: retryTask, reason: "ready" });
      } else {
        const statusMessage =
          latest.analysis_status === "cancelled"
            ? "原分析已经取消。确认后可以重新运行。"
            : latest.analysis_status === "failed"
              ? "原分析执行失败。确认后可以重新运行。"
              : "没有发现正在运行的任务。确认后可以重新运行。";
        setError(statusMessage);
        setRetryOffer({ task: retryTask, reason: "ready" });
      }
    } catch (err) {
      const error = err as Error & { status?: number };
      setRunning(false);
      setCurrentNodeTitle("");
      if (error.name === "AbortError") {
        // 用户主动取消或 stopAnalysis 中断，不覆盖已有 error。
        return;
      }
      if (error instanceof ApiError && error.status === 404) {
        // 服务端 session 已被清理（Render 重启 /tmp 清空、TTL 过期等）。
        // 明确告知用户并重置到上传界面，避免用户反复点"检查状态"得到 404。
        handleSessionLost(
          "会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。",
        );
        return;
      }
      // 网络错误（TypeError）或超时：立即退出轮询，不傻等 5 分钟。
      // 保留 retryOffer 让用户可以再次尝试检查状态。
      setError(
        `暂时无法确认任务状态：${error.message}。可稍后再次点击检查状态。`,
      );
    } finally {
      if (retryController.current === controller)
        retryController.current = null;
      setRetryChecking(false);
    }
  }

  // 断点续跑：从上次中断的步骤继续分析，跳过已完成的步骤。
  // 与 retryAnalysis（重新运行）的区别：
  //   - retryAnalysis 从头开始，所有步骤重新执行
  //   - resumeAnalysis 复用已有 plan，跳过 completed 中的步骤，从中断处继续
  function resumeAnalysis() {
    if (!retryOffer?.canResume) return;
    const {
      task: resumeTask,
      plan: savedPlan,
      completed: savedCompleted,
    } = retryOffer;
    setRetryOffer(null);
    setError("");
    startAnalysis(resumeTask, {
      plan: savedPlan || [],
      completed_steps: savedCompleted || [],
    });
  }

  // === Batch 4：计划审批回调 ===
  // 用户在 PlanPanel 中编辑计划后点击"批准并执行"触发，
  // 用编辑后的计划作为 resume_from，completed_steps 为空表示从头执行。
  const approvePlan = useCallback(
    (editedPlan: PlanStep[]) => {
      setAwaitingApproval(false);
      setPlan(editedPlan);
      setStepProgress(null);
      const objective = useAnalysisStore.getState().task.trim() || lastTaskRef.current;
      lastTaskRef.current = objective;
      startAnalysis(objective, {
        plan: editedPlan,
        completed_steps: [],
      });
    },
    [startAnalysis],
  );

  // 用户取消审批：丢弃编辑、清空待执行计划
  const cancelApproval = useCallback(() => {
    setAwaitingApproval(false);
    setPlan([]);
    setPendingObjective("");
    setCurrentNodeTitle("");
  }, []);

  // 从指定步骤重跑：截断 completed 至 index 之前的步骤，
  // 用原始 plan 作为 resume_from，跳过已保留部分。
  const rerunFromStep = useCallback(
    (index: number) => {
      if (!session || running) return;
      // 截断 completed_steps：保留 index 之前的步骤，丢弃 index 及之后的
      const truncatedCompleted = completed.slice(0, index);
      startAnalysis(lastTaskRef.current, {
        plan,
        completed_steps: truncatedCompleted,
      });
    },
    [session, running, completed, plan, startAnalysis],
  );

  const profile = (session?.profile || null) as
    | (DatasetProfile & {
        rows?: number;
        columns?: number;
        load_warnings?: string[];
      })
    | null;
  const columnInfo = profile?.column_info || [];
  const missingCount = columnInfo.reduce(
    (sum, item) => sum + ((item.missing as number) || 0),
    0,
  );
  const rows =
    (profile?.rows as number | undefined) ??
    (profile?.row_count as number | undefined) ??
    0;
  const columns =
    (profile?.columns as number | undefined) ??
    (profile?.column_count as number | undefined) ??
    0;
  const missingRate = profile
    ? ((missingCount / Math.max(rows * columns, 1)) * 100).toFixed(1)
    : "0.0";

  // 送审正文：优先用当前分析结果；会话恢复等场景回退到历史 last_result。
  // 若审计页里保存的正文已落后于最新报告，自动跟随最新报告，避免拿旧稿核验。
  const currentReportText = result?.response ?? session?.last_result?.response;
  const auditReportText =
    auditReport && auditReport === currentReportText ? auditReport : currentReportText;

  if (!authReady) {
    return (
      <main className="auth-gate">
        <div className="auth-loading">正在连接数据工作台…</div>
      </main>
    );
  }
  if (authRequired && !authenticated) {
    return (
      <AccessGate
        onAuthenticated={() => {
          setAuthenticated(true);
          window.location.reload();
        }}
      />
    );
  }

  return (
    <div className="app-shell">
      {/* 跳过导航：键盘 / 读屏用户可一步越过侧栏，直达主内容。
          放在 DOM 首位，获得焦点前视觉隐藏（见 a11y.css）。 */}
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        跳到主内容
      </a>
      <aside
        id="app-sidebar"
        className={`sidebar ${sidebarOpen ? "is-open" : ""}`}
        onTouchStart={onSidebarTouchStart}
        onTouchEnd={onSidebarTouchEnd}
      >
        <div className="wordmark">
          <strong>
            <GradientText speed={8}>数据台</GradientText>
          </strong>
          <span>DATA DESK</span>
        </div>

        {/* ReactBits ClickSpark：点击时白色火花迸发，强化"新建"这一主操作的确认感；
            spark 层 pointer-events:none 不影响点击，reduced-motion 下自动隐藏 */}
        <ClickSpark
          sparkColor="#ffffff"
          sparkCount={8}
          sparkLength={14}
          className="sidebar-spark"
        >
          <button
            type="button"
            className="new-analysis"
            onClick={() => fileInput.current?.click()}
            disabled={uploading}
          >
            <FilePlus2 size={17} />
            新建分析
          </button>
        </ClickSpark>

        <div className="sidebar-section">
          <span className="sidebar-label">当前数据</span>
          {/* ReactBits GlareHover：悬停流光扫过数据卡片/上传入口。
              borderColor 透明避免与按钮自身边框叠加；.sidebar-glare 撑满宽度并
              放开 overflow，防止按钮 hover 的 translateX/阴影被裁切 */}
          {session ? (
            <GlareHover
              className="sidebar-glare"
              borderRadius="var(--radius-md)"
              borderColor="transparent"
            >
              <button
                type="button"
                className="dataset-button"
                onClick={() => fileInput.current?.click()}
              >
                <FileSpreadsheet size={17} />
                <span>
                  <strong>{session.filename}</strong>
                  <small>
                    {rows.toLocaleString()} 行 · {columns} 列
                  </small>
                </span>
                <RefreshCw size={13} />
              </button>
            </GlareHover>
          ) : (
            <GlareHover
              className="sidebar-glare"
              borderRadius="var(--radius-md)"
              borderColor="transparent"
            >
              <button
                type="button"
                className="upload-button"
                onClick={() => fileInput.current?.click()}
                disabled={uploading}
              >
                {uploading ? (
                  <LoaderCircle className="spin" size={17} />
                ) : (
                  <Upload size={17} />
                )}
                {uploading
                  ? uploadProgress != null
                    ? `上传中 ${uploadProgress}%`
                    : "正在读取"
                  : "选择数据文件"}
              </button>
            </GlareHover>
          )}
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".csv,.tsv,.xlsx,.xls,.json,.jsonl,.parquet,.db,.sqlite,.sqlite3,.pdf,.txt,.docx"
            hidden
            onChange={(event) => {
              const files = event.target.files
                ? Array.from(event.target.files)
                : [];
              if (files.length === 0) return;
              uploadFile(files[0]);
              if (files.length > 1) {
                window.dispatchEvent(
                  new CustomEvent("empty-workspace:extra-files", {
                    detail: { files: files.slice(1) },
                  }),
                );
              }
            }}
          />
        </div>

        <HistoryPanel
          sessions={history}
          currentSessionId={session?.id}
          onSelect={selectSession}
          onRefresh={() => fetchHistory(true)}
          loading={historyLoading}
          expanded={historyExpanded}
          onToggle={() => setHistoryExpanded((value) => !value)}
          historyError={historyError}
          switchingSessionId={switchingSessionId}
          exportingSessionId={exportingSessionId}
          onExportSession={exportSession}
          onImportSession={importSession}
          onDeleteSession={deleteSession}
          onRenameSession={renameSession}
        />

        <div className="sidebar-spacer" />

        {/* 分析引擎区块：同一容器内的状态列表，与工作台细线分组保持一致 */}
        <div className="sidebar-status-group">
        <SettingsPanel />

        <div className="sidebar-foot" title={settings?.langsmith_project || undefined}>
          <span>
            <i aria-hidden="true" className={traceIndicator.dot} />
            LangSmith
          </span>
          <small>{traceIndicator.label}</small>
        </div>
        <div className="storage-status">
          <span>
            <i
              className={settings?.storage_status === "ok" ? "online" : ""}
            />
            对象存储
          </span>
          <small>
            {settings?.storage_status === "ok" ? "持久化正常" : "本地存储"}
          </small>
        </div>
        </div>
      </aside>
      {sidebarOpen && (
        <div
          className="sidebar-overlay"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      <main
        id="main-content"
        className={session ? "app-main" : "app-main is-empty"}
        tabIndex={-1}
      >
        {/* 全局读屏播报：载入示例、切换到核验视图等异步状态变化。
            role="status" 自带 aria-live="polite"，不打断当前朗读。 */}
        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {liveMessage}
        </div>
        <header className="topbar">
          <button
            ref={sidebarToggleRef}
            type="button"
            className="sidebar-toggle"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label={sidebarOpen ? "关闭侧边栏" : "打开侧边栏"}
            aria-expanded={sidebarOpen}
            aria-controls="app-sidebar"
          >
            <Menu size={18} aria-hidden="true" />
          </button>
          <div className="breadcrumb">
            <span>分析工作区</span>
            <ChevronRight size={13} />
            <strong>{session?.filename || "未命名分析"}</strong>
          </div>
          <div className="topbar-actions">
            <div className="api-status">
              <i className={settings ? "online" : ""} />
              {settings ? "服务正常" : "连接中"}
            </div>
            {/* 命令面板入口：点击等价于 Cmd+K，给不熟悉快捷键的用户一个可见入口。
                与主题按钮统一用 GlareHover 悬停流光，顶栏三个图标按钮交互一致 */}
            <GlareHover
              borderRadius="var(--radius-md)"
              borderColor="transparent"
            >
              <button
                type="button"
                className="icon-button topbar-action"
                title="命令面板 (⌘K)"
                aria-label="打开命令面板"
                onClick={() => setCommandOpen(true)}
              >
                <Command size={16} />
              </button>
            </GlareHover>
            {/* 快捷键帮助入口：与 ? 快捷键等价 */}
            <GlareHover
              borderRadius="var(--radius-md)"
              borderColor="transparent"
            >
              <button
                type="button"
                className="icon-button topbar-action"
                title="键盘快捷键 (?)"
                aria-label="查看键盘快捷键"
                onClick={() => setHelpOpen(true)}
              >
                <Keyboard size={16} />
              </button>
            </GlareHover>
            {/* 主题切换：太阳/月亮图标随当前 theme 切换，与 T 快捷键等价。
                外层用 ReactBits 的 GlareHover 做悬停流光（默认 accent #5b5bd6），
                原生 button 保留无障碍属性与点击；glare 在 icon 之下、仅作悬停反馈。 */}
            <GlareHover
              borderRadius="var(--radius-md)"
              borderColor="transparent"
              className="theme-toggle-glare"
            >
              <button
                type="button"
                className="icon-button topbar-action theme-toggle"
                title={theme === "dark" ? "切换到亮色 (T)" : "切换到暗色 (T)"}
                aria-label="切换主题"
                onClick={toggleTheme}
              >
                {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
              </button>
            </GlareHover>
          </div>
        </header>

        {error && (
          <div className="error-banner" role="alert">
            <AlertTriangle size={16} />
            <span
              className={
                error.length > 120 ? (errorExpanded ? "" : "is-clamped") : ""
              }
            >
              {error}
            </span>
            {error.length > 120 && (
              <button
                type="button"
                className="error-expand-toggle"
                onClick={() => setErrorExpanded((v) => !v)}
                aria-expanded={errorExpanded}
              >
                {errorExpanded ? "收起" : "查看详情"}
              </button>
            )}
            {retryOffer && !running && retryOffer.canResume && (
              <button
                type="button"
                className="resume-button"
                onClick={resumeAnalysis}
                title={`从已完成的 ${retryOffer.completed?.length || 0} 个步骤继续，跳过已完成部分`}
              >
                <Play size={13} fill="currentColor" />
                继续分析
              </button>
            )}
            {retryOffer && !running && (
              <button
                type="button"
                className="retry-button"
                onClick={() => retryAnalysis()}
                disabled={retryChecking}
                aria-busy={retryChecking}
              >
                <RefreshCw size={13} className={retryChecking ? "spin" : ""} />
                {retryChecking
                  ? "检查中…"
                  : retryOffer.reason === "ready"
                    ? "重新运行"
                    : "检查状态"}
              </button>
            )}
            <button
              type="button"
              title="关闭"
              aria-label="关闭错误提示"
              className="error-close"
              onClick={() => setError("")}
            >
              <X size={15} />
            </button>
          </div>
        )}

        {/* retryOffer 独立恢复入口：用户关掉错误横幅后仍能重试，
            避免之前 setError("") 同时清空 retryOffer 导致彻底失去恢复入口。
            canResume 时优先展示"继续分析"（绿色主操作），"重新运行"降为次操作。 */}
        {!error && retryOffer && !running && (
          <div className="retry-bar" role="status">
            <RefreshCw size={13} />
            <span>
              {retryOffer.canResume
                ? `上次分析完成了 ${retryOffer.completed?.length || 0} 个步骤后中断，可以继续或重新运行`
                : "上次分析未完成，可以重新运行"}
            </span>
            {retryOffer.canResume && (
              <button
                type="button"
                className="resume-button"
                onClick={resumeAnalysis}
                title="从已完成的步骤继续，跳过已完成部分"
              >
                <Play size={13} fill="currentColor" />
                继续分析
              </button>
            )}
            <button
              type="button"
              className="retry-button"
              onClick={() => retryAnalysis()}
              disabled={retryChecking}
              aria-busy={retryChecking}
            >
              <RefreshCw size={13} className={retryChecking ? "spin" : ""} />
              {retryChecking
                ? "检查中…"
                : retryOffer.reason === "ready"
                  ? "重新运行"
                  : "检查状态"}
            </button>
            <button
              type="button"
              title="放弃恢复"
              aria-label="放弃恢复"
              className="error-close"
              onClick={() => setRetryOffer(null)}
            >
              <X size={13} />
            </button>
          </div>
        )}

        {!session ? (
          <EmptyWorkspace
            uploading={uploading}
            uploadProgress={uploadProgress}
            onUpload={() => fileInput.current?.click()}
            onFileDrop={uploadFile}
            onCancelUpload={cancelUpload}
          />
        ) : (
          <>
            <section className="dataset-header">
              <div>
                <span className="section-kicker">当前数据集</span>
                <h1>{session.filename}</h1>
                {profile?.load_warnings && profile.load_warnings.length > 0 && (
                  <p className="dataset-warning">
                    <AlertTriangle size={13} />
                    {profile.load_warnings[0]}
                  </p>
                )}
              </div>
              <button
                type="button"
                className="change-file"
                onClick={() => fileInput.current?.click()}
              >
                <RefreshCw size={14} />
                替换数据
              </button>
            </section>

            <section className="metrics-band">
              {/* 可点击指标：记录数 / 字段数 → 跳转数据 Tab；
                  分析产物 → 跳转产物 Tab。缺失率保持静态展示。 */}
              <button
                type="button"
                className="metric metric-clickable"
                onClick={() => setActiveTab("data")}
                title="查看数据预览"
              >
                <span>记录</span>
                <strong>
                  <CountUp end={rows} duration={1100} />
                  <small>行</small>
                </strong>
              </button>
              <button
                type="button"
                className="metric metric-clickable"
                onClick={() => setActiveTab("data")}
                title="查看数据预览"
              >
                <span>字段</span>
                <strong>
                  <CountUp end={columns} duration={900} />
                  <small>列</small>
                </strong>
              </button>
              <Metric label="缺失率" value={missingRate} unit="%" />
              <button
                type="button"
                className="metric metric-clickable"
                onClick={() => setActiveTab("artifacts")}
                title="查看分析产物"
              >
                <span>分析产物</span>
                <strong>
                  <CountUp
                    end={session.artifacts?.length || 0}
                    duration={900}
                  />
                  <small>项</small>
                </strong>
              </button>
            </section>

            {activeTab !== "audit" && <>
              <TaskBox
                task={task}
                running={running}
                stopping={stopping}
                awaitingApproval={awaitingApproval}
                configured={!!settings?.configured}
                hasSession={!!session}
                currentNodeTitle={currentNodeTitle}
                elapsedSeconds={elapsedSeconds}
                onTaskChange={setTask}
                onRun={() => startAnalysis()}
                onRunPlanReview={() => startAnalysis(task, null, true)}
                onStop={stopAnalysis}
                onFocusPlan={focusPlanPanel}
                taskInputRef={taskInput}
              />
              <MetricsEditor sessionId={session.id} />
              {!settings?.configured && (
                <p className="composer-note">请先在左侧配置 DeepSeek API Key。</p>
              )}
            </>}

            <WorkspaceTabs
              activeTab={activeTab}
              artifactCount={session.artifacts?.length || 0}
              onSelectTab={setActiveTab}
            />

            <section
              ref={auditPanelRef}
              id="tabpanel-audit"
              role="tabpanel"
              aria-labelledby="tab-audit"
              tabIndex={0}
              hidden={activeTab !== "audit"}
            >
              <SalesAudit
                key={session.id}
                sessionId={session.id}
                filename={session.filename}
                initialDemo={auditDemoSession.current === session.id}
                report={running && session.id === runningSessionIdRef.current ? undefined : auditReportText}
                onPrepareDraft={settings?.configured && !running && !awaitingApproval ? () => {
                  setTask(SALES_BRIEF_TASK);
                  setActiveTab("analysis");
                } : undefined}
                onReloadSession={(id) => void reloadAuditSession(id)}
                onExitSession={() => handleSessionLost("已返回工作台，请重新上传数据。")}
              />
            </section>

            {activeTab === "analysis" && (
              <div
                className="analysis-grid tab-content-enter"
                key="tab-analysis"
                id="tabpanel-analysis"
                role="tabpanel"
                aria-labelledby="tab-analysis"
                tabIndex={0}
              >
                {session?.demo?.insights && (
                  <DemoBrief
                    key={session.id}
                    sessionId={session.id}
                    insights={session.demo.insights}
                    rows={session.preview}
                    principals={session.demo.principals}
                  />
                )}
                <section className="analysis-column">
                  {result ? (
                    <>
                      <Suspense fallback={<LazyFallback />}>
                        <ReportView
                          result={result}
                          streaming={running && !!result}
                          artifacts={result.artifacts || session?.artifacts}
                          onPreview={openArtifactPreview}
                          reasoning={reasoning}
                          reasoningStreaming={reasoningStreaming && running}
                          theme={theme}
                          usage={usage}
                          sessionId={session?.id}
                          onReplay={replayLastAnalysis}
                          replayBusy={replayBusy}
                          onAuditReport={sendReportToAudit}
                        />
                      </Suspense>
                      <ConversationThread
                        messages={followUps}
                        input={followUpInput}
                        onInputChange={setFollowUpInput}
                        onSubmit={startFollowUp}
                        onStop={stopFollowUp}
                        running={chatRunning}
                        disabled={running || !settings?.configured}
                        onPreview={openArtifactPreview}
                        artifacts={session?.artifacts}
                        onEditMessage={handleEditFollowUp}
                        theme={theme}
                        inputRef={followUpInputRef}
                      />
                    </>
                  ) : (
                    <Reveal>
                      <DatasetOverview profile={profile} />
                    </Reveal>
                  )}
                </section>
                <PlanPanel
                  plan={plan}
                  planSource={planSource}
                  replanReason={replanReason}
                  completed={completed}
                  running={
                    running && session?.id === runningSessionIdRef.current
                  }
                  currentNodeTitle={currentNodeTitle}
                  elapsedSeconds={
                    session?.id === runningSessionIdRef.current
                      ? elapsedSeconds
                      : (session?.elapsed_seconds ?? null)
                  }
                  toolTrace={
                    session?.id === runningSessionIdRef.current ? toolTrace : []
                  }
                  awaitingApproval={awaitingApproval}
                  pendingObjective={pendingObjective}
                  stepProgress={stepProgress}
                  onApprovePlan={approvePlan}
                  onCancelApproval={cancelApproval}
                  onRerunFromStep={rerunFromStep}
                />
              </div>
            )}

            {activeTab === "data" && (
              <section
                className="data-view tab-content-enter"
                key="tab-data"
                id="tabpanel-data"
                role="tabpanel"
                aria-labelledby="tab-data"
                tabIndex={0}
              >
                <div className="section-title">
                  <div>
                    <span className="section-kicker">数据预览</span>
                    <h2>原始记录</h2>
                  </div>
                  <small>前 100 行</small>
                </div>
                <Suspense fallback={<LazyFallback />}>
                  <DataTable rows={session.preview} />
                </Suspense>
              </section>
            )}

            {activeTab === "artifacts" && (
              <section
                className="artifact-view tab-content-enter"
                key="tab-artifacts"
                id="tabpanel-artifacts"
                role="tabpanel"
                aria-labelledby="tab-artifacts"
                tabIndex={0}
              >
                <div className="section-title artifact-title">
                  <div>
                    <span className="section-kicker">结果中心</span>
                    <h2>值得保留的结论</h2>
                  </div>
                  <small>中间文件已自动收起</small>
                </div>
                <ArtifactCenter
                  artifacts={session.artifacts}
                  onDownload={downloadArtifact}
                  onPreview={openArtifactPreview}
                  onBatchDownload={batchDownload}
                  candidateDatasets={session.candidate_datasets}
                  onAdoptDataset={(name) => void adoptCandidateDataset(name)}
                  onDiscardDataset={(name) => void discardCandidateDataset(name)}
                  adoptBusy={adoptBusy}
                  onRescan={() => void rescanArtifacts()}
                  rescanBusy={rescanBusy}
                />
              </section>
            )}

            {activeTab === "comments" && (
              <section
                className="tab-content-enter"
                key="tab-comments"
                id="tabpanel-comments"
                role="tabpanel"
                aria-labelledby="tab-comments"
                tabIndex={0}
              >
                <CommentsPanel key={session.id} sessionId={session.id} />
              </section>
            )}
          </>
        )}
      </main>
      {/* 产物预览模态：图表预览/对比/全屏/PNG/内联编辑，已提取至 PreviewModal；
          previewItem 为空时组件内部直接返回 null */}
      <PreviewModal
        preview={artifactPreview}
        theme={theme}
        setTheme={setTheme}
        onDownload={downloadArtifact}
      />
      {commandOpen && (
        <Suspense fallback={<LazyFallback />}>
          <CommandPalette
            query={commandQuery}
            onQueryChange={setCommandQuery}
            actions={COMMAND_ACTIONS}
            sessions={history}
            onAction={runCommandAction}
            onSelectSession={(item) => {
              setCommandOpen(false);
              setCommandQuery("");
              selectSession(item);
            }}
            onClose={() => {
              setCommandOpen(false);
              setCommandQuery("");
            }}
            theme={theme}
          />
        </Suspense>
      )}
      {helpOpen && (
        <Suspense fallback={<LazyFallback />}>
          <HelpPanel onClose={() => setHelpOpen(false)} />
        </Suspense>
      )}
    </div>
  );
}

export default App;
