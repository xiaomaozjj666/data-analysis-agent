import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  BarChart3,
  Check,
  ChevronDown,
  Circle,
  Database,
  Download,
  Eye,
  EyeOff,
  FileCheck2,
  FileSpreadsheet,
  KeyRound,
  LoaderCircle,
  Network,
  Play,
  RefreshCw,
  Send,
  Settings2,
  Sparkles,
  Upload,
} from "lucide-react";
import "./styles.css";

const API_URL = (
  import.meta.env.VITE_API_URL ||
  (import.meta.env.PROD ? window.location.origin : "http://127.0.0.1:8000")
).replace(/\/$/, "");

const presets = [
  {
    title: "完整质量检查",
    detail: "清洗、统计、图表与导出",
    icon: FileCheck2,
    task: "对当前数据执行完整分析：检查数据质量，采用保守策略完成必要清洗，进行描述统计和关键关系分析，创建最有解释力的图表，并导出清洗后的数据。",
  },
  {
    title: "识别关键驱动",
    detail: "相关分析与回归诊断",
    icon: Network,
    task: "识别核心数值指标之间的关系和潜在驱动因素，完成必要清洗、相关分析和适用的回归分析，并生成关系图表。",
  },
  {
    title: "诊断异常分布",
    detail: "缺失、离群与分布检查",
    icon: Activity,
    task: "诊断缺失、重复和异常值，分析主要数值字段的分布与离群点，采用谨慎的清洗方式并创建分布图和箱线图。",
  },
];

async function api(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error(payload?.detail || payload || `请求失败 (${response.status})`);
  }
  return payload;
}

function Metric({ label, value, unit }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}<small>{unit}</small></strong>
    </div>
  );
}

function PlanPanel({ plan, completed, running }) {
  const completedIds = new Set((completed || []).map((item) => item.id));
  return (
    <aside className="plan-panel" aria-label="执行计划">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">PLAN &amp; EXECUTE</span>
          <h2>执行计划</h2>
        </div>
        <span className={`run-state ${running ? "is-running" : ""}`}>
          {running ? <LoaderCircle size={14} className="spin" /> : <Circle size={11} />}
          {running ? "执行中" : plan.length ? "已就绪" : "待规划"}
        </span>
      </div>
      {!plan.length ? (
        <div className="plan-empty">开始分析后，规划器会根据字段与数据质量生成执行步骤。</div>
      ) : (
        <ol className="plan-list">
          {plan.map((step, index) => {
            const done = completedIds.has(step.id);
            const active = running && !done && plan.slice(0, index).every((item) => completedIds.has(item.id));
            return (
              <li key={`${step.id}-${index}`} className={done ? "done" : active ? "active" : ""}>
                <span className="step-mark">{done ? <Check size={14} /> : index + 1}</span>
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
        <Network size={15} />
        <span>规划器 → ReAct 执行器 → 动态重规划</span>
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
        <thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>{columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function App() {
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
  const fileInput = useRef(null);

  useEffect(() => {
    api("/api/settings")
      .then((value) => {
        setSettings(value);
        setEffort(value.reasoning_effort);
        setThinking(value.thinking_enabled);
        setKeyOpen(!value.configured);
      })
      .catch((err) => setError(`后端连接失败：${err.message}`));
  }, []);

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
    } catch (err) {
      setError(err.message);
    }
  }

  async function uploadFile(file) {
    if (!file) return;
    setUploading(true);
    setError("");
    const form = new FormData();
    form.append("file", file);
    try {
      const value = await api("/api/sessions", { method: "POST", body: form });
      setSession(value);
      setPlan([]);
      setCompleted([]);
      setResult(null);
      setActiveTab("analysis");
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function startAnalysis(nextTask = task) {
    if (!session || !nextTask.trim() || running) return;
    setRunning(true);
    setError("");
    setResult(null);
    setPlan([]);
    setCompleted([]);
    setTask(nextTask);
    try {
      const response = await fetch(`${API_URL}/api/sessions/${session.id}/analyze/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: nextTask }),
      });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(payload.detail || "分析请求失败");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) {
          const event = block.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim();
          const dataText = block.split("\n").find((line) => line.startsWith("data:"))?.slice(5).trim();
          if (!event || !dataText) continue;
          const data = JSON.parse(dataText);
          if (event === "plan_analysis") setPlan(data.plan || []);
          if (event === "replan") setCompleted(data.completed_steps || []);
          if (event === "complete") {
            setResult(data);
            setPlan(data.plan || []);
            setCompleted(data.completed_steps || []);
            setSession((current) => ({ ...current, artifacts: data.artifacts || [] }));
          }
          if (event === "error") throw new Error(data.message || "分析失败");
        }
        if (done) break;
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  }

  const profile = session?.profile;
  const missingCount = profile?.column_info?.reduce((sum, item) => sum + item.missing, 0) || 0;
  const missingRate = profile ? ((missingCount / Math.max(profile.rows * profile.columns, 1)) * 100).toFixed(1) : "0.0";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="wordmark">
          <BarChart3 size={22} strokeWidth={1.8} />
          <div><strong>数据分析</strong><span>本地工作区</span></div>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">数据集</span>
          {session ? (
            <button className="dataset-button" onClick={() => fileInput.current?.click()}>
              <FileSpreadsheet size={18} />
              <span><strong>{session.filename}</strong><small>{profile.rows.toLocaleString()} 行 · {profile.columns} 列</small></span>
              <RefreshCw size={14} />
            </button>
          ) : (
            <button className="upload-button" onClick={() => fileInput.current?.click()} disabled={uploading}>
              {uploading ? <LoaderCircle className="spin" size={18} /> : <Upload size={18} />}
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

        <div className="sidebar-section model-section">
          <span className="sidebar-label">模型</span>
          <button className="model-line" onClick={() => setKeyOpen((value) => !value)}>
            <span><i className={settings?.configured ? "online" : ""} />DeepSeek V4 Pro</span>
            <ChevronDown size={15} className={keyOpen ? "rotate" : ""} />
          </button>
          {keyOpen && (
            <form className="settings-form" onSubmit={(event) => { event.preventDefault(); saveSettings(); }}>
              <label>API Key</label>
              <div className="secret-input">
                <KeyRound size={15} />
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={settings?.configured ? "已安全保存" : "输入 DeepSeek Key"}
                />
                <button type="button" title={showKey ? "隐藏 Key" : "显示 Key"} onClick={() => setShowKey((value) => !value)}>
                  {showKey ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
              <label className="toggle-row">
                <span>思考模式</span>
                <input type="checkbox" checked={thinking} onChange={(event) => setThinking(event.target.checked)} />
              </label>
              <label>推理强度</label>
              <div className="segment">
                {['high', 'max'].map((value) => (
                  <button type="button" key={value} className={effort === value ? "selected" : ""} onClick={() => setEffort(value)}>{value}</button>
                ))}
              </div>
              <button type="submit" className="save-button"><Check size={15} />保存设置</button>
            </form>
          )}
        </div>

        <div className="sidebar-foot">
          <span><i className={settings?.langsmith_tracing ? "online" : ""} />LangSmith Trace</span>
          <small>{settings?.langsmith_tracing ? settings.langsmith_project : "本地模式"}</small>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div><span>工作区</span><h1>{session?.filename || "新建分析"}</h1></div>
          <div className="api-status"><i className={settings ? "online" : ""} />API {settings ? "已连接" : "连接中"}</div>
        </header>

        {error && <div className="error-banner"><Activity size={16} />{error}<button onClick={() => setError("")}>关闭</button></div>}

        {!session ? (
          <section className="empty-workspace">
            <div className="empty-copy">
              <span className="eyebrow">DATA WORKSPACE</span>
              <h2>打开一个数据集</h2>
              <p>支持 CSV、Excel、JSON 与 Parquet，文件通过独立后端接口进入隔离会话。</p>
              <button className="primary" onClick={() => fileInput.current?.click()} disabled={uploading}>
                <Upload size={17} />{uploading ? "正在上传" : "上传数据"}
              </button>
            </div>
            <div className="empty-ledger" aria-label="支持能力">
              <div><span>01</span><strong>检查与清洗</strong><small>字段、缺失、重复、异常</small></div>
              <div><span>02</span><strong>统计分析</strong><small>检验、相关、回归与分组</small></div>
              <div><span>03</span><strong>复杂可视化</strong><small>交互图表与独立产物</small></div>
            </div>
          </section>
        ) : (
          <>
            <section className="metrics-band">
              <Metric label="记录" value={profile.rows.toLocaleString()} unit="行" />
              <Metric label="字段" value={profile.columns} unit="列" />
              <Metric label="缺失率" value={missingRate} unit="%" />
              <Metric label="分析产物" value={session.artifacts?.length || 0} unit="项" />
            </section>

            <nav className="tabs" aria-label="工作区视图">
              {[['analysis', '分析'], ['data', '数据'], ['artifacts', `产物 ${session.artifacts?.length || 0}`]].map(([id, label]) => (
                <button key={id} className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>{label}</button>
              ))}
            </nav>

            {activeTab === "analysis" && (
              <div className="analysis-grid">
                <section className="analysis-column">
                  <div className="section-title"><div><span className="eyebrow">ANALYSIS REQUEST</span><h2>分析任务</h2></div></div>
                  <div className="preset-row">
                    {presets.map(({ title, detail, icon: Icon, task: presetTask }) => (
                      <button key={title} onClick={() => { setTask(presetTask); startAnalysis(presetTask); }} disabled={running}>
                        <Icon size={18} /><span><strong>{title}</strong><small>{detail}</small></span>
                      </button>
                    ))}
                  </div>

                  {result && (
                    <article className="report">
                      <div className="report-meta"><Sparkles size={15} /><span>分析报告</span></div>
                      <div className="report-body">{result.response}</div>
                    </article>
                  )}

                  <div className="composer">
                    <textarea
                      value={task}
                      onChange={(event) => setTask(event.target.value)}
                      placeholder="例如：比较各区域销售表现，并检验差异是否显著"
                      rows={3}
                    />
                    <button title="开始分析" onClick={() => startAnalysis()} disabled={!task.trim() || running || !settings?.configured}>
                      {running ? <LoaderCircle className="spin" size={19} /> : <Send size={19} />}
                    </button>
                  </div>
                  {!settings?.configured && <p className="composer-note">配置 DeepSeek API Key 后即可开始分析。</p>}
                </section>
                <PlanPanel plan={plan} completed={completed} running={running} />
              </div>
            )}

            {activeTab === "data" && (
              <section className="data-view">
                <div className="section-title"><div><span className="eyebrow">DATA PREVIEW</span><h2>数据预览</h2></div><small>显示前 100 行</small></div>
                <DataTable rows={session.preview} />
              </section>
            )}

            {activeTab === "artifacts" && (
              <section className="artifact-view">
                <div className="section-title"><div><span className="eyebrow">OUTPUTS</span><h2>分析产物</h2></div></div>
                {!session.artifacts?.length ? <div className="empty-row">完成分析后，图表和数据文件会显示在这里。</div> : (
                  <div className="artifact-list">
                    {session.artifacts.map((item) => (
                      <div key={item.name}><FileSpreadsheet size={18} /><span><strong>{item.name}</strong><small>{item.description}</small></span><a title="下载" href={`${API_URL}${item.download_url}`}><Download size={17} /></a></div>
                    ))}
                  </div>
                )}
              </section>
            )}
          </>
        )}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
