import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  ChevronRight,
  Command,
  FilePlus2,
  FileSpreadsheet,
  Keyboard,
  ListChecks,
  LoaderCircle,
  Menu,
  Moon,
  Play,
  RefreshCw,
  Square,
  Sun,
  Table2,
  Upload,
  X,
} from "lucide-react";
import { AccessGate, Metric } from "./components/AccessGate";
import EmptyWorkspace from "./components/EmptyWorkspace";
import DatasetOverview from "./components/DatasetOverview";
import ConversationThread from "./components/ConversationThread";
import PlanPanel from "./components/PlanPanel";
import HistoryPanel from "./components/HistoryPanel";
import ArtifactCenter from "./components/ArtifactCenter";
import SettingsPanel from "./components/SettingsPanel";
import PreviewModal from "./components/PreviewModal";
// 代码分割/懒加载（#16）：重型组件延迟加载，减小首次 bundle 体积。
// - CommandPalette / HelpPanel：弹层，仅在用户触发（Cmd+K / ?）时显示
// - ReportView：含 ReactMarkdown，仅在分析完成后渲染（result 非空）
// - DataTable：含搜索/排序逻辑，仅在切到"数据" Tab 时渲染
const DataTable = React.lazy(() => import("./components/DataTable"));
const ReportView = React.lazy(() => import("./components/ReportView"));
const CommandPalette = React.lazy(() => import("./components/CommandPalette"));
const HelpPanel = React.lazy(() => import("./components/HelpPanel"));
import useAnalysisRunner from "./hooks/useAnalysisRunner";
import useArtifactPreview from "./hooks/useArtifactPreview";
import useAuthBootstrap from "./hooks/useAuthBootstrap";
import useChatRunner from "./hooks/useChatRunner";
import useDownloads from "./hooks/useDownloads";
import useScrollProgress from "./hooks/useScrollProgress";
import useSettingsPanel from "./hooks/useSettingsPanel";
import useShortcuts from "./hooks/useShortcuts";
import useTabPersistence from "./hooks/useTabPersistence";
import useTheme from "./hooks/useTheme";
import useTimer from "./hooks/useTimer";
import { useAppStore } from "./store/useAppStore";
import { api, ApiError, describeApiError, requestHeaders } from "./utils/api";
import { formatDuration, wait } from "./utils/format";
import {
  API_URL,
  ACTIVE_ANALYSIS_STATES,
  COMMAND_ACTIONS,
  MAX_UPLOAD_BYTES_CLIENT,
  presets,
} from "./constants";
import type {
  CommandAction,
  DatasetProfile,
  FollowUpMessage,
  HistorySessionItem,
  PlanStep,
  RetryOffer,
  Session,
} from "./types";

// /api/auth 返回的轻量结构
interface AuthStatus {
  required?: boolean;
  authenticated?: boolean;
}

// /api/sessions GET 列表响应
interface SessionListResponse {
  sessions?: HistorySessionItem[];
}

function App() {
  // 所有 UI 状态从 Zustand store 获取（见 src/store/useAppStore.js）。
  // store 按功能分组为 slices：认证 / 配置 / 会话 / 分析任务 / UI / 错误 /
  // Key / Tool trace / 多轮对话 / 重试 / Busy / 历史 / 计时 / Reasoning / 命令面板。
  // setError 保留原 useCallback 包装逻辑：msg 非空时重置 errorExpanded=false。
  // setter 既接受直接值也接受 functional updater，与原 useState 行为一致。
  const {
    // 认证
    authRequired, authenticated, authReady,
    setAuthRequired, setAuthenticated, setAuthReady,
    // 配置（apiKey/effort/thinking 的编辑逻辑已移至 SettingsPanel；
    // setSettings/setEffort/setThinking 仍供 useAuthBootstrap 初始化，
    // setApiKey 供 useShortcuts 的 Esc 清空逻辑使用）
    settings,
    setSettings, setApiKey, setEffort, setThinking,
    // 会话
    session, activeTab,
    setSession, setActiveTab,
    lastActiveTab, setLastActiveTab,
    // 分析任务
    task, plan, completed, result, running,
    setTask, setPlan, setCompleted, setResult, setRunning,
    // 计划审批：plan_only 流程的待审阅状态、步骤进度
    awaitingApproval, pendingObjective, stepProgress,
    setAwaitingApproval, setPendingObjective, setStepProgress,
    // UI（previewHtml/previewLoading/previewError 及其 setter 已交由
    // useArtifactPreview / PreviewModal 消费，App 不再直接读写）
    uploading, previewItem, currentNodeTitle,
    setUploading, setCurrentNodeTitle,
    // 错误
    error, errorExpanded,
    setError, setErrorExpanded,
    // Key UI（showKey 已移至 SettingsPanel；keyOpen/setKeyOpen 供
    // useSettingsPanel / useShortcuts / 命令面板动作使用）
    keyOpen,
    setKeyOpen,
    // 工具调用时间线（toolTrace）：ReAct 执行器内部每次工具调用实时推送，
    // 让用户看到"正在读取数据→正在清洗→正在生成图表"的过程
    toolTrace, setToolTrace,
    // 多轮对话：followUps 存储报告之后的追问消息对（user+assistant），
    // 让用户基于已有分析结果继续提问，不必每次都触发完整 plan→execute→finalize。
    // 结构：[{role, content, streaming?, tools?, error?}]
    followUps, followUpInput, chatRunning,
    setFollowUps, setFollowUpInput, setChatRunning,
    // 重试
    retryOffer, retryChecking,
    setRetryOffer, setRetryChecking,
    // Busy：停止分析的 busy 状态（保存设置的 busy 已移至 SettingsPanel）
    stopping,
    setStopping,
    // 历史（historyError 区分"没数据"和"加载失败"，避免用户误以为数据丢失）
    history, historyLoading, historyError, historyExpanded, switchingSessionId,
    setHistory, setHistoryLoading, setHistoryError, setHistoryExpanded, setSwitchingSessionId,
    // 计时：running 时由 setInterval 每秒刷新；非 running 时由
    // session.elapsed_seconds / completed - started 计算一次性赋值
    elapsedSeconds, setElapsedSeconds,
    // Reasoning（DeepSeek reasoning_content）：分析/追问时后端推送 thinking_chunk，
    // 累积到这里让 ReportView / ConversationBubble 展示思考过程。
    // 主分析的 reasoning 放在 App 级别（单条），追问的 reasoning 内嵌在每条 assistant 气泡上。
    // Token 用量（usage）：complete / chat_done 事件携带，展示在报告/气泡底部。
    reasoning, reasoningStreaming, usage,
    setReasoning, setReasoningStreaming, setUsage,
    // 命令面板（Cmd+K）与快捷键帮助（?）弹层状态
    commandOpen, commandQuery, helpOpen,
    setCommandOpen, setCommandQuery, setHelpOpen,
  } = useAppStore();

  // refs 保持局部状态：useRef 不迁移到 store（控制器、缓存、闭包内
  // 读取的最新值不需要触发重渲染）。
  // 分析 / 追问 / 预览相关 ref 已随逻辑提取至 useAnalysisRunner /
  // useChatRunner / useArtifactPreview，这里仅保留 App.tsx 仍直接使用的 ref。
  const fileInput = useRef<HTMLInputElement>(null);
  const taskInput = useRef<HTMLTextAreaElement>(null);
  const retryController = useRef<AbortController | null>(null);
  // 按 sessionId 保存草稿：切换历史会话时不丢失当前正在输入的任务
  const taskDraftsRef = useRef<Record<string, string>>({});

  // 主题（light/dark）：useTheme 内部读取 localStorage，无则跟随系统。
  const { theme, toggle: toggleTheme } = useTheme();

  // === 分析 / 追问 / 产物预览逻辑提取至独立 hook ===
  // useAnalysisRunner：startAnalysis / stopAnalysis 及 startedAtRef /
  // runningSessionIdRef / lastTaskRef / analysisController 等共享 ref。
  // handleSessionLost 为 function declaration（hoisted），此处即可引用；
  // 其内部读取的 analysisController / chatControllerRef / retryController
  // 在它被实际调用时均已赋值，无 TDZ 风险。
  const {
    startAnalysis, stopAnalysis,
    analysisController, startedAtRef, runningSessionIdRef, lastTaskRef,
  } = useAnalysisRunner({ handleSessionLost, retryController, autoRecover: retryAnalysis });
  // useChatRunner：startFollowUp / stopFollowUp 及 chatControllerRef /
  // followUpInputRef（供 handleSessionLost / deleteSession / ConversationThread 共享）。
  const { startFollowUp, stopFollowUp, chatControllerRef, followUpInputRef } = useChatRunner();
  // useArtifactPreview：图表预览模态、对比/全屏/PNG 导出、图表内联编辑。
  // 完整返回值整体传给 PreviewModal，App 仅直接使用开/关两个回调。
  const artifactPreview = useArtifactPreview();
  const { openArtifactPreview, closeArtifactPreview } = artifactPreview;
  // useDownloads：单产物下载 / 批量下载 / 会话导出 ZIP。
  const { downloadArtifact, batchDownload, exportSession } = useDownloads();

  // 移动端侧边栏抽屉开关：桌面端 sidebar 常驻，平板/手机折叠为抽屉
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // 设置面板点击外部关闭（提取至 useSettingsPanel）。
  useSettingsPanel(keyOpen, setKeyOpen);

  // Scroll 进度指示器：顶栏底部品牌色进度条。
  useScrollProgress();

  // Tab 持久化：activeTab 变化时同步到 lastActiveTab（提取至 useTabPersistence）。
  useTabPersistence(activeTab, setLastActiveTab);

  // 拉取历史会话列表。鉴权通过后立即拉一次，让用户在初次进入时就能
  // 看到之前的会话；上传/切换/分析完成时也会调用，保持列表新鲜。
  // manual=true 表示用户主动触发（点刷新按钮），才设置 historyLoading
  // 让刷新按钮转圈；轮询调用 manual=false，不触发按钮 disabled，避免
  // 30 秒一次的轮询让整个历史列表短暂瘫痪（用户正要点击时被禁用）。
  const fetchHistory = async (manual = false) => {
    if (manual) setHistoryLoading(true);
    try {
      const payload = await api<SessionListResponse>("/api/sessions?limit=30");
      setHistory(payload.sessions || []);
      setHistoryError(false);
    } catch {
      // 加载失败不阻塞主流程，但记录错误状态，让用户能区分"没数据"
      // 和"加载失败"，并提供重试入口（之前是完全静默，用户有几十个
      // 会话却看到"还没有历史会话"，会以为数据丢了）。
      setHistoryError(true);
    } finally {
      if (manual) setHistoryLoading(false);
    }
  };

  // 认证引导 + 鉴权就绪后拉取历史（提取至 useAuthBootstrap）。
  useAuthBootstrap({
    setAuthRequired, setAuthenticated, setAuthReady,
    setSettings, setEffort, setThinking, setKeyOpen, setError,
    fetchHistory, authReady, authRequired, authenticated,
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
      // 切换会话时清理审批/进度态，避免新会话复用旧审批 UI
      setAwaitingApproval(false);
      setPendingObjective("");
      setStepProgress(null);
      restoreCompletedAnalysis(latest);
      restoreFollowUps(latest);
      if (!running) {
        startedAtRef.current = (latest as Session & { analysis_started_at?: number | null }).analysis_started_at ?? null;
        setElapsedSeconds(latest.elapsed_seconds ?? null);
      }
      // 恢复上次查看的 Tab：有 preview 数据时恢复 lastActiveTab（如数据/产物），
      // 无数据时回退 analysis，避免切到空白的数据 Tab。
      setActiveTab(latest.preview?.length ? lastActiveTab : "analysis");
      fetchHistory();
    } catch (err) {
      const error = err as Error & { status?: number };
      if (error instanceof ApiError && error.status === 404) {
        handleSessionLost("该历史会话已被服务端清理，请选择其他会话或重新上传数据。");
      } else {
        setError(`无法打开历史会话：${error.message}`);
      }
    } finally {
      setSwitchingSessionId(null);
    }
  };

  // 实时耗时：running 时持续刷新 elapsed = now - startedAtRef（提取至 useTimer）。
  useTimer(running, startedAtRef, setElapsedSeconds);

  // 分析结束（running 转 false）时刷新历史，把当前会话的最新状态
  // 同步到侧边栏（产物数、状态、相对时间）。
  useEffect(() => {
    if (!running && session) fetchHistory();
  }, [running]);

  // 历史会话列表自动轮询：默认 30 秒刷新一次（保持相对时间新鲜），
  // 当本会话或其他会话正在 running 时缩短到 5 秒——让"运行中"圆点
  // 能及时变成"已完成"。后台 tab 时暂停轮询节省请求。
  // 不在 running 时也轮询是为了：用户在另一个 tab 启动分析，回到本 tab
  // 时列表能反映最新状态；相对时间"3 分钟前"也需要定期刷新才准确。
  useEffect(() => {
    if (!authReady || (authRequired && !authenticated)) return undefined;
    const interval = running ? 5000 : 30000;
    const poll = () => {
      if (document.hidden) return;
      fetchHistory();
    };
    const timer = window.setInterval(poll, interval);
    // 回到前台时立即刷新一次，避免等待下一个 interval tick
    const onVisible = () => { if (!document.hidden) fetchHistory(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [authReady, authRequired, authenticated, running]);

  // 命令面板动作执行器：根据 action.id 路由到具体操作。
  // 用 useCallback 保持身份稳定，作为 props 传入 CommandPalette 时不会触发重渲染。
  const runCommandAction = useCallback((action: CommandAction) => {
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
      case "show-help":
        setHelpOpen(true);
        break;
      default:
        break;
    }
    setCommandOpen(false);
    setCommandQuery("");
  }, [toggleTheme]);

  // 消息编辑重发：截断 index 之后的所有 followUps，把新文本作为新追问重发。
  // 参考 ChatGPT/Claude 的"编辑并重新发送"交互——保留历史上下文的同时重置分支。
  const handleEditFollowUp = useCallback((index: number, newContent: string) => {
    setFollowUps((prev) => prev.slice(0, index));
    setFollowUpInput(newContent);
    // 让输入框立即获得焦点，用户可直接 Cmd+Enter 发送
    window.setTimeout(() => followUpInputRef.current?.focus(), 0);
  }, []);

  // useCallback：downloadArtifact 作为 props 传给 React.memo(ArtifactCenter)。
  // 若每次渲染都创建新函数，memo 比较失败，ArtifactCenter 仍然每次重渲染。
  // useCallback 让函数身份稳定，memo 才能真正跳过无关重渲染。
  // openArtifactPreview / closeArtifactPreview 已提取至 useArtifactPreview。
  // 键盘快捷键：Esc 关闭预览/设置 + 全局 Cmd+K/Cmd+B/Cmd+./?/T/1-3（提取至 useShortcuts）。
  // closeArtifactPreview / stopAnalysis / stopFollowUp 由 useArtifactPreview /
  // useAnalysisRunner / useChatRunner 返回（hook 在上方已调用），此处可直接引用。
  useShortcuts({
    previewItem, keyOpen, closeArtifactPreview, setKeyOpen, setApiKey,
    running, chatRunning, stopAnalysis, stopFollowUp, toggleTheme,
    setCommandOpen, setHistoryExpanded, setHelpOpen, setActiveTab,
  });

  // loadCompareChart / downloadPng 已提取至 useArtifactPreview；
  // downloadArtifact / batchDownload / exportSession 已提取至 useDownloads。

  // 导入会话：multipart 上传 ZIP，后端返回完整 session payload；
  // 成功后刷新历史并切到新会话。FormData 不能带 Content-Type，
  // 浏览器会自动设置 multipart/form-data 边界。
  const importSession = useCallback(async (file: File) => {
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetch(`${API_URL}/api/sessions/import`, {
        method: "POST",
        headers: requestHeaders(),
        body: formData,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string };
        throw new Error(payload.detail || "导入失败");
      }
      const sessionPayload = await response.json() as Session;
      await fetchHistory();
      selectSession({ id: sessionPayload.id, filename: sessionPayload.filename, analysis_status: sessionPayload.analysis_status } as HistorySessionItem);
    } catch (err) {
      setError(`导入会话失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setUploading(false);
    }
  }, [fetchHistory, selectSession]);

  // 删除会话：调用 DELETE 端点清理服务端数据，成功后刷新历史列表。
  // 若删除的是当前正在查看的会话，清空前端状态回到空状态，让用户
  // 重新上传数据开始新分析，避免停留在已失效的会话视图上。
  const deleteSession = useCallback(async (item: HistorySessionItem) => {
    try {
      const response = await fetch(`${API_URL}/api/sessions/${item.id}`, {
        method: "DELETE",
        headers: requestHeaders(),
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const payload: unknown = contentType.includes("application/json")
          ? await response.json().catch(() => ({}))
          : await response.text().catch(() => "");
        throw new Error(describeApiError(payload, response.status));
      }
      // 删除的是当前会话：清空状态回到空工作台
      if (session?.id === item.id) {
        setSession(null);
        setResult(null);
        setPlan([]);
        setCompleted([]);
        setCurrentNodeTitle("");
        setRetryOffer(null);
        setFollowUps([]);
        setAwaitingApproval(false);
        setPendingObjective("");
        setStepProgress(null);
        setTask("");
        retryController.current?.abort();
        analysisController.current?.abort();
        chatControllerRef.current?.abort();
      }
      fetchHistory();
    } catch (err) {
      setError(`删除会话失败：${err instanceof Error ? err.message : "未知错误"}`);
    }
  }, [session?.id, fetchHistory]);

  // 重命名会话：PATCH 更新服务端 title，乐观更新本地 history 列表与当前 session。
  // 空串视为清除自定义标题（回退 filename），后端会存 None。
  const renameSession = useCallback(async (item: HistorySessionItem, title: string) => {
    try {
      const response = await fetch(`${API_URL}/api/sessions/${item.id}`, {
        method: "PATCH",
        headers: { ...requestHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string };
        throw new Error(payload.detail || "重命名失败");
      }
      const data = await response.json() as { title: string };
      // 乐观更新 history 列表中该 item 的 title
      const updated = (history || []).map((s) => s.id === item.id ? { ...s, title: data.title || undefined } : s);
      setHistory(updated);
      // 当前会话同步更新标题
      if (session?.id === item.id) {
        setSession((prev) => prev ? { ...prev, title: data.title || undefined } : prev);
      }
    } catch (err) {
      setError(`重命名会话失败：${err instanceof Error ? err.message : "未知错误"}`);
    }
  }, [session?.id, history, setHistory]);

  // editChart 及"打开预览时初始化编辑表单"的 useEffect 已提取至 useArtifactPreview。
  // 连接测试 testConnection / 保存设置 saveSettings 已随设置面板迁至 SettingsPanel。

  async function uploadFile(file: File | undefined | null) {
    if (!file) return;
    // 客户端文件大小校验：服务端 max_upload_bytes 兜底，但提前检查可以
    // 避免上传 100MB+ 才得到 422，浪费用户带宽和等待时间。Render 免费版
    // 100MB 限制与 max_upload_bytes 默认值对齐。
    if (file.size > MAX_UPLOAD_BYTES_CLIENT) {
      const mb = Math.round(MAX_UPLOAD_BYTES_CLIENT / (1024 * 1024));
      setError(`文件 ${file.name} 超过 ${mb}MB 上传上限，请拆分或精简后再上传。`);
      if (fileInput.current) fileInput.current.value = "";
      return;
    }
    setUploading(true);
    setError("");
    // 上传新数据集时清理上一个会话的残留 UI 状态，避免预览/重试/进度泄漏到新会话。
    closeArtifactPreview();
    setPlan([]);
    setCompleted([]);
    setResult(null);
    setTask("");
    setActiveTab("analysis");
    setCurrentNodeTitle("");
    setRetryOffer(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const value = await api<Session>("/api/sessions", { method: "POST", body: form });
      setSession(value);
      startedAtRef.current = (value as Session & { analysis_started_at?: number | null }).analysis_started_at ?? null;
      setElapsedSeconds(value.elapsed_seconds ?? null);
      setFollowUps([]);
      fetchHistory();
    } catch (err) {
      const error = err as Error;
      setError(error.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  // EmptyWorkspace 派发的两个自定义事件：
  //  - empty-workspace:load-sample —— 用户点击"加载示例数据体验"，后端用内置样例建会话
  //  - empty-workspace:extra-files  —— 多文件拖入时，首个文件已走主上传路径，
  //    其余文件在此顺序创建独立会话（数据分析常需对比多表）
  useEffect(() => {
    const onLoadSample = async () => {
      setUploading(true);
      setError("");
      closeArtifactPreview();
      setPlan([]); setCompleted([]); setResult(null); setTask("");
      setActiveTab("analysis"); setCurrentNodeTitle(""); setRetryOffer(null);
      try {
        const value = await api<Session>("/api/sessions/sample", { method: "POST" });
        setSession(value);
        startedAtRef.current = (value as Session & { analysis_started_at?: number | null }).analysis_started_at ?? null;
        setElapsedSeconds(value.elapsed_seconds ?? null);
        setFollowUps([]);
        fetchHistory();
      } catch (err) {
        setError((err as Error).message);
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
    window.addEventListener("empty-workspace:load-sample", onLoadSample as EventListener);
    window.addEventListener("empty-workspace:extra-files", onExtraFiles as EventListener);
    return () => {
      window.removeEventListener("empty-workspace:load-sample", onLoadSample as EventListener);
      window.removeEventListener("empty-workspace:extra-files", onExtraFiles as EventListener);
    };
  }, []);

  function restoreCompletedAnalysis(latest: Session): boolean {
    const savedResult = latest.last_result;
    if (savedResult) {
      // 前端 UI 不消费 trace 字段，恢复时丢弃以减小内存占用；
      // 后端持久化时 trace 也已截断到最近 20 条，这里不再透传。
      setResult({
        response: savedResult.response,
        artifacts: savedResult.artifacts || latest.artifacts || [],
        dataset_profile: savedResult.dataset_profile || latest.profile,
        plan: savedResult.plan || [],
        completed_steps: savedResult.completed_steps || [],
      });
      setPlan(savedResult.plan || []);
      setCompleted(savedResult.completed_steps || []);
      return true;
    }
    const assistantMessage = [...(latest.chat || [])]
      .reverse()
      .find((item) => item.role === "assistant" && !!item.content);
    if (!assistantMessage) return false;
    setResult({
      response: assistantMessage.content || "",
      trace: [],
      artifacts: latest.artifacts || [],
      dataset_profile: latest.profile,
      plan: [],
      completed_steps: [],
    });
    setPlan([]);
    setCompleted([]);
    return true;
  }

  // 从 session.chat 恢复追问历史。chat 数组结构为
  // [user(分析任务), assistant(分析报告), user(追问1), assistant(追问1回答), ...]，
  // 跳过前两条（首轮分析对），后续的都是追问。
  function restoreFollowUps(latest: Session) {
    const chat = latest?.chat || [];
    const tail = chat.length > 2 ? chat.slice(2) : [];
    // 恢复完整字段：除 role/content 外，还保留 tools（工具调用 chip）、
    // reasoning（思考过程）、usage（token 用量），让历史会话的追问回复
    // 仍能展示这些信息，而非降级为纯文本。
    setFollowUps(tail.map((item) => ({
      role: item.role,
      content: item.content || "",
      tools: item.tools,
      reasoning: item.reasoning,
      usage: item.usage,
    } as FollowUpMessage)));
  }

  // 会话失效（404）时清空前端状态，引导用户回到上传界面。
  // Render 免费实例重启会清空 /tmp，session 数据不可恢复，与其让用户
  // 反复点"检查状态"得到 404，不如明确告知并重置。
  function handleSessionLost(message?: string) {
    setSession(null);
    setResult(null);
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
      let latest = await api<Session & { analysis_started_at?: number | null }>(`/api/sessions/${session.id}`, { timeoutMs: 8000, signal: controller.signal });
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
        while (ACTIVE_ANALYSIS_STATES.has(latest.analysis_status) && Date.now() < deadline) {
          await wait(3000);
          if (controller.signal.aborted) break;
          latest = await api<Session & { analysis_started_at?: number | null }>(`/api/sessions/${session.id}`, { timeoutMs: 8000, signal: controller.signal });
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
      if (latest.analysis_status === "completed" && restoreCompletedAnalysis(latest)) {
        setError("");
        setRetryOffer(null);
      } else if (ACTIVE_ANALYSIS_STATES.has(latest.analysis_status)) {
        // 5 分钟 deadline 到了但后端仍在 running——可能是 Render free 实例被
        // 暂停、worker 死亡或 LLM 调用超长。提供"强制重新运行"让用户能
        // 中断旧任务重跑，而不是无限等待。
        setError("原分析长时间未结束（可能服务被暂停或任务卡住）。可以选择强制重新运行，将提交新的分析任务。");
        setRetryOffer({ task: retryTask, reason: "ready" });
      } else {
        const statusMessage = latest.analysis_status === "cancelled"
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
        handleSessionLost("会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。");
        return;
      }
      // 网络错误（TypeError）或超时：立即退出轮询，不傻等 5 分钟。
      // 保留 retryOffer 让用户可以再次尝试检查状态。
      setError(`暂时无法确认任务状态：${error.message}。可稍后再次点击检查状态。`);
    } finally {
      if (retryController.current === controller) retryController.current = null;
      setRetryChecking(false);
    }
  }

  // 断点续跑：从上次中断的步骤继续分析，跳过已完成的步骤。
  // 与 retryAnalysis（重新运行）的区别：
  //   - retryAnalysis 从头开始，所有步骤重新执行
  //   - resumeAnalysis 复用已有 plan，跳过 completed 中的步骤，从中断处继续
  function resumeAnalysis() {
    if (!retryOffer?.canResume) return;
    const { task: resumeTask, plan: savedPlan, completed: savedCompleted } = retryOffer;
    setRetryOffer(null);
    setError("");
    startAnalysis(resumeTask, { plan: savedPlan || [], completed_steps: savedCompleted || [] });
  }

  // === Batch 4：计划审批回调 ===
  // 用户在 PlanPanel 中编辑计划后点击"批准并执行"触发，
  // 用编辑后的计划作为 resume_from，completed_steps 为空表示从头执行。
  const approvePlan = useCallback((editedPlan: PlanStep[]) => {
    setAwaitingApproval(false);
    setPlan(editedPlan);
    setStepProgress(null);
    startAnalysis(lastTaskRef.current, { plan: editedPlan, completed_steps: [] });
  }, [startAnalysis]);

  // 用户取消审批：丢弃编辑、清空待执行计划
  const cancelApproval = useCallback(() => {
    setAwaitingApproval(false);
    setPlan([]);
    setPendingObjective("");
    setCurrentNodeTitle("");
  }, []);

  // 从指定步骤重跑：截断 completed 至 index 之前的步骤，
  // 用原始 plan 作为 resume_from，跳过已保留部分。
  const rerunFromStep = useCallback((index: number) => {
    if (!session || running) return;
    // 截断 completed_steps：保留 index 之前的步骤，丢弃 index 及之后的
    const truncatedCompleted = completed.slice(0, index);
    startAnalysis(lastTaskRef.current, { plan, completed_steps: truncatedCompleted });
  }, [session, running, completed, plan, startAnalysis]);

  const profile = (session?.profile || null) as (DatasetProfile & { rows?: number; columns?: number; load_warnings?: string[] }) | null;
  const columnInfo = profile?.column_info || [];
  const missingCount = columnInfo.reduce((sum, item) => sum + ((item.missing as number) || 0), 0);
  const rows = (profile?.rows as number | undefined) ?? (profile?.row_count as number | undefined) ?? 0;
  const columns = (profile?.columns as number | undefined) ?? (profile?.column_count as number | undefined) ?? 0;
  const missingRate = profile ? ((missingCount / Math.max(rows * columns, 1)) * 100).toFixed(1) : "0.0";

  if (!authReady) {
    return <main className="auth-gate"><div className="auth-loading">正在连接数据工作台…</div></main>;
  }
  if (authRequired && !authenticated) {
    return <AccessGate onAuthenticated={() => { setAuthenticated(true); window.location.reload(); }} />;
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="wordmark">
          <strong>数据台</strong>
          <span>DATA DESK</span>
        </div>

        <button className="new-analysis" onClick={() => fileInput.current?.click()} disabled={uploading}>
          <FilePlus2 size={17} />
          新建分析
        </button>

        <div className="sidebar-section">
          <span className="sidebar-label">当前数据</span>
          {session ? (
            <button className="dataset-button" onClick={() => fileInput.current?.click()}>
              <FileSpreadsheet size={17} />
              <span>
                <strong>{session.filename}</strong>
                <small>{rows.toLocaleString()} 行 · {columns} 列</small>
              </span>
              <RefreshCw size={13} />
            </button>
          ) : (
            <button className="upload-button" onClick={() => fileInput.current?.click()} disabled={uploading}>
              {uploading ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}
              {uploading ? "正在读取" : "选择数据文件"}
            </button>
          )}
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".csv,.tsv,.xlsx,.xls,.json,.jsonl,.parquet"
            hidden
            onChange={(event) => {
              const files = event.target.files ? Array.from(event.target.files) : [];
              if (files.length === 0) return;
              uploadFile(files[0]);
              if (files.length > 1) {
                window.dispatchEvent(new CustomEvent("empty-workspace:extra-files", { detail: { files: files.slice(1) } }));
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
          onExportSession={exportSession}
          onImportSession={importSession}
          onDeleteSession={deleteSession}
          onRenameSession={renameSession}
        />

        <div className="sidebar-spacer" />

        {/* 分析引擎区块：model-line + 设置面板，已提取至 SettingsPanel 组件 */}
        <SettingsPanel />

        <div className="sidebar-foot">
          <span><i className={settings?.langsmith_tracing ? "online" : ""} />LangSmith</span>
          <small>{settings?.langsmith_tracing ? "追踪开启" : "本地模式"}</small>
        </div>
        <div className="storage-status">
          <span><i className={settings?.storage_status === "ok" ? "online" : "warning"} />对象存储</span>
          <small>{settings?.storage_status === "ok" ? "持久化正常" : "降级模式"}</small>
        </div>
      </aside>
      {sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} aria-hidden="true" />}

      <main className={session ? "app-main" : "app-main is-empty"}>
        <header className="topbar">
          <button type="button" className="sidebar-toggle" onClick={() => setSidebarOpen(true)} aria-label="打开侧边栏"><Menu size={18} /></button>
          <div className="breadcrumb">
            <span>分析工作区</span>
            <ChevronRight size={13} />
            <strong>{session?.filename || "未命名分析"}</strong>
          </div>
          <div className="topbar-actions">
            <div className="api-status"><i className={settings ? "online" : ""} />{settings ? "服务正常" : "连接中"}</div>
            {/* 命令面板入口：点击等价于 Cmd+K，给不熟悉快捷键的用户一个可见入口 */}
            <button
              type="button"
              className="icon-button topbar-action"
              title="命令面板 (⌘K)"
              aria-label="打开命令面板"
              onClick={() => setCommandOpen(true)}
            >
              <Command size={16} />
            </button>
            {/* 快捷键帮助入口：与 ? 快捷键等价 */}
            <button
              type="button"
              className="icon-button topbar-action"
              title="键盘快捷键 (?)"
              aria-label="查看键盘快捷键"
              onClick={() => setHelpOpen(true)}
            >
              <Keyboard size={16} />
            </button>
            {/* 主题切换：太阳/月亮图标随当前 theme 切换，与 T 快捷键等价 */}
            <button
              type="button"
              className="icon-button topbar-action theme-toggle"
              title={theme === "dark" ? "切换到亮色 (T)" : "切换到暗色 (T)"}
              aria-label="切换主题"
              onClick={toggleTheme}
            >
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
          </div>
        </header>

        {error && (
          <div className="error-banner" role="alert">
            <AlertTriangle size={16} />
            <span className={error.length > 120 ? (errorExpanded ? "" : "is-clamped") : ""}>
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
                <Play size={13} fill="currentColor" />继续分析
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
                {retryChecking ? "检查中…" : retryOffer.reason === "ready" ? "重新运行" : "检查状态"}
              </button>
            )}
            <button type="button" title="关闭" aria-label="关闭错误提示" className="error-close" onClick={() => setError("")}><X size={15} /></button>
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
                <Play size={13} fill="currentColor" />继续分析
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
              {retryChecking ? "检查中…" : retryOffer.reason === "ready" ? "重新运行" : "检查状态"}
            </button>
            <button type="button" title="放弃恢复" aria-label="放弃恢复" className="error-close" onClick={() => setRetryOffer(null)}><X size={13} /></button>
          </div>
        )}

        {!session ? (
          <EmptyWorkspace uploading={uploading} onUpload={() => fileInput.current?.click()} onFileDrop={uploadFile} />
        ) : (
          <>
            <section className="dataset-header">
              <div>
                <span className="section-kicker">当前数据集</span>
                <h1>{session.filename}</h1>
                {profile?.load_warnings && profile.load_warnings.length > 0 && (
                  <p className="dataset-warning"><AlertTriangle size={13} />{profile.load_warnings[0]}</p>
                )}
              </div>
              <button className="change-file" onClick={() => fileInput.current?.click()}>
                <RefreshCw size={14} />替换数据
              </button>
            </section>

            <section className="metrics-band">
              {/* 可点击指标：记录数 / 字段数 → 跳转数据 Tab；
                  分析产物 → 跳转产物 Tab。缺失率保持静态展示。 */}
              <button type="button" className="metric metric-clickable" onClick={() => setActiveTab("data")} title="查看数据预览">
                <span>记录</span>
                <strong>{rows.toLocaleString()}<small>行</small></strong>
              </button>
              <button type="button" className="metric metric-clickable" onClick={() => setActiveTab("data")} title="查看数据预览">
                <span>字段</span>
                <strong>{columns}<small>列</small></strong>
              </button>
              <Metric label="缺失率" value={missingRate} unit="%" />
              <button type="button" className="metric metric-clickable" onClick={() => setActiveTab("artifacts")} title="查看分析产物">
                <span>分析产物</span>
                <strong>{session.artifacts?.length || 0}<small>项</small></strong>
              </button>
            </section>

            {/* task-box 常驻顶部：无论切到哪个 tab 都能直接发起新分析 */}
            <div className={`task-box ${running ? "is-running" : ""}`}>
              <div className="task-heading">
                <div>
                  <span className="section-kicker">分析任务</span>
                  <h2>你想从数据中了解什么？</h2>
                </div>
                {running && (
                  <span className="task-running-hint">
                    <LoaderCircle className="spin" size={14} />
                    {currentNodeTitle ? `正在：${currentNodeTitle}` : "正在分析"}
                    {elapsedSeconds != null && ` · ${formatDuration(elapsedSeconds)}`}
                  </span>
                )}
              </div>
              <textarea
                ref={taskInput}
                value={task}
                onChange={(event) => setTask(event.target.value)}
                onKeyDown={(event) => {
                  // Ctrl/Cmd+Enter 快捷提交：与几乎所有聊天/搜索框一致，
                  // 避免用户输入完只能移动鼠标点按钮，破坏键盘操作流。
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                    event.preventDefault();
                    if (!running && task.trim() && settings?.configured) startAnalysis();
                  }
                }}
                placeholder="例如：比较各区域销售表现，解释异常波动并生成趋势图（⌘/Ctrl+Enter 运行）"
                rows={3}
              />
              <div className="task-actions">
                <div className="preset-row">
                  {presets.map(({ title, detail, icon: Icon, task: presetTask }) => (
                    <button
                      key={title}
                      title={settings?.configured ? detail : "请先在左下角配置 API Key"}
                      onClick={() => {
                        setTask(presetTask);
                        window.setTimeout(() => taskInput.current?.focus(), 0);
                      }}
                      disabled={running || !settings?.configured}
                    >
                      <Icon size={14} />{title}
                    </button>
                  ))}
                </div>
                {/* 任务输入提示 + 操作按钮分组：提示紧贴按钮左侧，
                    明确告知 Enter 换行、⌘/Ctrl+Enter 运行的键位约定 */}
                <div className="task-box-footer">
                  <small className="input-hint">Enter 换行 · ⌘/Ctrl+Enter 运行分析</small>
                  {running ? (
                    <button className="cancel-button" onClick={stopAnalysis} disabled={stopping}>
                      <Square size={13} fill="currentColor" />{stopping ? "停止中…" : "停止分析"}
                    </button>
                  ) : (
                    <>
                      <button className="plan-review-button" onClick={() => startAnalysis(task, null, true)} disabled={!task.trim() || !settings?.configured || !session} title="先生成计划，审阅后再执行">
                        <ListChecks size={15} />
                        审阅计划
                      </button>
                      <button className="run-button" onClick={() => startAnalysis()} disabled={!task.trim() || !settings?.configured}>
                        <Play size={15} fill="currentColor" />运行分析
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
            {!settings?.configured && <p className="composer-note">请先在左侧配置 DeepSeek API Key。</p>}

            {/* tabs 紧贴 task-box 下方：切换分析/数据/产物三个视图 */}
            {/* ARIA tablist 语义：roving tabindex + 左右箭头切换，屏幕阅读器可正确识别 */}
            <nav className="tabs" role="tablist" aria-label="工作区视图">
              <button
                id="tab-analysis"
                role="tab"
                aria-selected={activeTab === "analysis"}
                aria-controls="tabpanel-analysis"
                tabIndex={activeTab === "analysis" ? 0 : -1}
                className={activeTab === "analysis" ? "active" : ""}
                onClick={() => setActiveTab("analysis")}
                onKeyDown={(e) => {
                  if (e.key === "ArrowRight") { e.preventDefault(); document.getElementById("tab-data")?.focus(); }
                  else if (e.key === "ArrowLeft") { e.preventDefault(); document.getElementById("tab-artifacts")?.focus(); }
                }}
              ><BarChart3 size={15} />分析</button>
              <button
                id="tab-data"
                role="tab"
                aria-selected={activeTab === "data"}
                aria-controls="tabpanel-data"
                tabIndex={activeTab === "data" ? 0 : -1}
                className={activeTab === "data" ? "active" : ""}
                onClick={() => setActiveTab("data")}
                onKeyDown={(e) => {
                  if (e.key === "ArrowRight") { e.preventDefault(); document.getElementById("tab-artifacts")?.focus(); }
                  else if (e.key === "ArrowLeft") { e.preventDefault(); document.getElementById("tab-analysis")?.focus(); }
                }}
              ><Table2 size={15} />数据</button>
              <button
                id="tab-artifacts"
                role="tab"
                aria-selected={activeTab === "artifacts"}
                aria-controls="tabpanel-artifacts"
                tabIndex={activeTab === "artifacts" ? 0 : -1}
                className={activeTab === "artifacts" ? "active" : ""}
                onClick={() => setActiveTab("artifacts")}
                onKeyDown={(e) => {
                  if (e.key === "ArrowRight") { e.preventDefault(); document.getElementById("tab-analysis")?.focus(); }
                  else if (e.key === "ArrowLeft") { e.preventDefault(); document.getElementById("tab-data")?.focus(); }
                }}
              ><FileSpreadsheet size={15} />产物 <span>{session.artifacts?.length || 0}</span></button>
            </nav>

            {activeTab === "analysis" && (
              <div className="analysis-grid tab-content-enter" key="tab-analysis" id="tabpanel-analysis" role="tabpanel" aria-labelledby="tab-analysis" tabIndex={0}>
                <section className="analysis-column">
                  {result ? (
                    <>
                      <Suspense fallback={null}>
                        <ReportView
                          result={result}
                          streaming={running && !!result}
                          artifacts={result.artifacts || session?.artifacts}
                          onPreview={openArtifactPreview}
                          reasoning={reasoning}
                          reasoningStreaming={reasoningStreaming && running}
                          theme={theme}
                          usage={usage}
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
                    <DatasetOverview profile={profile} />
                  )}
                </section>
                <PlanPanel
                  plan={plan}
                  completed={completed}
                  running={running && session?.id === runningSessionIdRef.current}
                  currentNodeTitle={currentNodeTitle}
                  elapsedSeconds={session?.id === runningSessionIdRef.current ? elapsedSeconds : (session?.elapsed_seconds ?? null)}
                  toolTrace={session?.id === runningSessionIdRef.current ? toolTrace : []}
                  awaitingApproval={awaitingApproval}
                  stepProgress={stepProgress}
                  onApprovePlan={approvePlan}
                  onCancelApproval={cancelApproval}
                  onRerunFromStep={rerunFromStep}
                />
              </div>
            )}

            {activeTab === "data" && (
              <section className="data-view tab-content-enter" key="tab-data" id="tabpanel-data" role="tabpanel" aria-labelledby="tab-data" tabIndex={0}>
                <div className="section-title">
                  <div><span className="section-kicker">数据预览</span><h2>原始记录</h2></div>
                  <small>前 100 行</small>
                </div>
                <Suspense fallback={null}>
                  <DataTable rows={session.preview} />
                </Suspense>
              </section>
            )}

            {activeTab === "artifacts" && (
              <section className="artifact-view tab-content-enter" key="tab-artifacts" id="tabpanel-artifacts" role="tabpanel" aria-labelledby="tab-artifacts" tabIndex={0}>
                <div className="section-title artifact-title">
                  <div><span className="section-kicker">结果中心</span><h2>值得保留的结论</h2></div>
                  <small>中间文件已自动收起</small>
                </div>
                <ArtifactCenter
                  artifacts={session.artifacts}
                  onDownload={downloadArtifact}
                  onPreview={openArtifactPreview}
                  onBatchDownload={batchDownload}
                />
              </section>
            )}
          </>
        )}
      </main>
      {/* 产物预览模态：图表预览/对比/全屏/PNG/内联编辑，已提取至 PreviewModal；
          previewItem 为空时组件内部直接返回 null */}
      <PreviewModal preview={artifactPreview} theme={theme} onDownload={downloadArtifact} />
      {commandOpen && (
        <Suspense fallback={null}>
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
            onClose={() => { setCommandOpen(false); setCommandQuery(""); }}
            theme={theme}
          />
        </Suspense>
      )}
      {helpOpen && (
        <Suspense fallback={null}>
          <HelpPanel onClose={() => setHelpOpen(false)} />
        </Suspense>
      )}
    </div>
  );
}

export default App;
