import React, { Component, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight, oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
// 按需注册 Prism 语言：数据分析场景仅涉及 SQL/Python/JSON/JS/Bash 等。
// 全量导入 Prism 会注册 200+ 语言定义（gzip 后数百 KB），PrismLight 只注册
// 用到的语言，可减少 bundle 体积 200-400KB（gzip）。
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";

SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("markdown", markdown);
import {
  AlertTriangle,
  Activity,
  BarChart3,
  Boxes,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock,
  Command,
  CornerDownLeft,
  Database,
  Download,
  Eye,
  EyeOff,
  ExternalLink,
  FileCheck2,
  FileChartColumn,
  FilePlus2,
  FileSpreadsheet,
  Grid3x3,
  History,
  KeyRound,
  Keyboard,
  LineChart,
  LoaderCircle,
  Moon,
  Network,
  PieChart,
  Play,
  RefreshCw,
  Rows3,
  ScatterChart,
  Search,
  Settings2,
  Square,
  Sun,
  Table2,
  Upload,
  X,
} from "lucide-react";
import "./styles.css";

const API_URL = (
  import.meta.env.VITE_API_URL ||
  (import.meta.env.PROD ? window.location.origin : "http://127.0.0.1:8000")
).replace(/\/$/, "");
const ACCESS_TOKEN_KEY = "data-desk-access-token";
const THEME_KEY = "data-desk-theme";
const ACTIVE_ANALYSIS_STATES = new Set(["running", "cancelling"]);

// 快捷键帮助面板的内容定义。集中维护，避免散落在多处 JSX。
// 每条快捷键对应一个真实可用的全局或上下文快捷键（见 App 内的 keydown 监听）。
const HELP_SHORTCUTS = [
  {
    section: "通用",
    items: [
      { keys: ["⌘", "K"], desc: "打开命令面板" },
      { keys: ["?"], desc: "查看键盘快捷键" },
      { keys: ["⌘", "B"], desc: "展开/收起历史会话侧栏" },
      { keys: ["Esc"], desc: "关闭当前弹层" },
    ],
  },
  {
    section: "分析",
    items: [
      { keys: ["⌘", "Enter"], desc: "运行分析任务 / 发送追问" },
      { keys: ["⌘", "."], desc: "停止正在运行的分析" },
      { keys: ["T"], desc: "切换亮色 / 暗色主题" },
      { keys: ["1", "2", "3"], desc: "切换 分析 / 数据 / 产物 三个 Tab" },
    ],
  },
];

// 命令面板可执行的动作。每个动作的 run 接收 App 上下文所需的回调。
// 这里只声明静态元数据，动态回调通过 props 注入。
const COMMAND_ACTIONS = [
  { id: "new-analysis", icon: FilePlus2, title: "新建分析", subtitle: "上传新的数据集", section: "操作" },
  { id: "toggle-theme", icon: Moon, title: "切换主题", subtitle: "亮色 ↔ 暗色", section: "操作" },
  { id: "open-settings", icon: Settings2, title: "打开模型设置", subtitle: "API Key、推理强度", section: "操作" },
  { id: "tab-analysis", icon: BarChart3, title: "切换到分析视图", subtitle: "报告与对话", section: "导航" },
  { id: "tab-data", icon: Table2, title: "切换到数据视图", subtitle: "原始记录预览", section: "导航" },
  { id: "tab-artifacts", icon: FileSpreadsheet, title: "切换到产物视图", subtitle: "图表与导出文件", section: "导航" },
  { id: "show-help", icon: Keyboard, title: "查看键盘快捷键", subtitle: "全部快捷键列表", section: "帮助" },
];

// 主题 hook：管理 light/dark 切换，首次进入时读取 localStorage，若无则跟随系统。
// 通过 document.documentElement.dataset.theme 设置 CSS 变量覆盖范围。
// 跟随系统模式下（localStorage 无值），监听 prefers-color-scheme 变化实时切换，
// 让 macOS 自动深色模式等场景能即时响应。用户手动切换后写入 localStorage，
// 不再跟随系统。
function useTheme() {
  const [theme, setTheme] = useState(() => {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  // 跟随系统模式：localStorage 被清除后，监听系统主题变化
  useEffect(() => {
    const mediaQuery = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mediaQuery) return;
    const handler = (e) => {
      // 仅在用户未手动选择（localStorage 无值）时跟随系统
      if (!window.localStorage.getItem(THEME_KEY)) {
        setTheme(e.matches ? "dark" : "light");
      }
    };
    mediaQuery.addEventListener?.("change", handler);
    return () => mediaQuery.removeEventListener?.("change", handler);
  }, []);
  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  return { theme, toggle, setTheme };
}

// 把任意字符串尝试格式化为缩进 JSON；若不是合法 JSON 则原样返回。
// 用于工具调用 input_preview / output_preview 的展示：很多 LangChain 工具
// 的输入输出本身就是 JSON 字符串，缩进后可读性大幅提升。
function tryFormatJson(text) {
  if (!text) return "";
  const trimmed = String(text).trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return text;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return text;
  }
}

// 简单的 token 用量格式化：< 1000 显示原数，≥ 1000 显示 1.0k 形式
function formatTokens(n) {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

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

// Module-level constant: remarkPlugins array is recreated on every ReportView
// render if declared inline, which forces ReactMarkdown to re-process the
// markdown AST even when the content hasn't changed. Hoisting it to module
// scope keeps the array identity stable across renders.
const REMARK_PLUGINS = [remarkGfm];

// Maximum upload size hint for client-side validation. The server enforces the
// real limit (max_upload_bytes); this mirror lets us fail fast in the browser
// instead of uploading 100MB before getting a 422.
const MAX_UPLOAD_BYTES_CLIENT = 100 * 1024 * 1024;

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

// 把秒数格式化为细颗粒时长：
//   < 60s    → "23 秒"   （直观，避免 "0:23" 显得突兀）
//   < 1h     → "12:34"   （分秒，业界通用格式）
//   ≥ 1h     → "1:23:45" （时:分:秒）
// 参考了 GitHub Actions / Vercel deployment / Linear cycle 的显示风格。
function formatDuration(seconds) {
  if (seconds == null || seconds < 0 || !Number.isFinite(seconds)) return "";
  const total = Math.floor(seconds);
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

// 把时间戳（秒）格式化为相对时间（"3 分钟前"），用于历史会话列表。
function formatRelativeTime(timestamp) {
  if (!timestamp || !Number.isFinite(timestamp)) return "";
  const now = Date.now() / 1000;
  const diff = Math.max(0, now - timestamp);
  if (!Number.isFinite(diff)) return "";
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  // 超过一周显示具体日期。
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

// 把会话按 created_at 分组：今天 / 昨天 / 本周 / 更早。
// 参考 Linear / Notion / VSCode 的历史列表分组惯例——人类记不住具体时间，
// 但能记住"昨天那次分析"，分组让用户快速定位。
function groupSessionsByTime(sessions) {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const startOfYesterday = startOfToday - 86400;
  // 本周从周一开始（中国惯例），getDay() 周日是 0 要转成 7。
  const dayOfWeek = now.getDay() === 0 ? 7 : now.getDay();
  const startOfWeek = startOfToday - (dayOfWeek - 1) * 86400;
  const groups = { today: [], yesterday: [], thisWeek: [], earlier: [] };
  for (const item of sessions || []) {
    const ts = item.created_at || 0;
    if (ts >= startOfToday) groups.today.push(item);
    else if (ts >= startOfYesterday) groups.yesterday.push(item);
    else if (ts >= startOfWeek) groups.thisWeek.push(item);
    else groups.earlier.push(item);
  }
  return [
    { label: "今天", items: groups.today },
    { label: "昨天", items: groups.yesterday },
    { label: "本周", items: groups.thisWeek },
    { label: "更早", items: groups.earlier },
  ].filter((group) => group.items.length > 0);
}

// 历史会话状态描述：圆点 class + 中文标签，供 list item 渲染。
function describeHistoryStatus(status) {
  switch (status) {
    case "completed":
      return { dot: "is-done", label: "已完成" };
    case "running":
      return { dot: "is-running", label: "运行中" };
    case "cancelling":
      return { dot: "is-cancelling", label: "取消中" };
    case "cancelled":
      return { dot: "is-cancelled", label: "已取消" };
    case "failed":
      return { dot: "is-failed", label: "失败" };
    default:
      return { dot: "is-idle", label: "未运行" };
  }
}

function requestHeaders(headers = {}) {
  const token = window.localStorage.getItem(ACCESS_TOKEN_KEY);
  return {
    ...headers,
    ...(token ? { "X-App-Token": token } : {}),
  };
}

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null, resetKey: 0 };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("工作台渲染失败：", error, info);
  }

  reset = () => {
    // 递增 resetKey 强制子树重挂，清掉可能导致再次抛错的内部 state。
    this.setState((prev) => ({ error: null, resetKey: prev.resetKey + 1 }));
  };

  render() {
    if (this.state.error) {
      return (
        <main className="auth-gate">
          <div className="auth-card">
            <span className="section-kicker">DATA DESK</span>
            <h1>渲染出现异常</h1>
            <p>{String(this.state.error?.message || this.state.error || "未知错误")}</p>
            <button className="primary" type="button" onClick={() => window.location.reload()}>
              刷新页面
            </button>
            <button type="button" onClick={this.reset} style={{ marginTop: 8 }}>
              尝试恢复
            </button>
          </div>
        </main>
      );
    }
    return <div key={this.state.resetKey}>{this.props.children}</div>;
  }
}

const presets = [
  {
    title: "完整分析",
    detail: "质量、统计与图表",
    icon: FileCheck2,
    task: "对当前数据执行完整分析：检查数据质量，采用保守策略完成必要清洗，进行描述统计和关键关系分析，创建最有解释力的图表，并导出清洗后的数据。",
  },
  {
    title: "关键驱动",
    detail: "相关与回归诊断",
    icon: Network,
    task: "识别核心数值指标之间的关系和潜在驱动因素，完成必要清洗、相关分析和适用的回归分析，并生成关系图表。",
  },
  {
    title: "异常诊断",
    detail: "缺失、离群与分布",
    icon: Activity,
    task: "诊断缺失、重复和异常值，分析主要数值字段的分布与离群点，采用谨慎的清洗方式并创建分布图和箱线图。",
  },
];

function describeApiError(payload, status) {
  if (payload == null || payload === "") return `请求失败 (${status})`;
  if (typeof payload === "string") {
    // 服务端返回整页 HTML 时截断到 200 字符，避免错误消息变成一长坨标签。
    const trimmed = payload.length > 200 ? `${payload.slice(0, 200)}…` : payload;
    return trimmed;
  }
  if (typeof payload === "object") {
    if (typeof payload.detail === "string" && payload.detail) return payload.detail;
    // FastAPI HTTPException 默认 {detail: ...}，但也可能嵌套其他字段。
    const fallback = payload.message || payload.error;
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
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function api(path, options = {}) {
  const { timeoutMs = 30000, signal: providedSignal, ...fetchOptions } = options;
  const controller = providedSignal ? null : new AbortController();
  // 标记是否为内部超时触发的 abort，用于区分用户主动取消（providedSignal.aborted）
  let timedOut = false;
  const timeout = controller ? window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs) : null;
  try {
    const response = await fetch(`${API_URL}${path}`, {
      ...fetchOptions,
      headers: requestHeaders(fetchOptions.headers),
      signal: providedSignal || controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) throw new ApiError(describeApiError(payload, response.status), response.status);
    return payload;
  } catch (error) {
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

function AccessGate({ onAuthenticated }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function submit(event) {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setMessage("");
    window.localStorage.setItem(ACCESS_TOKEN_KEY, token.trim());
    try {
      const status = await api("/api/auth");
      if (!status.authenticated) throw new Error("访问令牌无效。");
      onAuthenticated();
    } catch (error) {
      window.localStorage.removeItem(ACCESS_TOKEN_KEY);
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-gate">
      <form className="auth-card" onSubmit={submit}>
        <span className="section-kicker">DATA DESK</span>
        <h1>进入数据工作台</h1>
        <p>请输入部署管理员提供的访问令牌。</p>
        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="应用访问令牌"
          autoFocus
        />
        <button className="primary" type="submit" disabled={busy || !token.trim()}>
          {busy ? "验证中" : "进入工作台"}
        </button>
        {message && <small className="auth-error">{message}</small>}
      </form>
    </main>
  );
}

function Metric({ label, value, unit }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}<small>{unit}</small></strong>
    </div>
  );
}

// 报告区独立组件：负责渲染最终 Markdown 报告，并附带时间戳、复制按钮、
// 长 report-body 展开/收起。把这块从主组件拆出来也让 props 校验更清晰。
// React.memo：App 在用户输入 task、刷新历史等场景下会重渲染，但 result
// 通常不变。memo 让 ReportView 跳过这些无关重渲染，避免 ReactMarkdown
// 重新解析 markdown AST（report 可能长达数千字）。
const ReportView = React.memo(function ReportView({ result, streaming, onPreview, artifacts, reasoning, reasoningStreaming, theme, usage }) {
  // copyState: "idle" | "copied" | "failed"。之前只有 copied boolean，
  // 复制失败时静默吞掉错误，用户切到其他应用粘贴才发现是旧内容。
  const [copyState, setCopyState] = useState("idle");
  const [expanded, setExpanded] = useState(false);
  const reportBodyRef = useRef(null);
  // useDeferredValue: 流式追加时 ReactMarkdown 重解析整个 AST 会卡顿，
  // defer 让高优先级更新（输入框交互）先走，Markdown 渲染延后。
  const deferredResponse = useDeferredValue(result.response || "");
  const deferredReasoning = useDeferredValue(reasoning || "");

  // useMemo 缓存 markdownComponents：否则每次渲染都返回新对象，ReactMarkdown
  // 会因 components prop 引用变化而全量重解析 AST，流式时每个 chunk 都重解析。
  const mdComponents = useMemo(
    () => markdownComponents(artifacts, onPreview, theme),
    [artifacts, onPreview, theme]
  );

  // 流式时自动滚动到底部，让用户看到最新生成的文字
  useEffect(() => {
    if (streaming && reportBodyRef.current) {
      reportBodyRef.current.scrollTop = reportBodyRef.current.scrollHeight;
    }
  }, [deferredResponse, streaming]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(result.response || "");
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      // HTTP 部署、iframe 受限或浏览器禁用 clipboard 时明确告知用户，
      // 让用户知道需要手动选择文本复制，而不是以为复制成功了。
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 3000);
    }
  };

  return (
    <article className="report">
      <div className="report-meta">
        <div className="report-title">
          <FileChartColumn size={15} />
          <span>分析报告</span>
          {streaming ? (
            <small className="report-count is-streaming"><LoaderCircle size={11} className="spin" />生成中</small>
          ) : (
            <small className="report-count">{result.artifacts?.length || 0} 个产物</small>
          )}
        </div>
        <div className="report-actions">
          {!streaming && <UsageChip usage={usage} />}
          <button
            type="button"
            className={`report-copy ${copyState === "failed" ? "is-failed" : ""}`}
            onClick={handleCopy}
            title="复制全文"
            aria-label="复制报告全文"
            disabled={streaming}
          >
            {copyState === "copied" ? <Check size={13} /> : <FileSpreadsheet size={13} />}
            {copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制"}
          </button>
        </div>
      </div>
      <ReasoningBlock content={deferredReasoning} streaming={reasoningStreaming} />
      <div className={`report-body ${expanded ? "is-expanded" : ""} ${streaming ? "is-streaming" : ""}`} ref={reportBodyRef}>
        {streaming && !deferredResponse ? (
          <div className="report-placeholder"><LoaderCircle size={14} className="spin" />正在生成报告…</div>
        ) : (
          <>
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={mdComponents}>
              {deferredResponse}
            </ReactMarkdown>
            {streaming && <span className="report-cursor" aria-hidden="true" />}
          </>
        )}
      </div>
      {!streaming && (
        <button
          type="button"
          className="report-toggle"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
        >
          <ChevronDown size={13} className={expanded ? "rot-180" : ""} />
          {expanded ? "收起报告" : "展开完整报告"}
        </button>
      )}
    </article>
  );
});

// 代码块组件：Prism 语法高亮 + 一键复制 + 语言标签。
// 替代 ReactMarkdown 默认的 <pre><code> 渲染，让报告中的 SQL/Python/JSON
// 代码块具备 IDE 级别的可读性。主题随当前 theme 切换 oneLight/oneDark。
const CodeBlock = React.memo(function CodeBlock({ language, value, theme }) {
  const [copyState, setCopyState] = useState("idle");
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value || "");
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 3000);
    }
  };
  const lang = (language || "").toLowerCase() || "text";
  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-block-lang">{lang}</span>
        <button
          type="button"
          className={`code-block-copy ${copyState === "copied" ? "is-copied" : ""}`}
          onClick={handleCopy}
          aria-label="复制代码"
        >
          {copyState === "copied" ? <Check size={11} /> : <FileSpreadsheet size={11} />}
          {copyState === "copied" ? "已复制" : copyState === "failed" ? "失败" : "复制"}
        </button>
      </div>
      <SyntaxHighlighter
        language={lang}
        style={theme === "dark" ? oneDark : oneLight}
        customStyle={{ margin: 0, padding: "12px 14px", background: "transparent", fontSize: "12.5px" }}
        codeTagProps={{ style: { fontFamily: "var(--font-mono)" } }}
        wrapLongLines
      >
        {value || ""}
      </SyntaxHighlighter>
    </div>
  );
});

// 工具调用展开/折叠项：点击行展开 input_preview / output_preview JSON。
// 之前 toolTrace 已经携带这两段数据但 UI 没渲染，是"半成品"。
// 这里补上交互，让用户能像 Claude/ChatGPT 那样点开看工具实际做了什么。
const ToolTraceItem = React.memo(function ToolTraceItem({ tool, defaultExpanded = false }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const hasDetail = !!(tool.input_preview || tool.output_preview);
  return (
    <li className={`tool-trace-item ${expanded ? "is-expanded" : ""}`}>
      <div
        className="tool-trace-row"
        onClick={() => hasDetail && setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (hasDetail && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        role={hasDetail ? "button" : undefined}
        tabIndex={hasDetail ? 0 : undefined}
        aria-expanded={hasDetail ? expanded : undefined}
      >
        {hasDetail ? <ChevronRight size={11} className="tool-chevron" /> : <span style={{ width: 11 }} />}
        <span className="tool-dot" aria-hidden="true" />
        <span className="tool-name">{TOOL_LABELS[tool.name] || tool.name}</span>
        {tool.status === "running" ? (
          <LoaderCircle size={10} className="spin" />
        ) : (
          <span className="tool-duration">{tool.duration_ms ? `${tool.duration_ms}ms` : ""}</span>
        )}
      </div>
      {expanded && hasDetail && (
        <div className="tool-trace-detail">
          {tool.input_preview && (
            <div className="tool-trace-detail-section">
              <span className="tool-trace-detail-label">输入</span>
              <pre>{tryFormatJson(tool.input_preview)}</pre>
            </div>
          )}
          {tool.output_preview && (
            <div className="tool-trace-detail-section">
              <span className="tool-trace-detail-label">输出</span>
              <pre>{tryFormatJson(tool.output_preview)}</pre>
            </div>
          )}
        </div>
      )}
    </li>
  );
});

// 命令面板（Cmd+K）：参考 Linear / Raycast / VSCode 的命令面板体验。
// 输入框 + 动作列表 + 会话搜索结果。键盘上下选择，Enter 执行，Esc 关闭。
const CommandPalette = React.memo(function CommandPalette({
  query, onQueryChange, actions, sessions, onAction, onSelectSession, onClose, theme,
}) {
  const inputRef = useRef(null);
  const [activeIndex, setActiveIndex] = useState(0);
  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => { setActiveIndex(0); }, [query]);

  const q = query.trim().toLowerCase();
  const filteredActions = !q ? actions : actions.filter((a) =>
    a.title.toLowerCase().includes(q) || a.subtitle.toLowerCase().includes(q));
  const filteredSessions = !q ? [] : (sessions || []).filter((s) =>
    (s.filename || "").toLowerCase().includes(q)).slice(0, 5);

  const flat = [
    ...filteredActions.map((a) => ({ type: "action", value: a })),
    ...filteredSessions.map((s) => ({ type: "session", value: s })),
  ];
  const total = flat.length;

  const handleKeyDown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActiveIndex((i) => (i + 1) % Math.max(total, 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIndex((i) => (i - 1 + Math.max(total, 1)) % Math.max(total, 1)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const item = flat[activeIndex];
      if (!item) return;
      if (item.type === "action") onAction(item.value);
      else onSelectSession(item.value);
    } else if (e.key === "Escape") { e.preventDefault(); onClose(); }
  };

  // 按 section 分组动作
  const actionGroups = useMemo(() => {
    const map = new Map();
    for (const a of filteredActions) {
      if (!map.has(a.section)) map.set(a.section, []);
      map.get(a.section).push(a);
    }
    return Array.from(map.entries());
  }, [filteredActions]);

  return (
    <div className="command-palette-backdrop" role="dialog" aria-modal="true" aria-label="命令面板" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="command-palette">
        <div className="command-input-row">
          <Search size={16} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="搜索操作或历史会话…"
          />
          <kbd className="command-item-kbd">ESC</kbd>
        </div>
        <div className="command-list">
          {total === 0 && <div className="command-empty">没有匹配项</div>}
          {actionGroups.map(([section, items]) => (
            <div key={section}>
              <div className="command-section-label">{section}</div>
              {items.map((a) => {
                const idx = flat.findIndex((f) => f.type === "action" && f.value.id === a.id);
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`command-item ${idx === activeIndex ? "is-active" : ""}`}
                    onMouseEnter={() => setActiveIndex(idx)}
                    onClick={() => onAction(a)}
                  >
                    <a.icon size={16} className="command-item-icon" />
                    <span className="command-item-text">
                      <strong>{a.title}</strong>
                      <small>{a.subtitle}</small>
                    </span>
                  </button>
                );
              })}
            </div>
          ))}
          {filteredSessions.length > 0 && (
            <div>
              <div className="command-section-label">历史会话</div>
              {filteredSessions.map((s) => {
                const idx = flat.findIndex((f) => f.type === "session" && f.value.id === s.id);
                return (
                  <button
                    key={s.id}
                    type="button"
                    className={`command-item ${idx === activeIndex ? "is-active" : ""}`}
                    onMouseEnter={() => setActiveIndex(idx)}
                    onClick={() => onSelectSession(s)}
                  >
                    <FileSpreadsheet size={16} className="command-item-icon" />
                    <span className="command-item-text">
                      <strong>{s.filename}</strong>
                      <small>{formatRelativeTime(s.created_at)} · {describeHistoryStatus(s.analysis_status).label}</small>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <div className="command-footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>Enter</kbd> 执行</span>
          <span><kbd>Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>
  );
});

// 快捷键帮助面板（? 唤起）：完整列出所有可用快捷键。
// 集中在 HELP_SHORTCUTS 常量维护，避免文档与实现脱节。
const HelpPanel = React.memo(function HelpPanel({ onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="help-panel-backdrop" role="dialog" aria-modal="true" aria-label="键盘快捷键" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="help-panel">
        <div className="help-panel-header">
          <h2>键盘快捷键</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X size={16} /></button>
        </div>
        <div className="help-panel-body">
          {HELP_SHORTCUTS.map((group) => (
            <div key={group.section} className="help-section">
              <h3 className="help-section-title">{group.section}</h3>
              <div className="help-shortcut-list">
                {group.items.map((item, idx) => (
                  <div key={idx} className="help-shortcut">
                    <span>{item.desc}</span>
                    <span className="help-shortcut-keys">
                      {item.keys.map((k, i) => <kbd key={i}>{k}</kbd>)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
});

// 思考过程展示：可折叠的推理区域，把 DeepSeek 的 reasoning_content 实时
// 流式展示。默认折叠，避免推理内容过长挤占正文；流式时显示光标。
const ReasoningBlock = React.memo(function ReasoningBlock({ content, streaming, expanded: expandedProp }) {
  const [expanded, setExpanded] = useState(false);
  // 流式期间自动展开，让用户看到模型在想什么；结束后自动收起
  useEffect(() => {
    if (streaming) setExpanded(true);
  }, [streaming]);
  if (!content && !streaming) return null;
  return (
    <div className={`reasoning-block ${expanded ? "is-expanded" : ""}`}>
      <button
        type="button"
        className="reasoning-toggle"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <ChevronRight size={11} className="reasoning-chevron" />
        <Brain size={11} />
        <span>{streaming ? "正在思考…" : "思考过程"}</span>
        {content && <em style={{ marginLeft: "auto", color: "var(--fg-subtle)", fontWeight: 400 }}>{content.length} 字</em>}
      </button>
      {expanded && (
        <div className={`reasoning-body ${streaming ? "is-streaming" : ""}`}>
          {content || (streaming ? "…" : "")}
          {streaming && <span className="reasoning-cursor" aria-hidden="true" />}
        </div>
      )}
    </div>
  );
});

// Token 用量 chip：在报告头部和追问气泡末尾展示本次回答的 token 用量。
// 主流 Agent（ChatGPT/Claude）都展示这个指标，让用户感知模型消耗。
const UsageChip = React.memo(function UsageChip({ usage }) {
  if (!usage || (!usage.prompt_tokens && !usage.completion_tokens && !usage.total_tokens)) return null;
  const total = usage.total_tokens || ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
  return (
    <span className="usage-chip" title="本次 LLM 调用的 token 用量">
      <Clock size={11} />
      {usage.prompt_tokens > 0 && <>
        <span>输入 <strong>{formatTokens(usage.prompt_tokens)}</strong></span>
        <span className="usage-sep">·</span>
      </>}
      {usage.completion_tokens > 0 && <>
        <span>输出 <strong>{formatTokens(usage.completion_tokens)}</strong></span>
        <span className="usage-sep">·</span>
      </>}
      <span>共 <strong>{formatTokens(total)}</strong></span>
    </span>
  );
});

// ReactMarkdown 自定义 components：识别 ![描述](artifact:图表文件名) 语法，
// 把图表占位符渲染为可点击的内嵌图表卡片，点击在模态框打开交互版。
// 让图表直接嵌在报告正文中图文混排，而不是只能切到产物 tab 查看。
// code 组件用 CodeBlock 渲染（语法高亮 + 复制按钮）。
function markdownComponents(artifacts, onPreview, theme) {
  return {
    img: ({ src, alt }) => {
      if (typeof src === "string" && src.startsWith("artifact:")) {
        const name = src.slice("artifact:".length);
        const artifact = artifacts?.find((a) => a.name === name);
        if (artifact) {
          const { Icon, label } = pickChartIcon(artifact.name);
          return (
            <button type="button" className="embedded-chart" onClick={() => onPreview?.(artifact)}>
              <Icon size={18} />
              <span>{alt || label || artifact.description || artifact.name}</span>
              <ExternalLink size={12} />
            </button>
          );
        }
        return <em className="embedded-chart-missing">图表 {name} 已丢失</em>;
      }
      return <img src={src} alt={alt} />;
    },
    code: ({ inline, className, children, ...props }) => {
      if (inline) {
        return <code className="inline-code" {...props}>{children}</code>;
      }
      // 从 className "language-xxx" 中提取语言
      const match = /language-(\w+)/.exec(className || "");
      const language = match ? match[1] : "";
      const value = String(children || "").replace(/\n$/, "");
      return <CodeBlock language={language} value={value} theme={theme} />;
    },
    pre: ({ children }) => <>{children}</>,
  };
}

const PlanPanel = React.memo(function PlanPanel({ plan, completed, running, currentNodeTitle, elapsedSeconds, toolTrace }) {
  const completedIds = new Set((completed || []).map((item) => item.id));
  const doneCount = plan.filter((item) => completedIds.has(item.id)).length;
  // 显示耗时：运行中显示"已耗时"，完成时显示"总耗时"。
  // 当 elapsedSeconds 为 null（如未运行且没有完成记录）时不显示。
  const hasTiming = elapsedSeconds != null && elapsedSeconds >= 0;
  const elapsedLabel = running ? "已耗时" : plan.length ? "总耗时" : "";
  const isCompleted = !running && plan.length > 0;
  // 工具调用时间线：显示最近 8 条，让用户看到 ReAct 内部正在做什么
  const recentTools = (toolTrace || []).slice(-8);

  return (
    <aside className="plan-panel" aria-label="执行记录">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">执行记录</span>
          <h2>分析进度</h2>
        </div>
        <div className="panel-meta">
          {hasTiming && elapsedLabel && (
            <span
              className={`elapsed-chip ${running ? "is-running" : isCompleted ? "is-done" : ""}`}
              title={running ? "本次分析已运行时长" : "本次分析总耗时"}
            >
              <Clock size={11} />
              <span className="elapsed-label">{elapsedLabel}</span>
              <span className="elapsed-value">{formatDuration(elapsedSeconds)}</span>
            </span>
          )}
          <span className={`run-state ${running ? "is-running" : isCompleted ? "is-done" : ""}`}>
            {running ? (
              <span className="status-dot" aria-hidden="true" />
            ) : isCompleted ? (
              <Check size={11} />
            ) : (
              <Circle size={7} />
            )}
            {running ? "运行中" : isCompleted ? "已完成" : "待开始"}
          </span>
        </div>
      </div>

      {running && currentNodeTitle && (
        <div className="current-node" role="status" aria-live="polite">
          <LoaderCircle size={12} className="spin" />
          <span>{currentNodeTitle}</span>
        </div>
      )}

      {plan.length > 0 && (
        <div className="progress-line" aria-label={`已完成 ${doneCount}/${plan.length}`}>
          <span style={{ width: `${(doneCount / plan.length) * 100}%` }} />
        </div>
      )}

      {!plan.length ? (
        <div className="plan-empty">
          <Rows3 size={18} />
          <p>运行任务后，这里会显示规划和执行状态。</p>
        </div>
      ) : (
        <ol className="plan-list">
          {plan.map((step, index) => {
            const done = completedIds.has(step.id);
            const active = running && !done && plan.slice(0, index).every((item) => completedIds.has(item.id));
            return (
              <li key={`${step.id}-${index}`} className={done ? "done" : active ? "active" : ""}>
                <span className="step-mark">{done ? <Check size={13} /> : index + 1}</span>
                <div>
                  <strong>{step.title}</strong>
                  <p>{step.success_criteria}</p>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {/* 工具调用时间线：实时展示 ReAct 内部工具调用，让用户看到"正在读取数据
          → 正在清洗 → 正在生成图表"的过程，而不是只看到"正在执行 (2/4)"等 30 秒 */}
      {recentTools.length > 0 && (
        <div className="tool-trace" aria-label="工具调用时间线">
          <div className="tool-trace-label">
            <Activity size={12} />
            <span>工具调用</span>
            <small>{recentTools.length}</small>
          </div>
          <ul className="tool-trace-list">
            {recentTools.map((tool) => (
              <ToolTraceItem key={tool.call_id} tool={tool} />
            ))}
          </ul>
        </div>
      )}

      <div className="architecture-note">
        <Network size={14} />
        <span>Plan &amp; Execute</span>
        <i />
        <span>ReAct</span>
      </div>
    </aside>
  );
});

// 多轮对话线程：在主报告下方展示追问历史 + 追问输入框。
// 设计参考 ChatGPT / Claude / Linear 的对话流：
//   - 用户气泡右对齐 + 主色底，assistant 气泡左对齐 + 卡片底
//   - assistant 气泡内嵌 Markdown 渲染 + 工具调用 mini 时间线
//   - 流式时显示闪烁光标，让用户知道回答正在写出
//   - 底部固定追问输入框，支持 Ctrl+Enter 提交
const ConversationThread = React.memo(function ConversationThread({
  messages, input, onInputChange, onSubmit, onStop, running, disabled, onPreview, artifacts,
  onEditMessage, theme, inputRef,
}) {
  const listRef = useRef(null);
  const deferredLastContent = useDeferredValue(
    messages.length ? messages[messages.length - 1].content || "" : ""
  );

  // 流式时自动滚动到底部，让用户看到最新生成的文字
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [deferredLastContent, messages.length, running]);

  const handleKeyDown = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      if (!running && input.trim() && !disabled) onSubmit();
    }
  };

  return (
    <section className="conversation-thread" aria-label="追问对话">
      <div className="conversation-header">
        <div>
          <span className="section-kicker">继续对话</span>
          <h2>追问与补充分析</h2>
        </div>
        <small>基于当前数据集直接回答，无需重跑完整流程</small>
      </div>
      {messages.length > 0 && (
        <div className="conversation-list" ref={listRef}>
          {messages.map((msg, index) => (
            <ConversationBubble
              key={index}
              message={msg}
              index={index}
              onPreview={onPreview}
              artifacts={artifacts}
              onEditMessage={onEditMessage}
              theme={theme}
              canEdit={!running && !disabled}
            />
          ))}
        </div>
      )}
      <div className="follow-up-composer">
        <textarea
          ref={inputRef}
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "请先配置 API Key 后再追问" : "追问任何关于数据的问题，如：把刚才那张图改成红色 / 解释这个 p 值（⌘/Ctrl+Enter 发送）"}
          rows={2}
          disabled={disabled}
        />
        <div className="follow-up-actions">
          {running ? (
            <button className="cancel-button" type="button" onClick={onStop} disabled={false}>
              <Square size={12} fill="currentColor" />停止
            </button>
          ) : (
            <button
              className="run-button"
              type="button"
              onClick={onSubmit}
              disabled={!input.trim() || disabled}
            >
              <Play size={14} fill="currentColor" />发送追问
            </button>
          )}
        </div>
      </div>
    </section>
  );
});

// 单条对话气泡：
//   - user 气泡支持 hover 显示编辑按钮，点击进入编辑模式，保存后调用 onEditMessage 截断重发
//   - assistant 气泡支持 reasoning（思考过程）展示、工具调用展开、usage chip、流式光标
const ConversationBubble = React.memo(function ConversationBubble({
  message, index, onPreview, artifacts, onEditMessage, theme, canEdit,
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content || "");

  // 进入编辑模式时同步 draft
  useEffect(() => {
    if (editing) setDraft(message.content || "");
  }, [editing, message.content]);

  // useMemo 缓存 markdownComponents，避免每次渲染重建对象导致 ReactMarkdown 重解析
  const mdComponents = useMemo(
    () => markdownComponents(artifacts, onPreview, theme),
    [artifacts, onPreview, theme]
  );

  if (message.role === "user") {
    return (
      <div className="chat-bubble is-user">
        {editing ? (
          <div>
            <textarea
              className="chat-bubble-edit-area"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              autoFocus
              rows={Math.min(8, Math.max(2, draft.split("\n").length))}
            />
            <div className="chat-bubble-edit-actions">
              <button type="button" className="btn-save" onClick={() => {
                if (draft.trim() && draft.trim() !== (message.content || "").trim()) {
                  onEditMessage?.(index, draft.trim());
                }
                setEditing(false);
              }}>
                <CornerDownLeft size={11} />保存并重发
              </button>
              <button type="button" className="btn-cancel" onClick={() => setEditing(false)}>取消</button>
            </div>
          </div>
        ) : (
          <>
            <div className="chat-bubble-content">{message.content}</div>
            {canEdit && onEditMessage && (
              <div className="chat-bubble-actions">
                <button
                  type="button"
                  className="chat-bubble-action-btn"
                  title="编辑后重新发送（会清除后续对话）"
                  onClick={() => setEditing(true)}
                  aria-label="编辑消息"
                >
                  <RefreshCw size={11} />
                </button>
              </div>
            )}
          </>
        )}
      </div>
    );
  }
  // assistant 气泡：reasoning + Markdown 渲染 + 工具时间线 + 流式光标 + usage
  const isStreaming = message.streaming;
  const hasContent = !!message.content;
  const hasReasoning = !!message.reasoning;
  return (
    <div className="chat-bubble is-assistant">
      {(hasReasoning || (isStreaming && !hasContent)) && (
        <ReasoningBlock content={message.reasoning || ""} streaming={isStreaming && !hasContent} />
      )}
      {message.tools?.length > 0 && (
        <div className="chat-tools" aria-label="本次追问工具调用">
          {message.tools.map((tool) => (
            <ChatToolChip key={tool.call_id} tool={tool} />
          ))}
        </div>
      )}
      <div className={`chat-bubble-content ${isStreaming ? "is-streaming" : ""}`}>
        {isStreaming && !hasContent && !hasReasoning ? (
          <div className="thinking-placeholder">
            <span className="thinking-dots"><span /><span /><span /></span>
            正在思考…
          </div>
        ) : hasContent ? (
          <>
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={mdComponents}>
              {message.content || ""}
            </ReactMarkdown>
            {isStreaming && <span className="report-cursor" aria-hidden="true" />}
          </>
        ) : null}
      </div>
      {!isStreaming && message.usage && <UsageChip usage={message.usage} />}
      {message.error && <div className="chat-bubble-error"><AlertTriangle size={12} />{message.error}</div>}
    </div>
  );
});

// 对话气泡内的工具 chip：支持点击展开查看 input/output preview
const ChatToolChip = React.memo(function ChatToolChip({ tool }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = !!(tool.input_preview || tool.output_preview);
  return (
    <span
      className={`chat-tool-chip ${tool.status === "running" ? "is-running" : "is-done"}`}
      onClick={() => hasDetail && setExpanded((v) => !v)}
      onKeyDown={(e) => {
        if (hasDetail && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          setExpanded((v) => !v);
        }
      }}
      role={hasDetail ? "button" : undefined}
      tabIndex={hasDetail ? 0 : undefined}
      aria-expanded={hasDetail ? expanded : undefined}
    >
      <span className="tool-dot" aria-hidden="true" />
      {TOOL_LABELS[tool.name] || tool.name}
      {tool.status === "running" ? (
        <LoaderCircle size={9} className="spin" />
      ) : (
        <em>{tool.duration_ms ? `${tool.duration_ms}ms` : ""}</em>
      )}
      {expanded && hasDetail && (
        <div className="chat-tool-detail" onClick={(e) => e.stopPropagation()}>
          {tool.input_preview && <div><strong>输入</strong><pre>{tryFormatJson(tool.input_preview)}</pre></div>}
          {tool.output_preview && <div><strong>输出</strong><pre>{tryFormatJson(tool.output_preview)}</pre></div>}
        </div>
      )}
    </span>
  );
});

// 历史会话面板：可折叠的侧边栏组件，按时间分组列出最近会话并允许切换。
// 关键设计：
//   1. 时间分组（今天/昨天/本周/更早）—— Linear / Notion / VSCode 都这么做，
//      人类记不住"5 小时前那次分析"，但能记住"今天上午那次"。
//   2. 骨架屏加载（而非"加载中"文字）—— 让用户立即看到列表骨架，
//      避免"什么都没有"的瞬间错愕。
//   3. 状态圆点 + 中文标签 —— running 圆点带脉冲动画，completed 是绿色，
//      failed 是红色，cancelled 是灰色，状态一眼可读。
//   4. 当前会话用左侧竖条 + 浅蓝底高亮，比单纯背景色更醒目。
const HistoryPanel = React.memo(function HistoryPanel({ sessions, currentSessionId, onSelect, onRefresh, loading, expanded, onToggle, historyError, switchingSessionId }) {
  const [searchQuery, setSearchQuery] = useState("");
  // 搜索过滤：按文件名匹配，匹配不到时显示空状态。本地过滤即可，
  // 不需要后端 query 参数——历史列表通常 ≤ 30 条，前端 filter 毫秒级。
  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return sessions || [];
    return (sessions || []).filter((s) => (s.filename || "").toLowerCase().includes(q));
  }, [sessions, searchQuery]);
  const groups = useMemo(() => groupSessionsByTime(filtered), [filtered]);
  const isEmpty = !sessions?.length && !loading;
  const isSearching = searchQuery.trim().length > 0;
  const noResults = isSearching && filtered.length === 0 && !loading;

  return (
    <div className="sidebar-section history-section">
      <button type="button" className="history-toggle" onClick={onToggle} aria-expanded={expanded}>
        <History size={14} />
        <span className="sidebar-label">历史会话</span>
        {sessions?.length > 0 && <em className="history-total">{sessions.length}</em>}
        <ChevronRight size={13} className={expanded ? "rot-90" : ""} />
      </button>
      {expanded && (
        <>
          <button type="button" className="history-refresh" onClick={onRefresh} disabled={loading} title="刷新历史" aria-label="刷新历史会话列表">
            <RefreshCw size={12} className={loading ? "spin" : ""} />
            {loading ? "加载中" : "刷新"}
          </button>
          {sessions?.length > 0 && (
            <div className="history-search">
              <div className="history-search-wrap">
                <Search size={12} />
                <input
                  type="search"
                  className="history-search-input"
                  placeholder="搜索文件名…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  aria-label="搜索历史会话"
                />
              </div>
            </div>
          )}
          {noResults ? (
            <div className="history-search-empty">没有匹配「{searchQuery.trim()}」的会话</div>
          ) : isEmpty ? (
            <div className="history-empty">
              <History size={16} />
              {historyError ? (
                <>
                  <p>历史会话加载失败，请检查网络后重试。</p>
                  <button type="button" className="history-retry" onClick={onRefresh}>重新加载</button>
                </>
              ) : (
                <p>还没有历史会话，上传数据后会自动出现在这里。</p>
              )}
            </div>
          ) : loading && !sessions?.length ? (
            <ul className="history-list history-skeleton" aria-hidden="true">
              {[0, 1, 2].map((index) => (
                <li key={index}>
                  <div className="skeleton-row">
                    <span className="skeleton-icon" />
                    <span className="skeleton-lines">
                      <span className="skeleton-line skeleton-line-wide" />
                      <span className="skeleton-line skeleton-line-narrow" />
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            groups.map((group) => (
              <div key={group.label} className="history-group">
                <span className="history-group-label">{group.label}</span>
                <ul className="history-list">
                  {group.items.map((item) => {
                    const active = item.id === currentSessionId;
                    const status = describeHistoryStatus(item.analysis_status);
                    return (
                      <li key={item.id} className={active ? "is-active" : ""}>
                        <button type="button" onClick={() => onSelect(item)} disabled={switchingSessionId != null}>
                          <FileSpreadsheet size={14} />
                          <span>
                            <strong>{item.filename}</strong>
                            <small>
                              <span className={`history-status-dot ${status.dot}`} aria-hidden="true" />
                              <span className="history-status-text">{status.label}</span>
                              {item.has_result && <span className="history-result">· 有报告</span>}
                              <span className="history-time">· {formatRelativeTime(item.created_at)}</span>
                            </small>
                          </span>
                          {switchingSessionId === item.id ? (
                            <LoaderCircle size={13} className="spin" />
                          ) : item.artifact_count > 0 ? (
                            <em className="history-count">{item.artifact_count}</em>
                          ) : null}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))
          )}
        </>
      )}
    </div>
  );
});

// React.memo：rows 仅在 session 切换时变化，但 App 每次输入 task 或
// 刷新历史都会重渲染。memo 让 DataTable 跳过这些场景，避免重新生成
// 几百个 <td>。
const DataTable = React.memo(function DataTable({ rows }) {
  const columns = useMemo(() => Object.keys(rows?.[0] || {}), [rows]);
  if (!rows?.length) return <div className="empty-row">没有可预览的数据</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th className="row-number">#</th>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td className="row-number">{index + 1}</td>
              {columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

function EmptyWorkspace({ uploading, onUpload }) {
  // 鼠标跟随光斑：跟踪鼠标在 grid 上的相对位置，更新 CSS 变量，
  // 由 styles.css 的 radial-gradient 渲染柔和光晕。参考 Linear/Vercel
  // 空状态的 spotlight 效果——比静态装饰更有"活物感"。
  const gridRef = useRef(null);
  const handleMouseMove = useCallback((event) => {
    const grid = gridRef.current;
    if (!grid) return;
    const rect = grid.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 100;
    const y = ((event.clientY - rect.top) / rect.height) * 100;
    grid.style.setProperty("--mouse-x", `${x}%`);
    grid.style.setProperty("--mouse-y", `${y}%`);
  }, []);

  return (
    <section className="empty-workspace">
      <div
        className="empty-grid"
        aria-hidden="true"
        ref={gridRef}
        onMouseMove={handleMouseMove}
      >
        <span className="grid-tab" />
        {Array.from({ length: 20 }, (_, index) => (
          <i key={index} style={{ "--cell-index": index }} />
        ))}
      </div>
      <div className="empty-copy">
        <span className="section-kicker">新建分析</span>
        <h2>从一份数据开始</h2>
        <p>CSV、Excel、JSON 或 Parquet</p>
        <button className="primary" onClick={onUpload} disabled={uploading}>
          {uploading ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}
          {uploading ? "正在读取" : "选择数据文件"}
        </button>
      </div>
    </section>
  );
}

const DatasetOverview = React.memo(function DatasetOverview({ profile }) {
  const columns = profile?.column_info?.slice(0, 6) || [];
  return (
    <section className="dataset-overview">
      <div className="overview-title">
        <span className="section-kicker">数据概览</span>
        <h2>字段质量</h2>
      </div>
      <div className="column-list">
        {columns.map((column) => (
          <div key={column.name}>
            <span className="field-name"><Database size={13} />{column.name}</span>
            <span>{column.dtype}</span>
            <span className={column.missing ? "has-issue" : ""}>{column.missing ? `${column.missing} 缺失` : "完整"}</span>
          </div>
        ))}
      </div>
    </section>
  );
});

function formatBytes(value = 0) {
  if (!value) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

// 后端工具名 → 中文短标签。PlanPanel 工具调用时间线用它把 inspect_data
// 这种程序化名字翻译成"检查数据"，让用户看懂 ReAct 内部在做什么。
// 缺失映射时回退到原始工具名，保证新工具上线也不会显示 undefined。
const TOOL_LABELS = {
  inspect_data: "检查数据",
  repair_data_format: "修复格式",
  clean_data: "清洗数据",
  transform_data: "派生变换",
  statistical_analysis: "统计分析",
  create_visualization: "生成图表",
  export_data: "导出数据",
};

// React.memo：artifacts 仅在 session 切换或分析完成时变化。memo 让产物
// 列表跳过 task 输入、历史刷新等无关重渲染。onDownload/onPreview 用
// useCallback 稳定身份，否则 memo 失效。
// 根据图表文件名前缀推断图表类型，选择对应的有意义图标。
// 后端 _chart_filename_stem 用中文标签命名（如 "柱状图_1.html"），
// 前端据此匹配 lucide 图标，让用户一眼看出图表类型。
const CHART_ICON_BY_PREFIX = [
  { prefix: "柱状图", Icon: BarChart3, label: "柱状图" },
  { prefix: "折线图", Icon: LineChart, label: "折线图" },
  { prefix: "面积图", Icon: LineChart, label: "面积图" },
  { prefix: "散点矩阵", Icon: Grid3x3, label: "散点矩阵" },
  { prefix: "散点图", Icon: ScatterChart, label: "散点图" },
  { prefix: "三维散点", Icon: ScatterChart, label: "三维散点" },
  { prefix: "直方图", Icon: Activity, label: "直方图" },
  { prefix: "箱线图", Icon: Boxes, label: "箱线图" },
  { prefix: "小提琴图", Icon: Boxes, label: "小提琴图" },
  { prefix: "饼图", Icon: PieChart, label: "饼图" },
  { prefix: "相关性热力图", Icon: Grid3x3, label: "相关性热力图" },
  { prefix: "热力图", Icon: Grid3x3, label: "热力图" },
  { prefix: "旭日图", Icon: Network, label: "旭日图" },
  { prefix: "矩形树图", Icon: Network, label: "矩形树图" },
];

function pickChartIcon(name = "") {
  for (const entry of CHART_ICON_BY_PREFIX) {
    if (name.startsWith(entry.prefix)) return entry;
  }
  return { prefix: "", Icon: FileChartColumn, label: "图表" };
}

const ArtifactCenter = React.memo(function ArtifactCenter({ artifacts = [], onDownload, onPreview }) {
  const charts = artifacts.filter((item) => item.kind === "visualization");
  const files = artifacts.filter((item) => item.kind !== "visualization");
  if (!artifacts.length) return <div className="empty-row">分析完成后，最终图表和数据文件会出现在这里。</div>;
  return (
    <div className="artifact-center">
      {charts.length > 0 && (
        <section className="artifact-section">
          <div className="artifact-section-label"><span>交互图表</span><small>{charts.length} 张精选结果</small></div>
          <div className="chart-grid">
            {charts.map((item, index) => {
              const { Icon, label } = pickChartIcon(item.name);
              return (
                <article className="chart-card" key={item.name}>
                  <div className="chart-index">{String(index + 1).padStart(2, "0")}</div>
                  <Icon size={20} />
                  <div>
                    <strong>{item.description || item.name}</strong>
                    <small>{label} · {formatBytes(item.size_bytes)} · 点击查看交互</small>
                  </div>
                  <div className="artifact-actions">
                    <button className="preview-button" onClick={() => onPreview(item)}>
                      <ExternalLink size={14} />在线查看
                    </button>
                    <button className="icon-button" title={`下载 ${item.name}`} onClick={() => onDownload(item)}>
                      <Download size={15} />
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}
      {files.length > 0 && (
        <section className="artifact-section artifact-files">
          <div className="artifact-section-label"><span>数据文件</span><small>仅保留最终版本</small></div>
          <div className="artifact-list">
            {files.map((item) => (
              <div key={item.name}>
                <FileSpreadsheet size={17} />
                <span><strong>{item.name}</strong><small>{item.description} {formatBytes(item.size_bytes)}</small></span>
                <button className="artifact-download" title={`下载 ${item.name}`} onClick={() => onDownload(item)}><Download size={16} /></button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
});

function App() {
  const [authRequired, setAuthRequired] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [settings, setSettings] = useState(null);
  const [session, setSession] = useState(null);
  const [activeTab, setActiveTab] = useState("analysis");
  const [task, setTask] = useState("");
  const [plan, setPlan] = useState([]);
  const [completed, setCompleted] = useState([]);
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [keyOpen, setKeyOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [effort, setEffort] = useState("high");
  const [thinking, setThinking] = useState(true);
  const [previewItem, setPreviewItem] = useState(null);
  const [previewHtml, setPreviewHtml] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [currentNodeTitle, setCurrentNodeTitle] = useState("");
  // toolTrace: 工具调用时间线。ReAct 执行器内部每次工具调用实时推送，
  // 让用户看到"正在读取数据→正在清洗→正在生成图表"的过程，
  // 而不是只看到"正在执行 (2/4)"一行字等 30 秒。
  const [toolTrace, setToolTrace] = useState([]);
  // 多轮对话：followUps 存储报告之后的追问消息对（user+assistant），
  // 让用户基于已有分析结果继续提问，不必每次都触发完整 plan→execute→finalize。
  // 结构：[{role, content, streaming?, tools?, error?}]
  const [followUps, setFollowUps] = useState([]);
  const [followUpInput, setFollowUpInput] = useState("");
  const [chatRunning, setChatRunning] = useState(false);
  const followUpInputRef = useRef(null);
  const chatControllerRef = useRef(null);
  const [retryOffer, setRetryOffer] = useState(null);
  const [retryChecking, setRetryChecking] = useState(false);
  // 保存设置 / 停止分析的 busy 状态：防止连点发多请求，给用户即时反馈
  const [savingSettings, setSavingSettings] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  // 历史加载错误状态：区分"没数据"和"加载失败"，避免用户误以为数据丢失
  const [historyError, setHistoryError] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  // 已耗时（秒）。running 时由 setInterval 每秒刷新；非 running 时
  // 由 session.elapsed_seconds / completed - started 计算一次性赋值。
  const [elapsedSeconds, setElapsedSeconds] = useState(null);
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
  const retryController = useRef(null);
  const cancelRequested = useRef(false);
  const lastTaskRef = useRef("");
  // 按 sessionId 保存草稿：切换历史会话时不丢失当前正在输入的任务
  const taskDraftsRef = useRef({});
  // 切换历史会话的 loading：让被点击的项立即有反馈，避免用户重复点击
  const [switchingSessionId, setSwitchingSessionId] = useState(null);

  // 主题（light/dark）：useTheme 内部读取 localStorage，无则跟随系统。
  const { theme, toggle: toggleTheme } = useTheme();
  // Reasoning（DeepSeek reasoning_content）：分析/追问时后端推送 thinking_chunk，
  // 累积到这里让 ReportView / ConversationBubble 展示思考过程。
  // 主分析的 reasoning 放在 App 级别（单条），追问的 reasoning 内嵌在每条 assistant 气泡上。
  const [reasoning, setReasoning] = useState("");
  const [reasoningStreaming, setReasoningStreaming] = useState(false);
  // Token 用量：complete / chat_done 事件携带，非流式时展示在报告/气泡底部。
  const [usage, setUsage] = useState(null);
  // 命令面板（Cmd+K）与快捷键帮助（?）弹层状态
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandQuery, setCommandQuery] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);

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
    return () => window.clearInterval(timer);
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
    setPreviewHtml("");
    setPreviewError("");
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
    setError("已发送停止请求；如果模型正在响应，会在本轮响应结束后安全停止。");
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
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (!done) resetIdleTimeout();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) {
          const lines = block.split("\n").filter((line) => line.length > 0 && !line.startsWith(":"));
          const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
          const dataText = lines.find((line) => line.startsWith("data:"))?.slice(5).trim();
          if (!event || !dataText) continue;
          const data = parseSSEData(dataText);
          if (!data) continue;
          if (event === "chat_chunk") {
            // 流式追加到最后一个 assistant 气泡
            setFollowUps((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, content: (last.content || "") + (data.chunk || "") };
              }
              return next;
            });
          } else if (event === "thinking_chunk") {
            // 思考过程追加到最后一个 assistant 气泡的 reasoning 字段，
            // ConversationBubble 内嵌的 ReasoningBlock 会自动展示。
            if (!data.chunk) continue;
            setFollowUps((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, reasoning: (last.reasoning || "") + (data.chunk || "") };
              }
              return next;
            });
          } else if (event === "tool_call") {
            setFollowUps((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = {
                  ...last,
                  tools: [...(last.tools || []), {
                    call_id: data.call_id, name: data.name,
                    status: "running", started_at: data.started_at,
                  }],
                };
              }
              return next;
            });
          } else if (event === "tool_result") {
            setFollowUps((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = {
                  ...last,
                  tools: (last.tools || []).map((t) => t.call_id === data.call_id
                    ? { ...t, status: "done", duration_ms: data.duration_ms }
                    : t),
                };
              }
              return next;
            });
          } else if (event === "chat_done") {
            // 终态：写入完整回复（防止 chunk 丢失），标记非流式，追加新产物
            setFollowUps((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = {
                  ...last,
                  content: data.response || last.content,
                  streaming: false,
                  // 后端可能在终态一次性给出完整 reasoning / usage，覆盖流式累计值
                  reasoning: data.reasoning || last.reasoning,
                  usage: data.usage || last.usage,
                };
              }
              return next;
            });
            if (data.artifacts?.length) {
              setSession((current) => current
                ? { ...current, artifacts: [...(current.artifacts || []), ...data.artifacts] }
                : current);
            }
          } else if (event === "cancelled") {
            setFollowUps((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, streaming: false, error: data.message || "追问已取消。" };
              }
              return next;
            });
          } else if (event === "error") {
            throw new Error(data.message || "追问失败");
          }
        }
        if (done) break;
      }
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
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (!done) resetIdleTimeout();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) {
          // 显式过滤 SSE 注释行（": keep-alive"），避免被当作无 event 的块。
          const lines = block.split("\n").filter((line) => line.length > 0 && !line.startsWith(":"));
          const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
          const dataText = lines.find((line) => line.startsWith("data:"))?.slice(5).trim();
          if (!event || !dataText) continue;
          sawEvent = true;
          const data = parseSSEData(dataText);
          if (!data) continue;
          // 用户切换到历史会话后，SSE 帧仍会到达（后台分析未中断），但 UI 已展示
          // 历史会话的内容。此时不应覆盖 plan/completed/result/currentNodeTitle/session，
          // 否则历史视图会被运行中的分析数据污染。complete 帧仍需记录 completedPayload
          // 以便结束后 setRunning(false) 和刷新 history，让历史列表反映新状态。
          const isViewingRunningSession = session.id === runningSessionIdRef.current;
          if (event === "started") {
            if (isViewingRunningSession) setCurrentNodeTitle("后端已接收任务");
          } else if (event === "progress") {
            if (isViewingRunningSession) setCurrentNodeTitle(data.title || "正在分析");
          } else if (event === "validate_dataset") {
            if (isViewingRunningSession) setCurrentNodeTitle("正在检查数据集结构");
          } else if (event === "plan_analysis") {
            if (isViewingRunningSession) {
              setPlan(data.plan || []);
              setCurrentNodeTitle("正在规划分析步骤");
            }
          } else if (event === "execute_step") {
            if (isViewingRunningSession) {
              setCurrentNodeTitle((current) => current || "正在执行分析步骤");
            }
          } else if (event === "replan") {
            if (isViewingRunningSession) {
              setCompleted(data.completed_steps || []);
              setCurrentNodeTitle("正在审查进度并重规划");
            }
          } else if (event === "thinking_chunk") {
            // DeepSeek reasoning_content：流式思考过程。开始接收时打开 streaming 标记，
            // ReportView 顶部的 ReasoningBlock 会自动展开；接收完后由 report_chunk / complete
            // 阶段自然关闭 streaming。思考过程让用户看到 Agent 的推理链路，减少"黑盒等待"焦虑。
            if (isViewingRunningSession) {
              if (!data.chunk) return;
              setReasoningStreaming(true);
              setReasoning((prev) => prev + (data.chunk || ""));
            }
          } else if (event === "finalize") {
            if (isViewingRunningSession) {
              setCurrentNodeTitle("正在汇总最终报告");
              // finalize 阶段开始输出报告正文，思考过程已结束，关闭 streaming
              setReasoningStreaming(false);
              // 创建空壳 result，让 ReportView 立即显示"正在生成报告…"占位，
              // 后续 report_chunk 事件会逐字追加 response，实现流式打字效果。
              setResult((prev) => prev || { response: "", artifacts: [], plan, completed_steps: completed });
            }
          } else if (event === "report_chunk") {
            // 流式报告：逐字追加，用户看着报告逐字写出，而不是等 30-60 秒看完整报告。
            if (isViewingRunningSession) {
              setResult((prev) => prev
                ? { ...prev, response: (prev.response || "") + (data.chunk || "") }
                : { response: data.chunk || "", artifacts: [], plan, completed_steps: completed }
              );
            }
          } else if (event === "tool_call") {
            // 工具调用开始：追加到时间线，让用户看到 ReAct 内部正在做什么
            if (isViewingRunningSession) {
              setToolTrace((prev) => [...prev, {
                call_id: data.call_id,
                name: data.name,
                input_preview: data.input_preview,
                status: "running",
                started_at: data.started_at,
              }]);
            }
          } else if (event === "tool_result") {
            // 工具调用结束：更新对应 call_id 的状态和耗时
            if (isViewingRunningSession) {
              setToolTrace((prev) => prev.map((item) => item.call_id === data.call_id
                ? { ...item, status: "done", output_preview: data.output_preview, duration_ms: data.duration_ms }
                : item
              ));
            }
          } else if (event === "complete") {
            completedPayload = data;
            if (isViewingRunningSession) {
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
          } else if (event === "cancelled") {
            // 仅在用户未通过 stopAnalysis 主动取消时显示后端取消消息，避免重复 setError。
            if (!cancelRequested.current && isViewingRunningSession) {
              setError(data.message || "分析已取消。");
            }
            // 断点续跑：取消时若有已完成步骤，提供"继续分析"入口
            if (completed.length > 0) {
              setRetryOffer({ task: nextTask, reason: "cancelled", canResume: true, plan, completed });
            }
          } else if (event === "error") {
            throw new Error(data.message || "分析失败");
          } else if (event === "heartbeat") {
            // 仅用于保活，重置 idle timer 即可；不更新 UI。
          }
        }
        if (done) break;
      }
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
              <span><strong>{settings?.model || "deepseek-v4-pro"}</strong><small>{settings?.configured ? "已连接" : "等待配置"}</small></span>
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
            <span>{error}</span>
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
          <EmptyWorkspace uploading={uploading} onUpload={() => fileInput.current?.click()} />
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
                <DataTable rows={session.preview} />
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
                  onLoad={() => setPreviewLoading(false)}
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
      )}
      {helpOpen && <HelpPanel onClose={() => setHelpOpen(false)} />}
    </div>
  );
}

createRoot(document.getElementById("root")).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
);
