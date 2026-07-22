import React, { Suspense, useCallback, useEffect, useRef } from "react";
import {
  AlertTriangle,
  BarChart3,
  Check,
  ChevronRight,
  Command,
  Download,
  Eye,
  EyeOff,
  FilePlus2,
  FileSpreadsheet,
  Keyboard,
  KeyRound,
  LoaderCircle,
  Moon,
  Play,
  RefreshCw,
  Settings2,
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
// 代码分割/懒加载（#16）：重型组件延迟加载，减小首次 bundle 体积。
// - CommandPalette / HelpPanel：弹层，仅在用户触发（Cmd+K / ?）时显示
// - ReportView：含 ReactMarkdown，仅在分析完成后渲染（result 非空）
// - DataTable：含搜索/排序逻辑，仅在切到"数据" Tab 时渲染
const DataTable = React.lazy(() => import("./components/DataTable"));
const ReportView = React.lazy(() => import("./components/ReportView"));
const CommandPalette = React.lazy(() => import("./components/CommandPalette"));
const HelpPanel = React.lazy(() => import("./components/HelpPanel"));
import useTheme from "./hooks/useTheme";
import { useAppStore } from "./store/useAppStore";
import { api, ApiError, describeApiError, requestHeaders } from "./utils/api";
import { consumeSSEStream } from "./utils/sse";
import { notifyAnalysisDone } from "./utils/notify";
import { formatDuration, wait } from "./utils/format";
import {
  API_URL,
  ACTIVE_ANALYSIS_STATES,
  COMMAND_ACTIONS,
  MAX_UPLOAD_BYTES_CLIENT,
  PREVIEW_CACHE_MAX,
  presets,
} from "./constants";

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
    // 配置
    settings, apiKey, effort, thinking,
    setSettings, setApiKey, setEffort, setThinking,
    // 会话
    session, activeTab,
    setSession, setActiveTab,
    // 分析任务
    task, plan, completed, result, running,
    setTask, setPlan, setCompleted, setResult, setRunning,
    // UI
    uploading, previewItem, previewHtml, previewLoading, previewError, currentNodeTitle,
    setUploading, setPreviewItem, setPreviewHtml, setPreviewLoading, setPreviewError, setCurrentNodeTitle,
    // 错误
    error, errorExpanded,
    setError, setErrorExpanded,
    // Key UI
    showKey, keyOpen,
    setShowKey, setKeyOpen,
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
    // Busy：保存设置 / 停止分析的 busy 状态，防止连点发多请求
    savingSettings, stopping,
    setSavingSettings, setStopping,
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
  const followUpInputRef = useRef(null);
  const chatControllerRef = useRef(null);
  // 分析开始时间戳（秒）。startAnalysis 时用客户端时间立即赋值，
  // 避免 useEffect 依赖 session.analysis_started_at —— 该字段只在
  // 后端 set_running() 后存在，前端 session 对象在 SSE 期间不会刷新，
  // 依赖它会导致 setInterval 永远不启动，计时停在 0。
  const startedAtRef = useRef(null);
  // 正在运行的 SSE 所属 session id。用户切换到历史会话时这个 ref 仍是原 session，
  // SSE 帧到达时若 currentSession.id !== runningSessionId，说明用户在查看历史，
  // 不应覆盖 plan/completed/result 等 UI 状态。
  const runningSessionIdRef = useRef(null);
  const fileInput = useRef(null);
  const taskInput = useRef(null);
  const analysisController = useRef(null);
  const previewController = useRef(null);
  // 预览 HTML LRU 缓存：每个图表 HTML 完全自包含（含 Plotly.js ~3.5MB），
  // 重复打开同一图表时秒开，避免重新 fetch + 解析。最多缓存 5 条。
  const previewCacheRef = useRef(new Map());
  const retryController = useRef(null);
  const cancelRequested = useRef(false);
  const lastTaskRef = useRef("");
  // 流式报告节流缓冲：report_chunk 每个 token 都直接 setResult 会触发
  // ReactMarkdown 全量重解析 AST，长报告（5000+ 字）在低端设备卡顿。
  // 改为缓冲 chunks，80ms 批量刷新一次（每秒约 12 次，人眼感知流畅）。
  const reportBufferRef = useRef("");
  const reportFlushTimerRef = useRef(null);
  // 按 sessionId 保存草稿：切换历史会话时不丢失当前正在输入的任务
  const taskDraftsRef = useRef({});

  // 主题（light/dark）：useTheme 内部读取 localStorage，无则跟随系统。
  const { theme, toggle: toggleTheme } = useTheme();

  // Esc 键关闭预览模态框或设置面板（P0-4）。
  // 之前 Esc 只对 previewItem 生效，设置面板打开时按 Esc 没反应，
  // 用户必须移动鼠标到右上角点 X，破坏键盘操作流。
  useEffect(() => {
    if (!previewItem && !keyOpen) return;
    const onKey = (event) => {
      if (event.key !== "Escape") return;
      if (previewItem) closeArtifactPreview();
      else if (keyOpen) { setKeyOpen(false); setApiKey(""); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewItem, keyOpen]);

  useEffect(() => {
    api("/api/auth")
      .then((status) => {
        setAuthRequired(status.required);
        setAuthenticated(status.authenticated);
        if (!status.required || status.authenticated) return api("/api/settings");
        return null;
      })
      .then((value) => {
        if (value) {
          setSettings(value);
          setEffort(value.reasoning_effort);
          setThinking(value.thinking_enabled);
          setKeyOpen(!value.configured);
        }
        setAuthReady(true);
      })
      .catch((err) => {
        setAuthReady(true);
        setError(`后端连接失败：${err.message}`);
      });
  }, []);

  // 拉取历史会话列表。鉴权通过后立即拉一次，让用户在初次进入时就能
  // 看到之前的会话；上传/切换/分析完成时也会调用，保持列表新鲜。
  // manual=true 表示用户主动触发（点刷新按钮），才设置 historyLoading
  // 让刷新按钮转圈；轮询调用 manual=false，不触发按钮 disabled，避免
  // 30 秒一次的轮询让整个历史列表短暂瘫痪（用户正要点击时被禁用）。
  const fetchHistory = async (manual = false) => {
    if (manual) setHistoryLoading(true);
    try {
      const payload = await api("/api/sessions?limit=30");
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

  useEffect(() => {
    if (authReady && (!authRequired || authenticated)) fetchHistory();
  }, [authReady, authRequired, authenticated]);

  // 切换到历史会话：拉取完整 session payload，并恢复 result/plan/completed。
  // 失败时（404 等）按 handleSessionLost 处理，避免遗留半残状态。
  // 注意：running 时不覆盖 startedAtRef —— 当前 SSE 流仍在后台跑原分析，
  // 计时应继续基于原分析的 started_at，否则会跳到新会话的 started_at
  // 导致"已耗时"突然变成一个不相关的数字。
  const selectSession = async (item) => {
    if (!item?.id || item.id === session?.id || switchingSessionId) return;
    // 保存当前会话的草稿，切换后还能恢复（用户输入了一半任务还没运行）
    if (session?.id) taskDraftsRef.current[session.id] = task;
    setError("");
    closeArtifactPreview();
    setSwitchingSessionId(item.id);
    try {
      const latest = await api(`/api/sessions/${item.id}`);
      setSession(latest);
      // 恢复目标会话的草稿（若有），否则清空
      setTask(taskDraftsRef.current[item.id] || "");
      setPlan([]);
      setCompleted([]);
      setResult(null);
      setCurrentNodeTitle("");
      setRetryOffer(null);
      restoreCompletedAnalysis(latest);
      restoreFollowUps(latest);
      if (!running) {
        startedAtRef.current = latest.analysis_started_at ?? null;
        setElapsedSeconds(latest.elapsed_seconds ?? null);
      }
      setActiveTab("analysis");
      fetchHistory();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        handleSessionLost("该历史会话已被服务端清理，请选择其他会话或重新上传数据。");
      } else {
        setError(`无法打开历史会话：${err.message}`);
      }
    } finally {
      setSwitchingSessionId(null);
    }
  };

  // 实时耗时：running 时持续刷新 elapsed = now - startedAtRef。
  // 关键设计：
  //   1. 用 ref 而非 session.analysis_started_at 作依赖——后者在 SSE
  //      期间不会刷新到前端，会让 setInterval 永远不启动。
  //   2. tick 频率 250ms（rAF 级别流畅），但只在"显示秒数"变化时
  //      setState，避免每秒一次的视觉跳变和不必要的 React 渲染。
  //   3. 后台 tab 暂停 setInterval（visibilitychange hidden），节省
  //      CPU 并避免 throttled timer 造成累积漂移；回到前台立即 tick
  //      一次追上真实耗时。
  //   4. running 转 false 时 effect cleanup 清 interval，自然停止。
  //   5. tick 每次都重新读 startedAtRef.current，而不是闭包捕获 started。
  //      complete 帧后 refresh 会用服务端精确 started_at 校正 ref，闭包
  //      捕获的旧值会让校正失效（下一帧用旧 started 重新算 elapsed 覆盖）。
  useEffect(() => {
    if (!running) return undefined;
    if (!startedAtRef.current) return undefined;
    let lastDisplayedSecond = -1;
    const tick = () => {
      const started = startedAtRef.current;
      if (!started) return;
      const elapsed = Math.max(0, Date.now() / 1000 - started);
      const currentSecond = Math.floor(elapsed);
      // 只有秒数实际变化才 setState，250ms 的 tick 大多数时候是 no-op。
      if (currentSecond !== lastDisplayedSecond) {
        lastDisplayedSecond = currentSecond;
        setElapsedSeconds(elapsed);
      }
    };
    const onVisibilityChange = () => {
      if (document.hidden) {
        window.clearInterval(timer);
        timer = null;
      } else if (!timer) {
        tick();
        timer = window.setInterval(tick, 250);
      }
    };
    tick();
    let timer = window.setInterval(tick, 250);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      if (timer) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [running]);

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

  // 全局键盘快捷键：Cmd+K 命令面板、? 快捷键帮助、T 切换主题、Cmd+B 折叠侧栏、
  // 1/2/3 切换 Tab、Cmd+. 停止分析。这些快捷键参考 Linear / Notion / VSCode，
  // 让熟练用户完全脱离鼠标操作，是缩小与主流 Agent 体验差距的关键。
  // 注意：在 input/textarea/contenteditable 中按键时跳过单字符快捷键（? T 1 2 3），
  // 避免用户输入这些字符时误触发；Cmd 组合键不受此限制。
  useEffect(() => {
    const onKey = (event) => {
      const target = event.target;
      const isTyping = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      const meta = event.metaKey || event.ctrlKey;

      // Cmd+K：打开命令面板（任何焦点下都生效）
      if (meta && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((v) => !v);
        return;
      }
      // Cmd+B：折叠/展开历史侧栏
      if (meta && event.key.toLowerCase() === "b") {
        event.preventDefault();
        setHistoryExpanded((v) => !v);
        return;
      }
      // Cmd+.：停止正在运行的分析
      if (meta && event.key === ".") {
        if (running) { event.preventDefault(); stopAnalysis(); }
        else if (chatRunning) { event.preventDefault(); stopFollowUp(); }
        return;
      }
      // 单字符快捷键：只在非输入态下生效
      if (isTyping) return;
      // ?：打开快捷键帮助面板（Shift+/）
      if (event.key === "?" && !meta) {
        event.preventDefault();
        setHelpOpen((v) => !v);
        return;
      }
      // T：切换主题
      if (event.key.toLowerCase() === "t" && !meta && !event.altKey) {
        event.preventDefault();
        toggleTheme();
        return;
      }
      // 1/2/3：切换分析/数据/产物 Tab
      if (event.key === "1" && !meta) { event.preventDefault(); setActiveTab("analysis"); return; }
      if (event.key === "2" && !meta) { event.preventDefault(); setActiveTab("data"); return; }
      if (event.key === "3" && !meta) { event.preventDefault(); setActiveTab("artifacts"); return; }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [running, chatRunning, toggleTheme]);

  // 命令面板动作执行器：根据 action.id 路由到具体操作。
  // 用 useCallback 保持身份稳定，作为 props 传入 CommandPalette 时不会触发重渲染。
  const runCommandAction = useCallback((action) => {
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
  const handleEditFollowUp = useCallback((index, newContent) => {
    setFollowUps((prev) => prev.slice(0, index));
    setFollowUpInput(newContent);
    // 让输入框立即获得焦点，用户可直接 Cmd+Enter 发送
    window.setTimeout(() => followUpInputRef.current?.focus(), 0);
  }, []);

  // useCallback：openArtifactPreview / downloadArtifact 作为 props 传给
  // React.memo(ArtifactCenter)。若每次渲染都创建新函数，memo 比较失败，
  // ArtifactCenter 仍然每次重渲染。useCallback 让函数身份稳定，memo 才
  // 能真正跳过无关重渲染。
  const openArtifactPreview = useCallback(async (item) => {
    if (!item.preview_url) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setPreviewItem(item);
    setPreviewError("");
    // LRU 缓存命中：直接展示已加载的 HTML，跳过 fetch + 解析（Plotly.js ~3.5MB）。
    // 重复打开同一图表时秒开，多个图表来回切换无需重新加载。
    const cacheKey = item.preview_url;
    const cached = previewCacheRef.current.get(cacheKey);
    if (cached !== undefined) {
      // LRU：删除再插入，将命中条目移到 Map 末尾标记为最近使用
      previewCacheRef.current.delete(cacheKey);
      previewCacheRef.current.set(cacheKey, cached);
      setPreviewHtml(cached);
      setPreviewLoading(false);
      return;
    }
    setPreviewHtml("");
    setPreviewLoading(true);
    try {
      // 预览仍使用请求头鉴权，主访问令牌不会进入 URL、历史记录或服务器 access log。
      // 服务端返回完全离线的文档，再交给无同源权限的 sandbox iframe 执行。
      const response = await fetch(`${API_URL}${item.preview_url}`, {
        headers: requestHeaders(),
        signal: controller.signal,
      });
      const html = await response.text();
      if (!response.ok) {
        let payload = html;
        try {
          payload = JSON.parse(html);
        } catch {
          // 非 JSON 错误正文交给统一错误描述处理。
        }
        throw new Error(describeApiError(payload, response.status));
      }
      // 缓存结果：超过上限时淘汰 Map 中最旧（最久未使用）的条目
      previewCacheRef.current.set(cacheKey, html);
      if (previewCacheRef.current.size > PREVIEW_CACHE_MAX) {
        const oldest = previewCacheRef.current.keys().next().value;
        previewCacheRef.current.delete(oldest);
      }
      setPreviewHtml(html);
      // loading 状态由 iframe onLoad 关闭，确保用户看到的是完成渲染的图表。
    } catch (err) {
      if (err.name !== "AbortError") {
        setPreviewLoading(false);
        setPreviewError(`图表加载失败：${err.message}`);
      }
    } finally {
      if (previewController.current === controller) previewController.current = null;
    }
  }, []);

  const closeArtifactPreview = useCallback(() => {
    previewController.current?.abort();
    previewController.current = null;
    setPreviewItem(null);
    setPreviewHtml("");
    setPreviewLoading(false);
    setPreviewError("");
  }, []);

  const downloadArtifact = useCallback(async (item) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetch(`${API_URL}${item.download_url}`, {
        headers: requestHeaders(),
        signal: controller.signal,
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const payload = contentType.includes("application/json")
          ? await response.json().catch(() => ({}))
          : await response.text().catch(() => "");
        throw new Error(describeApiError(payload, response.status));
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = item.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      setError(`下载失败：${err.name === "AbortError" ? "下载超时，请稍后重试。" : err.message}`);
    } finally {
      window.clearTimeout(timeout);
    }
  }, []);

  async function saveSettings() {
    if (savingSettings) return;
    setError("");
    setSavingSettings(true);
    try {
      const payload = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey || undefined,
          thinking_enabled: thinking,
          reasoning_effort: effort,
          persist_key: true,
        }),
      });
      setSettings(payload);
      setApiKey("");
      setKeyOpen(false);
      if (payload.warning) setError(payload.warning);
    } catch (err) {
      setError(err.message);
    } finally {
      setSavingSettings(false);
    }
  }

  async function uploadFile(file) {
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
      const value = await api("/api/sessions", { method: "POST", body: form });
      setSession(value);
      startedAtRef.current = value.analysis_started_at ?? null;
      setElapsedSeconds(value.elapsed_seconds ?? null);
      setFollowUps([]);
      fetchHistory();
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

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

  // 轻量追问：基于已有分析结果继续提问，走 /chat/stream 端点。
  // 与 startAnalysis 的区别：
  //   - 不触发 plan→execute→finalize 工作流，单次 ReAct 循环回答
  //   - 回答渲染为对话气泡（而非主报告区），保留主报告不动
  //   - 工具调用时间线内嵌在气泡内，不占用 PlanPanel 的全局 toolTrace
  //   - 新产物追加到 session.artifacts，让产物中心即时更新
  async function startFollowUp() {
    const message = followUpInput.trim();
    if (!session || !message || chatRunning || running) return;
    setChatRunning(true);
    setError("");
    setFollowUpInput("");
    // 乐观插入用户气泡 + 空壳 assistant 气泡，让用户立即看到自己的提问
    const userBubble = { role: "user", content: message };
    const assistantBubble = { role: "assistant", content: "", streaming: true, tools: [] };
    setFollowUps((prev) => [...prev, userBubble, assistantBubble]);
    const controller = new AbortController();
    chatControllerRef.current = controller;
    let idleTimeout = null;
    const resetIdleTimeout = () => {
      if (idleTimeout) window.clearTimeout(idleTimeout);
      idleTimeout = window.setTimeout(() => controller.abort(), 120000);
    };
    resetIdleTimeout();
    try {
      const response = await fetch(`${API_URL}/api/sessions/${session.id}/chat/stream`, {
        method: "POST",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ task: message }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new ApiError(describeApiError(payload, response.status), response.status);
      }
      // SSE 事件分发：buffer 拆分 / event+data 提取 / JSON 解析由
      // consumeSSEStream 统一处理，这里只声明各事件的业务逻辑。
      // error 事件抛出的异常会被 consumeSSEStream 重新抛出，落到下面的 catch。
      const appendToLastAssistant = (mutate) => setFollowUps((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant") {
          next[next.length - 1] = mutate(last);
        }
        return next;
      });
      await consumeSSEStream(response, {
        chat_chunk: (data) => {
          // 流式追加到最后一个 assistant 气泡
          appendToLastAssistant((last) => ({
            ...last,
            content: (last.content || "") + (data.chunk || ""),
          }));
        },
        thinking_chunk: (data) => {
          // 思考过程追加到最后一个 assistant 气泡的 reasoning 字段，
          // ConversationBubble 内嵌的 ReasoningBlock 会自动展示。
          if (!data.chunk) return;
          appendToLastAssistant((last) => ({
            ...last,
            reasoning: (last.reasoning || "") + (data.chunk || ""),
          }));
        },
        tool_call: (data) => {
          appendToLastAssistant((last) => ({
            ...last,
            tools: [...(last.tools || []), {
              call_id: data.call_id, name: data.name,
              status: "running", started_at: data.started_at,
            }],
          }));
        },
        tool_result: (data) => {
          appendToLastAssistant((last) => ({
            ...last,
            tools: (last.tools || []).map((t) => t.call_id === data.call_id
              ? { ...t, status: "done", duration_ms: data.duration_ms }
              : t),
          }));
        },
        chat_done: (data) => {
          // 终态：写入完整回复（防止 chunk 丢失），标记非流式，追加新产物
          appendToLastAssistant((last) => ({
            ...last,
            content: data.response || last.content,
            streaming: false,
            // 后端可能在终态一次性给出完整 reasoning / usage，覆盖流式累计值
            reasoning: data.reasoning || last.reasoning,
            usage: data.usage || last.usage,
          }));
          if (data.artifacts?.length) {
            setSession((current) => current
              ? { ...current, artifacts: [...(current.artifacts || []), ...data.artifacts] }
              : current);
          }
        },
        cancelled: (data) => {
          appendToLastAssistant((last) => ({
            ...last,
            streaming: false,
            error: data.message || "追问已取消。",
          }));
        },
        error: (data) => {
          throw new Error(data.message || "追问失败");
        },
      }, { onChunk: resetIdleTimeout });
    } catch (err) {
      if (err.name === "AbortError") {
        setFollowUps((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === "assistant" && last.streaming) {
            next[next.length - 1] = { ...last, streaming: false, error: "追问已取消或超时。" };
          }
          return next;
        });
      } else {
        setFollowUps((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === "assistant") {
            next[next.length - 1] = { ...last, streaming: false, error: err.message };
          }
          return next;
        });
      }
    } finally {
      if (idleTimeout) window.clearTimeout(idleTimeout);
      if (chatControllerRef.current === controller) chatControllerRef.current = null;
      setChatRunning(false);
    }
  }

  function stopFollowUp() {
    chatControllerRef.current?.abort();
  }

  async function startAnalysis(nextTask = task, resumeFrom = null) {
    if (!session || !nextTask.trim() || running) return;
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (_) { /* noop */ }
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
    if (reportFlushTimerRef.current) {
      clearTimeout(reportFlushTimerRef.current);
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
    let idleTimeout = null;
    let completedPayload = null;
    let sawEvent = false;
    const resetIdleTimeout = () => {
      if (idleTimeout) window.clearTimeout(idleTimeout);
      idleTimeout = window.setTimeout(() => controller.abort(), 180000);
    };
    resetIdleTimeout();
    // 节流刷新：将缓冲区内容批量写入 state，避免每个 token 都触发 ReactMarkdown 重解析。
    // 定义在 try 之前，以便 complete/cancelled/error/finally 都能调用。
    const flushReportBuffer = () => {
      if (reportFlushTimerRef.current) {
        clearTimeout(reportFlushTimerRef.current);
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
        body: JSON.stringify({ task: nextTask, resume_from: resumeFrom }),
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
        started: () => {
          if (session.id === runningSessionIdRef.current) setCurrentNodeTitle("后端已接收任务");
        },
        progress: (data) => {
          if (session.id === runningSessionIdRef.current) setCurrentNodeTitle(data.title || "正在分析");
        },
        validate_dataset: () => {
          if (session.id === runningSessionIdRef.current) setCurrentNodeTitle("正在检查数据集结构");
        },
        plan_analysis: (data) => {
          if (session.id === runningSessionIdRef.current) {
            setPlan(data.plan || []);
            setCurrentNodeTitle("正在规划分析步骤");
          }
        },
        execute_step: () => {
          if (session.id === runningSessionIdRef.current) {
            setCurrentNodeTitle((current) => current || "正在执行分析步骤");
          }
        },
        replan: (data) => {
          if (session.id === runningSessionIdRef.current) {
            setCompleted(data.completed_steps || []);
            setCurrentNodeTitle("正在审查进度并重规划");
          }
        },
        thinking_chunk: (data) => {
          // DeepSeek reasoning_content：流式思考过程。开始接收时打开 streaming 标记，
          // ReportView 顶部的 ReasoningBlock 会自动展开；接收完后由 report_chunk / complete
          // 阶段自然关闭 streaming。思考过程让用户看到 Agent 的推理链路，减少"黑盒等待"焦虑。
          if (session.id === runningSessionIdRef.current) {
            if (!data.chunk) return;
            setReasoningStreaming(true);
            setReasoning((prev) => prev + (data.chunk || ""));
          }
        },
        finalize: () => {
          if (session.id === runningSessionIdRef.current) {
            setCurrentNodeTitle("正在汇总最终报告");
            // finalize 阶段开始输出报告正文，思考过程已结束，关闭 streaming
            setReasoningStreaming(false);
            // 创建空壳 result，让 ReportView 立即显示"正在生成报告…"占位，
            // 后续 report_chunk 事件会逐字追加 response，实现流式打字效果。
            setResult((prev) => prev || { response: "", artifacts: [], plan, completed_steps: completed });
          }
        },
        report_chunk: (data) => {
          // 流式报告：逐字追加，用户看着报告逐字写出，而不是等 30-60 秒看完整报告。
          // 节流：不直接 setResult（每个 token 触发 ReactMarkdown 全量重解析 AST），
          // 而是写入 ref 缓冲，80ms 批量刷新一次。长报告（5000+ 字）在低端设备更流畅。
          if (session.id === runningSessionIdRef.current) {
            reportBufferRef.current += (data.chunk || "");
            if (!reportFlushTimerRef.current) {
              reportFlushTimerRef.current = setTimeout(() => {
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
        tool_call: (data) => {
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
        tool_result: (data) => {
          // 工具调用结束：更新对应 call_id 的状态和耗时
          if (session.id === runningSessionIdRef.current) {
            setToolTrace((prev) => prev.map((item) => item.call_id === data.call_id
              ? { ...item, status: "done", output_preview: data.output_preview, duration_ms: data.duration_ms }
              : item
            ));
          }
        },
        complete: (data) => {
          // complete 帧仍需记录 completedPayload，以便结束后 setRunning(false)
          // 和刷新 history，让历史列表反映新状态（即使用户已切到历史会话）。
          completedPayload = data;
          if (session.id === runningSessionIdRef.current) {
            // 清除节流定时器并丢弃缓冲：complete 帧的 data 是权威最终结果，
            // pending flush 不得覆盖它。
            if (reportFlushTimerRef.current) {
              clearTimeout(reportFlushTimerRef.current);
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
            if (data.reasoning) setReasoning(data.reasoning);
            if (data.usage) setUsage(data.usage);
          }
          notifyAnalysisDone("分析已完成", nextTask || "数据分析任务已完成");
        },
        cancelled: (data) => {
          // 仅在用户未通过 stopAnalysis 主动取消时显示后端取消消息，避免重复 setError。
          if (!cancelRequested.current && session.id === runningSessionIdRef.current) {
            setError(data.message || "分析已取消。");
          }
          // 立即刷新缓冲：让用户看到取消前已生成的报告内容
          flushReportBuffer();
          // 断点续跑：取消时若有已完成步骤，提供"继续分析"入口
          if (completed.length > 0) {
            setRetryOffer({ task: nextTask, reason: "cancelled", canResume: true, plan, completed });
          }
        },
        error: (data) => {
          // 立即刷新缓冲：让用户看到出错前已生成的报告内容
          flushReportBuffer();
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
          const refreshed = await api(`/api/sessions/${session.id}`);
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
      // 通用：取消/失败时若有已完成步骤，附加 canResume 标记让重试栏显示"继续分析"
      const resumePayload = completed.length > 0
        ? { task: nextTask, canResume: true, plan, completed }
        : null;
      if (err.name === "AbortError" && cancelRequested.current) {
        setError("分析已取消，已完成的步骤不会继续扩展。");
        if (resumePayload) setRetryOffer({ ...resumePayload, reason: "cancelled" });
      } else if (err.name === "AbortError") {
        // idle timeout：长时间未收到事件。
        setError("长时间未收到分析进度，连接已断开。");
        setRetryOffer(resumePayload ? { ...resumePayload, reason: "idle" } : { task: nextTask, reason: "idle" });
      } else if (!sawEvent && err.name === "TypeError") {
        // fetch 网络层错误（DNS/CORS/离线），尚未收到任何 SSE 帧。
        setError(`无法连接分析服务：${err.message}`);
        setRetryOffer(resumePayload ? { ...resumePayload, reason: "network" } : { task: nextTask, reason: "network" });
      } else if (err instanceof ApiError && err.status === 404) {
        // 重运行时服务端 session 已被清理，引导用户重新上传。
        handleSessionLost("会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。");
      } else {
        setError(err.message);
        setRetryOffer(resumePayload ? { ...resumePayload, reason: "error" } : { task: nextTask, reason: "error" });
      }
    } finally {
      if (idleTimeout) window.clearTimeout(idleTimeout);
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

  function restoreCompletedAnalysis(latest) {
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
      .find((item) => item.role === "assistant" && item.content);
    if (!assistantMessage) return false;
    setResult({
      response: assistantMessage.content,
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
  function restoreFollowUps(latest) {
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
    })));
  }

  // 会话失效（404）时清空前端状态，引导用户回到上传界面。
  // Render 免费实例重启会清空 /tmp，session 数据不可恢复，与其让用户
  // 反复点"检查状态"得到 404，不如明确告知并重置。
  function handleSessionLost(message) {
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

  async function retryAnalysis() {
    if (!retryOffer) return;
    const retryTask = retryOffer.task;
    if (retryOffer.reason === "ready") {
      setRetryOffer(null);
      startAnalysis(retryTask);
      return;
    }

    // 轮询期间使用独立的 AbortController，让 stopAnalysis 能中断轮询。
    retryController.current?.abort();
    const controller = new AbortController();
    retryController.current = controller;

    setRetryChecking(true);
    try {
      // 单次查询用短超时（8s），让网络故障快速暴露而不是卡 45s。
      let latest = await api(`/api/sessions/${session.id}`, { timeoutMs: 8000, signal: controller.signal });
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
          latest = await api(`/api/sessions/${session.id}`, { timeoutMs: 8000, signal: controller.signal });
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
      setRunning(false);
      setCurrentNodeTitle("");
      if (err.name === "AbortError") {
        // 用户主动取消或 stopAnalysis 中断，不覆盖已有 error。
        return;
      }
      if (err instanceof ApiError && err.status === 404) {
        // 服务端 session 已被清理（Render 重启 /tmp 清空、TTL 过期等）。
        // 明确告知用户并重置到上传界面，避免用户反复点"检查状态"得到 404。
        handleSessionLost("会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。");
        return;
      }
      // 网络错误（TypeError）或超时：立即退出轮询，不傻等 5 分钟。
      // 保留 retryOffer 让用户可以再次尝试检查状态。
      setError(`暂时无法确认任务状态：${err.message}。可稍后再次点击检查状态。`);
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
    const { task, plan: savedPlan, completed: savedCompleted } = retryOffer;
    setRetryOffer(null);
    setError("");
    startAnalysis(task, { plan: savedPlan, completed_steps: savedCompleted });
  }

  const profile = session?.profile;
  const missingCount = profile?.column_info?.reduce((sum, item) => sum + item.missing, 0) || 0;
  const missingRate = profile ? ((missingCount / Math.max(profile.rows * profile.columns, 1)) * 100).toFixed(1) : "0.0";

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
                <small>{profile.rows.toLocaleString()} 行 · {profile.columns} 列</small>
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
            accept=".csv,.tsv,.xlsx,.xls,.json,.jsonl,.parquet"
            hidden
            onChange={(event) => uploadFile(event.target.files?.[0])}
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
        />

        <div className="sidebar-spacer" />

        <div className="provider-block">
          <span className="sidebar-label">分析引擎</span>
          <button className="model-line" onClick={() => setKeyOpen((value) => !value)}>
            <span>
              <i className={settings?.configured ? "online" : ""} />
              <span><strong>{settings?.model || "deepseek-chat"}</strong><small>{settings?.configured ? "已连接" : "等待配置"}</small></span>
            </span>
            <Settings2 size={15} />
          </button>
          {keyOpen && (
            <form className="settings-form" onSubmit={(event) => { event.preventDefault(); saveSettings(); }}>
              <div className="settings-title">
                <strong>模型设置</strong>
                <button type="button" title="收起设置（Esc）" aria-label="收起设置" onClick={() => { setKeyOpen(false); setApiKey(""); }}><X size={15} /></button>
              </div>
              <label>API Key</label>
              <div className="secret-input">
                <KeyRound size={14} />
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={settings?.configured ? "已安全保存，留空则不变" : "输入 DeepSeek Key"}
                />
                <button type="button" title={showKey ? "隐藏 Key" : "显示 Key"} onClick={() => setShowKey((value) => !value)}>
                  {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              </div>
              <label className="toggle-row">
                <span>思考模式</span>
                <input type="checkbox" checked={thinking} onChange={(event) => setThinking(event.target.checked)} />
              </label>
              <label>推理强度</label>
              <div className="segment">
                {[{ value: "high", label: "标准" }, { value: "max", label: "深度" }].map(({ value, label }) => (
                  <button type="button" key={value} title={value === "high" ? "标准推理速度，适合大多数场景" : "最深推理，效果更好但更慢"} className={effort === value ? "selected" : ""} onClick={() => setEffort(value)}>{label}</button>
                ))}
              </div>
              <button type="submit" className="save-button" disabled={savingSettings}>
                {savingSettings ? <><LoaderCircle size={14} className="spin" />保存中…</> : <><Check size={14} />保存设置</>}
              </button>
            </form>
          )}
        </div>

        <div className="sidebar-foot">
          <span><i className={settings?.langsmith_tracing ? "online" : ""} />LangSmith</span>
          <small>{settings?.langsmith_tracing ? "追踪开启" : "本地模式"}</small>
        </div>
        <div className="storage-status">
          <span><i className={settings?.storage_status === "ok" ? "online" : "warning"} />对象存储</span>
          <small>{settings?.storage_status === "ok" ? "持久化正常" : "降级模式"}</small>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
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
                onClick={retryAnalysis}
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
              onClick={retryAnalysis}
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
                {profile.load_warnings?.length > 0 && (
                  <p className="dataset-warning"><AlertTriangle size={13} />{profile.load_warnings[0]}</p>
                )}
              </div>
              <button className="change-file" onClick={() => fileInput.current?.click()}>
                <RefreshCw size={14} />替换数据
              </button>
            </section>

            <section className="metrics-band">
              <Metric label="记录" value={profile.rows.toLocaleString()} unit="行" />
              <Metric label="字段" value={profile.columns} unit="列" />
              <Metric label="缺失率" value={missingRate} unit="%" />
              <Metric label="分析产物" value={session.artifacts?.length || 0} unit="项" />
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
                {running ? (
                  <button className="cancel-button" onClick={stopAnalysis} disabled={stopping}>
                    <Square size={13} fill="currentColor" />{stopping ? "停止中…" : "停止分析"}
                  </button>
                ) : (
                  <button className="run-button" onClick={() => startAnalysis()} disabled={!task.trim() || !settings?.configured}>
                    <Play size={15} fill="currentColor" />运行分析
                  </button>
                )}
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
              <div className="analysis-grid" id="tabpanel-analysis" role="tabpanel" aria-labelledby="tab-analysis" tabIndex={0}>
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
                />
              </div>
            )}

            {activeTab === "data" && (
              <section className="data-view" id="tabpanel-data" role="tabpanel" aria-labelledby="tab-data" tabIndex={0}>
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
              <section className="artifact-view" id="tabpanel-artifacts" role="tabpanel" aria-labelledby="tab-artifacts" tabIndex={0}>
                <div className="section-title artifact-title">
                  <div><span className="section-kicker">结果中心</span><h2>值得保留的结论</h2></div>
                  <small>中间文件已自动收起</small>
                </div>
                <ArtifactCenter
                  artifacts={session.artifacts}
                  onDownload={downloadArtifact}
                  onPreview={openArtifactPreview}
                />
              </section>
            )}
          </>
        )}
      </main>
      {previewItem && (
        <div
          className="preview-backdrop"
          role="presentation"
          onClick={(event) => {
            // 用 onClick 而非 onMouseDown，同时覆盖鼠标点击和触摸结束，
            // 避免纯触屏设备上 mousedown 被 preventDefault 或延迟 300ms。
            if (event.target === event.currentTarget) closeArtifactPreview();
          }}
        >
          <section className="preview-panel" role="dialog" aria-modal="true" aria-label={`预览 ${previewItem.description || previewItem.name}`}>
            <header>
              <div>
                <span className="section-kicker">交互图表</span>
                <h2>{previewItem.description || previewItem.name}</h2>
              </div>
              <div className="preview-actions">
                <button type="button" onClick={() => downloadArtifact(previewItem)}><Download size={15} />下载</button>
                <button type="button" className="icon-button" title="关闭预览 (Esc)" onClick={closeArtifactPreview}><X size={17} /></button>
              </div>
            </header>
            <div className="preview-stage">
              {previewLoading && !previewError && (
                <div className="preview-loading"><LoaderCircle className="spin" size={18} />正在准备交互图表…</div>
              )}
              {previewError && (
                <div className="preview-loading preview-error">
                  <AlertTriangle size={18} />
                  <span>{previewError}</span>
                  <button type="button" className="retry-button" onClick={() => openArtifactPreview(previewItem)}>
                    <RefreshCw size={13} />重试
                  </button>
                </div>
              )}
              {previewHtml && !previewError && (
                <iframe
                  title={previewItem.description || previewItem.name}
                  sandbox="allow-scripts"
                  referrerPolicy="no-referrer"
                  srcDoc={previewHtml}
                  onLoad={(e) => {
                    setPreviewLoading(false);
                    try {
                      e.target.contentWindow.document.documentElement.dataset.theme = theme;
                    } catch (_) { /* sandbox 跨域时 noop，图表脚本回退到 prefers-color-scheme */ }
                  }}
                  onError={() => {
                    setPreviewLoading(false);
                    setPreviewError("图表加载失败，请检查网络或重新生成产物。");
                  }}
                />
              )}
            </div>
          </section>
        </div>
      )}
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
