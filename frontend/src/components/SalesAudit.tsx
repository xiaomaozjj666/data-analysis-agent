import { useRef, useState } from "react";
import {
  Check,
  Copy,
  Download,
  FileCheck2,
  LoaderCircle,
  Presentation,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { api, ApiError } from "../utils/api";
import { splitReportAppendix } from "../utils/reportAppendix";
import SalesAuditDemo from "./SalesAuditDemo";
import "./SalesAudit.css";

const MAX_DRAFT_LENGTH = 6000;
const DEMO_DRAFT = "销售额合计：17224.00 元\n销量合计：462 件\n订单数：462 笔\n市场份额：37%";
const DRAFT_HEADING = "## 待核验指标（全表）";

export const SALES_BRIEF_TASK = [
  "为当前 CSV 起草简报。只规划一步计算三项全表指标，直接读取原始数据，不清洗、不采样、不做图、不单独写文件。",
  "销售额合计为 sales 求和（单位元，精确到分），销量合计为 quantity 求和（单位件），订单数为 order_id 去重计数（单位笔）。",
  `在最终报告开头添加「${DRAFT_HEADING}」这一 Markdown 小节。小节只包含三个独立的指标行，格式为「销售额合计：数字 元」「销量合计：数字 件」「订单数：数字 笔」。数字不带千位分隔符。`,
].join("\n");

const VERDICTS = { verified: "已核验", conflict: "数值冲突", unsupported: "待人工复核" } as const;
type Verdict = keyof typeof VERDICTS;
type Summary = Record<Verdict, number>;

export interface AuditSource {
  filename: string;
  sha256: string;
  rows: number;
  encoding?: string;
  preview: { record: number; order_id: string; sales: string; quantity: string }[];
  order_ids?: number;
}

export interface AuditWarning {
  code: "near_duplicate_order_id" | "summary_row" | string;
  message: string;
  samples?: { normalized: string; order_ids: string[]; count: number }[];
  total_groups?: number;
  count?: number;
}

export interface AuditLedgerEntry {
  seq: number;
  hash: string;
  prev?: string;
}

export interface ConfirmReceipt {
  seq: number;
  ts: string;
  hash: string;
  prev?: string;
  brief_sha256: string;
  draft_trace_sha256?: string | null;
}

export interface AuditResult {
  schema_version?: number;
  audit_id?: string;
  source: AuditSource;
  metrics?: Record<string, string>;
  warnings?: AuditWarning[];
  summary: Summary;
  claims: {
    line: number;
    text: string;
    metric: string | null;
    status: Verdict;
    reason: string;
    caveat?: string;
    submitted?: string;
    recalculated?: string;
    formula?: string;
  }[];
  needs_scope_confirmation: boolean;
  confirmed_full_table?: boolean;
  draft_trace_sha256?: string | null;
  corrected_brief: string | null;
  ledger?: AuditLedgerEntry;
  ledger_error?: string;
}

export interface ConfirmResult {
  confirmed: true;
  audit_id: string;
  brief: string;
  source: AuditSource;
  summary: Summary;
  receipt: ConfirmReceipt;
}

export interface QuickDraftResult {
  draft: string;
  model: string;
  thinking: boolean;
  mode: "direct_no_tools";
  elapsed_ms: number;
  rows_sent: number;
  masked: boolean;
  masked_cells: number;
  dropped: number;
  disclaimer: string;
  trace_sha256: string | null;
  raw_output_sha256: string;
  parsed_draft_sha256: string;
}

interface SalesAuditProps {
  sessionId: string;
  filename: string;
  initialDemo: boolean;
  report?: string;
  onPrepareDraft?: () => void;
  /** 会话失效后重新载入示例：只通知新会话 id，由上层切换当前会话。 */
  onReloadSession?: (id: string) => void;
  /** 返回空工作台上传数据：由上层清理当前会话，不在组件内改路由。 */
  onExitSession?: () => void;
}

function download(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function isStatus(err: unknown, status: number): boolean {
  return err instanceof ApiError && err.status === status;
}

// 429 的等待秒数优先取 Retry-After，缺失时回退到后端文案里的「N 秒」。
function retryAfterSeconds(err: unknown): number | null {
  if (err instanceof ApiError && typeof err.retryAfter === "number" && err.retryAfter > 0) {
    return err.retryAfter;
  }
  const match = err instanceof Error ? err.message.match(/(\d+)\s*秒/) : null;
  return match ? Number(match[1]) : null;
}

function formatReceiptTime(ts: string): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString("zh-CN", { hour12: false });
}

function extractDraft(report: string): string {
  const { body } = splitReportAppendix(report);
  const lines = body.split(/\r?\n/);
  let fence = "";
  let start = -1;
  let end = lines.length;
  for (const [index, line] of lines.entries()) {
    const delimiter = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (delimiter) {
      const [, marker, tail] = delimiter;
      if (!fence) fence = marker;
      else if (marker.startsWith(fence) && !tail.trim()) fence = "";
    } else if (!fence && /^ {0,3}#{1,6}[ \t]/.test(line)) {
      if (start >= 0) {
        end = index;
        break;
      }
      if (line.trim().replace(/^##[ \t]+/, "## ") === DRAFT_HEADING) start = index;
    }
  }
  return start >= 0 ? lines.slice(start + 1, end).join("\n").trimEnd() || body : body;
}

function SourceEvidence({ source }: { source: AuditSource }) {
  return (
    <details className="sales-audit-card sales-audit-evidence">
      <summary>查看来源 · {source.filename} · 共 {source.rows} 条明细</summary>
      <p>复算覆盖全表，下方展示前 {source.preview.length} 条记录。</p>
      <pre>SHA-256 {source.sha256}</pre>
      <p>
        文件编码 {source.encoding ?? "未报告"}
        {typeof source.order_ids === "number" ? ` · 去重后订单数 ${source.order_ids}` : ""}
      </p>
      <div className="sales-audit-table">
        <table>
          <thead><tr><th>记录</th><th>order_id</th><th>sales（元）</th><th>quantity（件）</th></tr></thead>
          <tbody>{source.preview.map((row) => (
            <tr key={row.record}><td>{row.record}</td><td>{row.order_id}</td><td>{row.sales}</td><td>{row.quantity}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </details>
  );
}

function VerifiedBrief({ brief, onDownload }: { brief: string; onDownload?: () => void }) {
  return (
    <section className="sales-audit-card">
      <div className="sales-audit-claim-heading">
        <h3>核验版简报</h3>
        {onDownload && (
          <div className="sales-audit-actions">
            <button className="primary" type="button" onClick={onDownload}>
              <Download size={16} />下载核验版简报 (.md)
            </button>
          </div>
        )}
      </div>
      <div className="sales-audit-report report-body">
        <ReactMarkdown>{brief.replace(/^# [^\n]+\n+/, "")}</ReactMarkdown>
      </div>
    </section>
  );
}

interface ResultNoticesProps {
  audit: AuditResult;
  receipt: ConfirmReceipt | null;
  draftTrace: string | null;
  copiedHash: string | null;
  copyHint: string;
  onCopyHash: (hash: string) => void;
}

// 告警、账本写入与发布回执：正常工作台和路演结果区共用。
function ResultNotices({
  audit, receipt, draftTrace, copiedHash, copyHint, onCopyHash,
}: ResultNoticesProps) {
  const warnings = audit.warnings ?? [];
  const ledger = audit.ledger;
  const trace = audit.draft_trace_sha256 ?? draftTrace;
  return (
    <>
      {trace && (
        <p className="sales-audit-trace">已绑定起草轨迹 {trace.slice(0, 12)}…</p>
      )}
      {warnings.length > 0 && (
        <section className="sales-audit-warnings" role="status">
          <p className="sales-audit-warnings-heading">
            <TriangleAlert size={16} aria-hidden="true" />
            <strong>数据告警（只告警，订单数口径未改）</strong>
          </p>
          <ul>
            {warnings.map((warning) => (
              <li key={warning.code}>{warning.message}</li>
            ))}
          </ul>
        </section>
      )}
      {ledger && (
        <div className="sales-audit-ledger" role="status">
          <span>已写入审计账本 #{ledger.seq}</span>
          <code>{ledger.hash}</code>
          <button type="button" onClick={() => onCopyHash(ledger.hash)}>
            {copiedHash === ledger.hash ? <Check size={14} /> : <Copy size={14} />}
            {copiedHash === ledger.hash ? "已复制" : "复制 hash"}
          </button>
          {copyHint && <small>{copyHint}</small>}
        </div>
      )}
      {audit.ledger_error && (
        <p className="sales-audit-error">审计账本写入失败：{audit.ledger_error}。本次核验结果仍然有效。</p>
      )}
      {receipt && (
        <section className="sales-audit-receipt" role="status">
          <strong>账本已记录</strong>
          <dl>
            <div><dt>序号</dt><dd>#{receipt.seq}</dd></div>
            <div><dt>时间</dt><dd>{formatReceiptTime(receipt.ts)}</dd></div>
            <div><dt>hash</dt><dd><code>{receipt.hash.slice(0, 16)}</code></dd></div>
          </dl>
          <small>发布回执来自服务端账本，可与核验明细中的回执字段逐项核对。</small>
        </section>
      )}
    </>
  );
}

export default function SalesAudit({
  sessionId,
  filename,
  initialDemo,
  report,
  onPrepareDraft,
  onReloadSession,
  onExitSession,
}: SalesAuditProps) {
  const [draft, setDraft] = useState(initialDemo ? DEMO_DRAFT : "");
  const [demoDraft, setDemoDraft] = useState(initialDemo);
  const [showcase, setShowcase] = useState(initialDemo);
  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [receipt, setReceipt] = useState<ConfirmReceipt | null>(null);
  const [serverBrief, setServerBrief] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [quickBusy, setQuickBusy] = useState(false);
  const [quickDraft, setQuickDraft] = useState<QuickDraftResult | null>(null);
  // 快速起草返回的轨迹哈希：手动改草稿后立即清空，避免绑定失效的轨迹。
  const [draftTrace, setDraftTrace] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [sessionLost, setSessionLost] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [recoveryNote, setRecoveryNote] = useState("");
  const [copiedHash, setCopiedHash] = useState<string | null>(null);
  const [copyHint, setCopyHint] = useState("");
  const draftInput = useRef<HTMLTextAreaElement>(null);

  function updateDraft(value: string, isDemo = false) {
    setDraft(value.replace(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/g, "\n"));
    setDemoDraft(isDemo);
    setAudit(null);
    setReceipt(null);
    setServerBrief(null);
    setQuickDraft(null);
    setDraftTrace(null);
    setError("");
  }

  function locateDraftLine(line: number) {
    const input = draftInput.current;
    const lines = draft.split("\n");
    if (!input || !lines[line - 1]) return;
    const start = lines.slice(0, line - 1).reduce((offset, text) => offset + text.length + 1, 0);
    input.focus({ preventScroll: true });
    input.setSelectionRange(start, start + lines[line - 1].length);
    input.scrollTop = (line - 1) * Number.parseFloat(getComputedStyle(input).lineHeight);
    input.scrollLeft = 0;
    input.scrollIntoView({ block: "nearest" });
  }

  async function runAudit(confirmFullTable = false) {
    setBusy(true);
    setError("");
    setAudit(null);
    setReceipt(null);
    setServerBrief(null);
    try {
      const response = await api<AuditResult>(`/api/sessions/${sessionId}/sales-audit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft,
          confirm_full_table: confirmFullTable,
          ...(draftTrace ? { draft_trace_sha256: draftTrace } : {}),
        }),
      });
      setAudit(response);
    } catch (err) {
      if (isStatus(err, 404)) {
        setSessionLost(true);
        return;
      }
      if (isStatus(err, 422) && err instanceof Error && err.message.includes("轨迹")) {
        setError(`${err.message} 修改过草稿后需要重新起草，或去掉轨迹绑定。`);
        return;
      }
      setError(err instanceof Error ? err.message : "核验失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  async function confirmBrief() {
    if (!audit?.audit_id) {
      setError("核验结果缺少 audit_id，请重新复算核验后再确认。");
      return;
    }
    setConfirming(true);
    setError("");
    try {
      const response = await api<ConfirmResult>(`/api/sessions/${sessionId}/sales-audit/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft,
          audit_id: audit.audit_id,
          confirm_full_table: Boolean(audit.confirmed_full_table),
          ...(draftTrace ? { draft_trace_sha256: draftTrace } : {}),
        }),
      });
      setServerBrief(response.brief);
      setReceipt(response.receipt);
      setAudit((current) => current ? {
        ...current,
        source: response.source ?? current.source,
        summary: response.summary ?? current.summary,
        corrected_brief: response.brief,
      } : current);
    } catch (err) {
      if (isStatus(err, 409)) {
        // 核验后草稿或 CSV 发生变化：清空结果，要求重新复算。
        setAudit(null);
        setReceipt(null);
        setServerBrief(null);
        setError("草稿或数据在核验后发生变化，请重新复算核验。");
        draftInput.current?.focus();
        return;
      }
      if (isStatus(err, 404)) {
        setAudit(null);
        setReceipt(null);
        setServerBrief(null);
        setSessionLost(true);
        return;
      }
      if (isStatus(err, 422) && err instanceof Error && err.message.includes("轨迹")) {
        setError(`${err.message} 修改过草稿后需要重新起草，或去掉轨迹绑定。`);
        return;
      }
      setError(err instanceof Error ? err.message : "确认失败，请重试。");
    } finally {
      setConfirming(false);
    }
  }

  async function runQuickDraft() {
    setQuickBusy(true);
    setError("");
    try {
      const response = await api<QuickDraftResult>(`/api/sessions/${sessionId}/sales-audit/quick-draft`, {
        method: "POST",
        timeoutMs: 60000,
      });
      updateDraft(response.draft);
      // updateDraft 会先清空轨迹；这里按服务端返回值重新绑定本次起草。
      setDraftTrace(response.trace_sha256);
      setQuickDraft(response);
    } catch (err) {
      if (isStatus(err, 404)) {
        setSessionLost(true);
        return;
      }
      if (isStatus(err, 429)) {
        const seconds = retryAfterSeconds(err);
        setError(seconds === null
          ? "模型起草请求过于频繁，请稍后重试。"
          : `模型起草请求过于频繁，请 ${seconds} 秒后再试。`);
        return;
      }
      // 503 / 422 等：直接展示后端 detail（模型未配置、上游失败或草稿非法）。
      setError(err instanceof Error ? err.message : "快速起草失败，请重试。");
    } finally {
      setQuickBusy(false);
    }
  }

  async function reloadSample() {
    setReloading(true);
    setRecoveryNote("");
    try {
      const created = await api<{ id: string }>("/api/sessions/sample?audit=true", { method: "POST" });
      if (onReloadSession) onReloadSession(created.id);
      else setRecoveryNote(`示例会话已创建（${created.id}），当前页面未接入会话切换，请刷新页面后继续。`);
    } catch (err) {
      setRecoveryNote(`重新载入示例失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setReloading(false);
    }
  }

  async function copyHash(hash: string) {
    setCopyHint("");
    try {
      await navigator.clipboard.writeText(hash);
      setCopiedHash(hash);
    } catch {
      setCopiedHash(null);
      setCopyHint("复制失败，请手动选择 hash。");
    }
  }

  const supportsCsv = filename.toLowerCase().endsWith(".csv");
  const tooLong = draft.length > MAX_DRAFT_LENGTH;
  const confirmed = Boolean(receipt);
  const brief = confirmed ? serverBrief : null;
  const evidenceView = audit && <SourceEvidence source={audit.source} />;
  const noticesView = audit && (
    <ResultNotices
      audit={audit}
      receipt={receipt}
      draftTrace={draftTrace}
      copiedHash={copiedHash}
      copyHint={copyHint}
      onCopyHash={(hash) => void copyHash(hash)}
    />
  );
  const briefView = brief && <VerifiedBrief brief={brief} onDownload={showcase ? undefined : downloadBrief} />;

  function downloadBrief() {
    if (brief) download("经营简报-核验版.md", brief, "text/markdown;charset=utf-8");
  }

  function downloadAudit() {
    if (!audit) return;
    download("sales-audit.json", JSON.stringify({
      ...audit,
      confirmed,
      receipt,
      server_brief: serverBrief,
      corrected_brief: brief ?? null,
      brief_confirmed: confirmed,
    }, null, 2), "application/json");
  }

  if (sessionLost) {
    return (
      <div className="sales-audit">
        <section className="sales-audit-recovery" role="alert">
          <h2>会话已失效</h2>
          <p>服务端已释放该会话（例如实例重启或会话过期），当前草稿与核验结果无法继续提交。</p>
          <div className="sales-audit-actions">
            <button className="primary" type="button" disabled={reloading} onClick={() => void reloadSample()}>
              {reloading ? <LoaderCircle className="spin" size={16} /> : <Presentation size={16} />}
              {reloading ? "正在载入…" : "重新载入示例"}
            </button>
            <button
              type="button"
              onClick={() => {
                if (onExitSession) onExitSession();
                else setRecoveryNote("请刷新页面后返回工作台上传数据。");
              }}
            >
              返回工作台上传数据
            </button>
          </div>
          {recoveryNote && <p role="status">{recoveryNote}</p>}
        </section>
      </div>
    );
  }

  if (showcase && supportsCsv) {
    return (
      <div className="sales-audit">
        <SalesAuditDemo
          draft={draft}
          isExample={demoDraft}
          audit={audit}
          busy={busy}
          confirming={confirming}
          confirmed={confirmed}
          canRun={Boolean(draft.trim()) && !tooLong}
          error={tooLong ? `草稿最多 ${MAX_DRAFT_LENGTH} 字符，请在工作台删减后重试。` : error}
          evidence={evidenceView}
          brief={briefView}
          details={noticesView}
          onRun={() => void runAudit()}
          onConfirm={() => void confirmBrief()}
          onDownloadEvidence={downloadAudit}
          onDownloadBrief={downloadBrief}
          onEdit={() => {
            setShowcase(false);
            window.requestAnimationFrame(() => draftInput.current?.focus());
          }}
          onReset={() => updateDraft(DEMO_DRAFT, true)}
        />
      </div>
    );
  }

  return (
    <div className="sales-audit">
      <header className="sales-audit-header">
        <ShieldCheck size={28} aria-hidden="true" />
        <div>
          <h2>经营简报核验</h2>
          <p>从原始明细复算销售额、销量和订单数，确认后导出核验版简报。</p>
        </div>
        {initialDemo && (
          <div className="sales-audit-actions sales-audit-showcase-action">
            <button type="button" onClick={() => setShowcase(true)} disabled={busy}>
              <Presentation size={16} />返回路演
            </button>
          </div>
        )}
      </header>
      {!supportsCsv ? (
        <p className="sales-audit-notice">请用「替换数据」上传含 order_id、sales、quantity 三列的 UTF-8 CSV。</p>
      ) : (
        <>
          <div className="sales-audit-grid">
            <form className="sales-audit-card" onSubmit={(event) => { event.preventDefault(); void runAudit(); }}>
              <h3><label htmlFor="sales-audit-draft">简报草稿</label></h3>
              <p id="sales-audit-format">
                统计范围为原始 CSV 全表。每行一个指标，使用「销售额合计：… 元」「销量合计：… 件」「订单数：… 笔」。
              </p>
              {demoDraft && <p className="sales-audit-demo-note">演示草稿含预设错数和一项缺少依据的结论。</p>}
              <div className="sales-audit-actions">
                {(filename === "sample_sales.csv" || filename === "sample_sales_audit.csv") && (
                  <button type="button" disabled={busy} onClick={() => updateDraft(DEMO_DRAFT, true)}>填入演示草稿</button>
                )}
                {onPrepareDraft && <button type="button" disabled={busy} onClick={onPrepareDraft}>准备 AI 起草任务</button>}
                {report && (
                  <button type="button" disabled={busy} onClick={() => updateDraft(extractDraft(report))}>
                    使用分析报告草稿
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || quickBusy || tooLong}
                  onClick={() => void runQuickDraft()}
                >
                  {quickBusy ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
                  {quickBusy ? "模型正在起草…" : "让模型直接起草（不调工具）"}
                </button>
              </div>
              <textarea
                ref={draftInput}
                id="sales-audit-draft"
                aria-describedby="sales-audit-format"
                aria-invalid={tooLong || undefined}
                value={draft}
                onChange={(event) => updateDraft(event.target.value)}
                placeholder={"销售额合计：… 元\n销量合计：… 件\n订单数：… 笔"}
                rows={7}
                disabled={busy}
              />
              {quickDraft && (
                <section className="sales-audit-quickdraft" role="status">
                  <p><strong>模型原始输出，尚未核验。</strong></p>
                  <p className="sales-audit-quickdraft-meta">
                    模型 {quickDraft.model} · 耗时 {(quickDraft.elapsed_ms / 1000).toFixed(1)} 秒
                    {" · "}读取 {quickDraft.rows_sent} 行
                    {" · "}{quickDraft.masked ? `已脱敏（${quickDraft.masked_cells} 格）` : "未脱敏"}
                    {" · "}丢弃 {quickDraft.dropped} 行
                  </p>
                  <p>{quickDraft.disclaimer}</p>
                </section>
              )}
              {tooLong && <p role="alert">草稿最多 {MAX_DRAFT_LENGTH} 字符，请保留要核验的部分。</p>}
              {error && <p className="sales-audit-error" role="alert">{error}</p>}
              <div className="sales-audit-actions">
                <button className="primary" type="submit" disabled={busy || !draft.trim() || tooLong}>
                  {busy ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}
                  {busy ? "正在复算…" : "复算并核验"}
                </button>
              </div>
            </form>

            <section className="sales-audit-card" aria-label="核验结果">
              <h3>核验结果</h3>
              {!audit ? <p>核验后查看草稿原值、复算值和计算口径。</p> : (
                <>
                  {noticesView}
                  <p className="sales-audit-summary" role="status">
                    {Object.entries(VERDICTS).map(([status, label]) => (
                      <span key={status} className={`sales-audit-verdict sales-audit-${status}`}>
                        {label} {audit.summary[status as Verdict]}
                      </span>
                    ))}
                  </p>
                  <ul className="sales-audit-claims">
                    {audit.claims.map((claim) => (
                      <li key={claim.line}>
                        <div className="sales-audit-claim-heading">
                          <strong>{claim.text}</strong>
                          <span className={`sales-audit-verdict sales-audit-${claim.status}`}>
                            {VERDICTS[claim.status]}
                          </span>
                        </div>
                        <small>
                          <button className="sales-audit-locate" type="button" onClick={() => locateDraftLine(claim.line)}>
                            定位第 {claim.line} 行
                          </button>
                          {" · "}{claim.reason}
                        </small>
                        {claim.caveat && <p className="sales-audit-caveat">{claim.caveat}</p>}
                        {claim.recalculated && claim.status !== "unsupported" && (
                          <div className="sales-audit-comparison">
                            <span>草稿 {claim.submitted}</span>
                            <span>复算 <b>{claim.recalculated}</b></span>
                          </div>
                        )}
                        {claim.formula && <small>{claim.formula}</small>}
                      </li>
                    ))}
                  </ul>
                  {audit.needs_scope_confirmation ? (
                    <p>草稿带有标题或说明。请确认指标行描述的是原始 CSV 全表，分组或时间范围的指标可以先从草稿中移除。</p>
                  ) : !audit.corrected_brief && <p>请修改草稿并重新核验，至少核验一项指标才能生成简报。</p>}
                  {audit.corrected_brief && !confirmed && audit.summary.conflict > 0 && (
                    <p>确认后用复算值更正 {audit.summary.conflict} 项，草稿原文保留在核验明细中。</p>
                  )}
                  <div className="sales-audit-actions">
                    {audit.needs_scope_confirmation && (
                      <button type="button" onClick={() => void runAudit(true)}>确认指标范围为全表并复算</button>
                    )}
                    {audit.corrected_brief && !confirmed && (
                      <button className="primary" type="button" disabled={confirming} onClick={() => void confirmBrief()}>
                        {confirming ? <LoaderCircle className="spin" size={16} /> : <FileCheck2 size={16} />}
                        {confirming
                          ? "正在确认…"
                          : audit.summary.conflict ? "确认更正并生成简报" : "生成核验版简报"}
                      </button>
                    )}
                    <button type="button" onClick={downloadAudit}>
                      <Download size={16} />下载核验明细 (.json)
                    </button>
                  </div>
                </>
              )}
            </section>
          </div>
          {evidenceView}
          {briefView}
        </>
      )}
    </div>
  );
}
