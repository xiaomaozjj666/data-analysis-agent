import React, { Component, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle,
  Activity,
  BarChart3,
  Check,
  ChevronDown,
  Circle,
  Database,
  Download,
  Eye,
  EyeOff,
  ExternalLink,
  FileCheck2,
  FileChartColumn,
  FilePlus2,
  FileSpreadsheet,
  KeyRound,
  LoaderCircle,
  Network,
  Play,
  RefreshCw,
  Rows3,
  Settings2,
  Square,
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
const ACTIVE_ANALYSIS_STATES = new Set(["running", "cancelling"]);

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
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
  const timeout = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null;
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
    if (error.name === "AbortError") throw new Error("连接服务超时，请刷新页面后重试。Render 免费实例首次唤醒可能需要几十秒。");
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
function ReportView({ result }) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(result.response || "");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // 浏览器禁用 clipboard（HTTP 部署、iframe 受限）时静默失败，
      // 不阻塞用户阅读报告。
    }
  };

  return (
    <article className="report">
      <div className="report-meta">
        <div className="report-title">
          <FileChartColumn size={15} />
          <span>分析报告</span>
          <small className="report-count">{result.artifacts?.length || 0} 个产物</small>
        </div>
        <div className="report-actions">
          <button
            type="button"
            className="report-copy"
            onClick={handleCopy}
            title="复制全文"
            aria-label="复制报告全文"
          >
            {copied ? <Check size={13} /> : <FileSpreadsheet size={13} />}
            {copied ? "已复制" : "复制"}
          </button>
        </div>
      </div>
      <div className={`report-body ${expanded ? "is-expanded" : ""}`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.response}</ReactMarkdown>
      </div>
      <button
        type="button"
        className="report-toggle"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <ChevronDown size={13} className={expanded ? "rot-180" : ""} />
        {expanded ? "收起报告" : "展开完整报告"}
      </button>
    </article>
  );
}

function PlanPanel({ plan, completed, running, currentNodeTitle }) {
  const completedIds = new Set((completed || []).map((item) => item.id));
  const doneCount = plan.filter((item) => completedIds.has(item.id)).length;

  return (
    <aside className="plan-panel" aria-label="执行记录">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">执行记录</span>
          <h2>分析进度</h2>
        </div>
        <span className={`run-state ${running ? "is-running" : ""}`}>
          {running ? <LoaderCircle size={13} className="spin" /> : <Circle size={9} />}
          {running ? "运行中" : plan.length ? "已完成" : "待开始"}
        </span>
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

      <div className="architecture-note">
        <Network size={14} />
        <span>Plan &amp; Execute</span>
        <i />
        <span>ReAct</span>
      </div>
    </aside>
  );
}

function DataTable({ rows }) {
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
}

function EmptyWorkspace({ uploading, onUpload }) {
  return (
    <section className="empty-workspace">
      <div className="empty-grid" aria-hidden="true">
        <span className="grid-tab" />
        {Array.from({ length: 20 }, (_, index) => <i key={index} />)}
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

function DatasetOverview({ profile }) {
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
}

function formatBytes(value = 0) {
  if (!value) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function ArtifactCenter({ artifacts = [], onDownload, onPreview }) {
  const charts = artifacts.filter((item) => item.kind === "visualization");
  const files = artifacts.filter((item) => item.kind !== "visualization");
  if (!artifacts.length) return <div className="empty-row">分析完成后，最终图表和数据文件会出现在这里。</div>;
  return (
    <div className="artifact-center">
      {charts.length > 0 && (
        <section className="artifact-section">
          <div className="artifact-section-label"><span>交互图表</span><small>{charts.length} 张精选结果</small></div>
          <div className="chart-grid">
            {charts.map((item, index) => (
              <article className="chart-card" key={item.name}>
                <div className="chart-index">{String(index + 1).padStart(2, "0")}</div>
                <FileChartColumn size={20} />
                <div>
                  <strong>{item.description || item.name}</strong>
                  <small>{formatBytes(item.size_bytes)} · HTML 交互图</small>
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
            ))}
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
}

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
  const [retryOffer, setRetryOffer] = useState(null);
  const [retryChecking, setRetryChecking] = useState(false);
  const fileInput = useRef(null);
  const taskInput = useRef(null);
  const analysisController = useRef(null);
  const previewController = useRef(null);
  const retryController = useRef(null);
  const cancelRequested = useRef(false);
  const lastTaskRef = useRef("");

  // Esc 键关闭预览模态框（P0-4）。
  useEffect(() => {
    if (!previewItem) return;
    const onKey = (event) => {
      if (event.key === "Escape") closeArtifactPreview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewItem]);

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

  async function openArtifactPreview(item) {
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
  }

  function closeArtifactPreview() {
    previewController.current?.abort();
    previewController.current = null;
    setPreviewItem(null);
    setPreviewHtml("");
    setPreviewLoading(false);
    setPreviewError("");
  }

  async function downloadArtifact(item) {
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
  }

  async function saveSettings() {
    setError("");
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
    }
  }

  async function uploadFile(file) {
    if (!file) return;
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
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function stopAnalysis() {
    if (!session || !running) return;
    cancelRequested.current = true;
    const cancelRequest = api(`/api/sessions/${session.id}/cancel`, {
      method: "POST",
      timeoutMs: 10000,
    }).catch(() => null);
    // 同时中断 SSE 流和 retry 轮询，确保用户点击停止后所有后台请求都结束。
    analysisController.current?.abort();
    retryController.current?.abort();
    await cancelRequest;
    setRunning(false);
    setRetryChecking(false);
    setCurrentNodeTitle("");
    setRetryOffer(null);
    setError("已发送停止请求；如果模型正在响应，会在本轮响应结束后安全停止。");
  }

  async function startAnalysis(nextTask = task) {
    if (!session || !nextTask.trim() || running) return;
    setRunning(true);
    setError("");
    setResult(null);
    setPlan([]);
    setCompleted([]);
    setCurrentNodeTitle("");
    setRetryOffer(null);
    setTask(nextTask);
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
        body: JSON.stringify({ task: nextTask }),
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
          const data = JSON.parse(dataText);
          if (event === "started") {
            // 任务已被后端接收，确认运行中。
            setCurrentNodeTitle("后端已接收任务");
          } else if (event === "progress") {
            // 节点级进度（如"正在检查数据集结构"），立即更新 UI。
            setCurrentNodeTitle(data.title || "正在分析");
          } else if (event === "validate_dataset") {
            setCurrentNodeTitle("正在检查数据集结构");
          } else if (event === "plan_analysis") {
            setPlan(data.plan || []);
            setCurrentNodeTitle("正在规划分析步骤");
          } else if (event === "execute_step") {
            // execute_step 的 data 是节点状态更新，标题已由前一帧 progress 给出；
            // 若没有 progress 帧兜底，使用一个通用文案。
            setCurrentNodeTitle((current) => current || "正在执行分析步骤");
          } else if (event === "replan") {
            setCompleted(data.completed_steps || []);
            setCurrentNodeTitle("正在审查进度并重规划");
          } else if (event === "finalize") {
            setCurrentNodeTitle("正在汇总最终报告");
          } else if (event === "complete") {
            completedPayload = data;
            setResult(data);
            setPlan(data.plan || []);
            setCompleted(data.completed_steps || []);
            // 乐观更新 artifacts，refresh 失败时仍能看到产物。
            setSession((current) => (current ? { ...current, artifacts: data.artifacts || [] } : current));
            setCurrentNodeTitle("");
          } else if (event === "cancelled") {
            // 仅在用户未通过 stopAnalysis 主动取消时显示后端取消消息，避免重复 setError。
            if (!cancelRequested.current) {
              setError(data.message || "分析已取消。");
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
        try {
          const refreshed = await api(`/api/sessions/${session.id}`);
          setSession(refreshed);
        } catch {
          // refresh 失败时保留 complete 帧的乐观更新，不阻塞用户。
        }
      }
    } catch (err) {
      if (err.name === "AbortError" && cancelRequested.current) {
        setError("分析已取消，已完成的步骤不会继续扩展。");
      } else if (err.name === "AbortError") {
        // idle timeout：长时间未收到事件。
        setError("长时间未收到分析进度，连接已断开。");
        setRetryOffer({ task: nextTask, reason: "idle" });
      } else if (!sawEvent && err.name === "TypeError") {
        // fetch 网络层错误（DNS/CORS/离线），尚未收到任何 SSE 帧。
        setError(`无法连接分析服务：${err.message}`);
        setRetryOffer({ task: nextTask, reason: "network" });
      } else if (err instanceof ApiError && err.status === 404) {
        // 重运行时服务端 session 已被清理，引导用户重新上传。
        handleSessionLost("会话已失效（服务端数据已被清理），请重新上传数据集后再开始分析。");
      } else {
        setError(err.message);
        setRetryOffer({ task: nextTask, reason: "error" });
      }
    } finally {
      if (idleTimeout) window.clearTimeout(idleTimeout);
      if (analysisController.current === controller) analysisController.current = null;
      setRunning(false);
      setCurrentNodeTitle("");
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
    retryController.current?.abort();
    analysisController.current?.abort();
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
        const deadline = Date.now() + 5 * 60 * 1000;
        while (ACTIVE_ANALYSIS_STATES.has(latest.analysis_status) && Date.now() < deadline) {
          await wait(3000);
          if (controller.signal.aborted) break;
          latest = await api(`/api/sessions/${session.id}`, { timeoutMs: 8000, signal: controller.signal });
          setSession(latest);
        }
      }

      setRunning(false);
      setCurrentNodeTitle("");
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
                <button type="button" title="收起设置" onClick={() => setKeyOpen(false)}><X size={15} /></button>
              </div>
              <label>API Key</label>
              <div className="secret-input">
                <KeyRound size={14} />
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={settings?.configured ? "已安全保存" : "输入 DeepSeek Key"}
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
                {["high", "max"].map((value) => (
                  <button type="button" key={value} className={effort === value ? "selected" : ""} onClick={() => setEffort(value)}>{value}</button>
                ))}
              </div>
              <button type="submit" className="save-button"><Check size={14} />保存设置</button>
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
            <ChevronDown size={13} />
            <strong>{session?.filename || "未命名分析"}</strong>
          </div>
          <div className="api-status"><i className={settings ? "online" : ""} />{settings ? "服务正常" : "连接中"}</div>
        </header>

        {error && (
          <div className="error-banner" role="alert">
            <Activity size={16} />
            <span>{error}</span>
            {retryOffer && !running && (
              <button
                type="button"
                className="retry-button"
                onClick={retryAnalysis}
                disabled={retryChecking}
                aria-busy={retryChecking}
              >
                <RefreshCw size={13} className={retryChecking ? "spin" : ""} />
                {retryChecking ? "确认中" : retryOffer.reason === "ready" ? "重新运行" : "检查状态"}
              </button>
            )}
            <button type="button" title="关闭" className="error-close" onClick={() => { setError(""); setRetryOffer(null); }}><X size={15} /></button>
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

            <nav className="tabs" aria-label="工作区视图">
              <button className={activeTab === "analysis" ? "active" : ""} onClick={() => setActiveTab("analysis")}><BarChart3 size={15} />分析</button>
              <button className={activeTab === "data" ? "active" : ""} onClick={() => setActiveTab("data")}><Table2 size={15} />数据</button>
              <button className={activeTab === "artifacts" ? "active" : ""} onClick={() => setActiveTab("artifacts")}><FileSpreadsheet size={15} />产物 <span>{session.artifacts?.length || 0}</span></button>
            </nav>

            {activeTab === "analysis" && (
              <div className="analysis-grid">
                <section className="analysis-column">
                  <div className={`task-box ${running ? "is-running" : ""}`}>
                    <div className="task-heading">
                      <div>
                        <span className="section-kicker">分析任务</span>
                        <h2>你想从数据中了解什么？</h2>
                      </div>
                      {running && <span><LoaderCircle className="spin" size={14} />正在分析</span>}
                    </div>
                    <textarea
                      ref={taskInput}
                      value={task}
                      onChange={(event) => setTask(event.target.value)}
                      placeholder="例如：比较各区域销售表现，解释异常波动并生成趋势图"
                      rows={3}
                    />
                    <div className="task-actions">
                      <div className="preset-row">
                        {presets.map(({ title, detail, icon: Icon, task: presetTask }) => (
                          <button
                            key={title}
                            title={detail}
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
                        <button className="cancel-button" onClick={stopAnalysis}>
                          <Square size={13} fill="currentColor" />停止分析
                        </button>
                      ) : (
                        <button className="run-button" onClick={() => startAnalysis()} disabled={!task.trim() || !settings?.configured}>
                          <Play size={15} fill="currentColor" />运行分析
                        </button>
                      )}
                    </div>
                  </div>
                  {!settings?.configured && <p className="composer-note">请先在左侧配置 DeepSeek API Key。</p>}

                  {result ? (
                    <ReportView result={result} />
                  ) : (
                    <DatasetOverview profile={profile} />
                  )}
                </section>
                <PlanPanel plan={plan} completed={completed} running={running} currentNodeTitle={currentNodeTitle} />
              </div>
            )}

            {activeTab === "data" && (
              <section className="data-view">
                <div className="section-title">
                  <div><span className="section-kicker">数据预览</span><h2>原始记录</h2></div>
                  <small>前 100 行</small>
                </div>
                <DataTable rows={session.preview} />
              </section>
            )}

            {activeTab === "artifacts" && (
              <section className="artifact-view">
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
    </div>
  );
}

createRoot(document.getElementById("root")).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
);
