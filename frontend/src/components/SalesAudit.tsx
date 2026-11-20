import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  FileCheck2,
  Link2,
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
const RECORDED_DRAFT = `销售额合计：17224.00 元
销量合计：462 件
订单数：462 笔`;
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
  signature?: string;
  signature_key_id?: string;
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
  schema_fingerprint?: string | null;
  schema_status?: string | null;
  policy?: {
    principal_id: string;
    pii_columns: string[];
    rls_column?: string | null;
    sha256: string;
  } | null;
  lineage?: {
    upstream_table?: string | null;
    catalog_urn?: string | null;
    owner?: string;
    extracted_at: string;
    age_hours: number;
    freshness_sla_hours: number;
    stale: boolean;
    sha256: string;
  } | null;
}

export interface AuditSchemaSpec {
  name: string;
  column?: string;
  aggregation: "sum" | "count" | "count_distinct" | "min" | "max" | "mean" | "ratio";
  unit?: string;
  decimals?: number;
  aliases?: string[];
  data_type?: "decimal" | "integer" | "date" | "string";
  null_policy?: "exclude" | "fail" | "zero";
  rounding?: "HALF_UP" | "HALF_EVEN";
  sign_policy?: "allow" | "non_negative";
  filters?: { column: string; op: string; value: unknown }[];
  time_window?: { column: string; start?: string; end?: string } | null;
  numerator?: string;
  denominator?: string;
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

interface LedgerChainRecord {
  seq: number;
  ts: string;
  kind: string;
  prev: string;
  hash: string;
  audit_id?: string;
  brief_sha256?: string;
  signature?: string;
  signature_key_id?: string;
}

interface LedgerChainResponse {
  ledger: string;
  verification: {
    valid: boolean;
    entries: number;
    signed?: number;
    tip: string;
    errors: string[];
  };
  entries: LedgerChainRecord[];
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

function retryAfterSeconds(err: unknown): number | null {
  if (err instanceof ApiError && typeof err.retryAfter === "number" && err.retryAfter > 0) {
    return err.retryAfter;
  }
  const match =
    err instanceof Error
      ? err.message.match(/请\s*(\d+)\s*秒/) ?? err.message.match(/(\d+)\s*秒/)
      : null;
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

function SourceEvidence({
  source,
  copiedHash,
  onCopyHash,
}: {
  source: AuditSource;
  copiedHash: string | null;
  onCopyHash: (hash: string) => void;
}) {
  // 统计预览行里重复出现的 order_id，便于大屏一眼看出“多条明细同属一笔订单”
  const orderCounts = new Map<string, number>();
  for (const row of source.preview) {
    orderCounts.set(row.order_id, (orderCounts.get(row.order_id) ?? 0) + 1);
  }
  const dupOrderCount = Array.from(orderCounts.values()).filter((c) => c > 1).length;

  return (
    <details className="sales-audit-card sales-audit-evidence">
      <summary>
        查看来源 · {source.filename} · 共 {source.rows} 条明细
        {dupOrderCount > 0 && <span className="sales-audit-dup-hint">前 {source.preview.length} 行含同单多行记录</span>}
      </summary>
      <p>
        复算覆盖全表，下方展示前 {source.preview.length} 条记录。文件编码 {source.encoding ?? "未报告"}
        {typeof source.order_ids === "number" ? ` · 去重后订单数 ${source.order_ids}` : ""}
      </p>
      <div className="sales-audit-sha-row">
        <pre>SHA-256 {source.sha256}</pre>
        <button type="button" className="sales-audit-copy-btn" onClick={() => onCopyHash(source.sha256)}>
          {copiedHash === source.sha256 ? <Check size={13} /> : <Copy size={13} />}
          {copiedHash === source.sha256 ? "已复制" : "复制指纹"}
        </button>
      </div>
      <div className="sales-audit-table">
        <table>
          <thead>
            <tr>
              <th>记录</th>
              <th>order_id</th>
              <th className="is-num">sales（元）</th>
              <th className="is-num">quantity（件）</th>
            </tr>
          </thead>
          <tbody>
            {source.preview.map((row) => {
              const count = orderCounts.get(row.order_id) ?? 0;
              const isDup = count > 1;
              return (
                <tr key={row.record} className={isDup ? "is-duplicate-order" : undefined}>
                  <td className="is-mono">{row.record}</td>
                  <td>
                    <span className="sales-audit-oid">{row.order_id}</span>
                    {isDup && <span className="sales-audit-dup-badge">同单合并（{count} 行）</span>}
                  </td>
                  <td className="is-num">{row.sales}</td>
                  <td className="is-num">{row.quantity}</td>
                </tr>
              );
            })}
          </tbody>
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
              <Download size={16} />下载简报 (.md)
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
  sessionId: string;
  audit: AuditResult;
  receipt: ConfirmReceipt | null;
  draftTrace: string | null;
  copiedHash: string | null;
  copyHint: string;
  onCopyHash: (hash: string) => void;
}

// 告警、账本写入、发布回执与按需展开的哈希链实时视图。
function ResultNotices({
  sessionId, audit, receipt, draftTrace, copiedHash, copyHint, onCopyHash,
}: ResultNoticesProps) {
  const receiptRef = useRef<HTMLElement>(null);
  const [chainOpen, setChainOpen] = useState(false);
  const [chainLoading, setChainLoading] = useState(false);
  const [chainError, setChainError] = useState("");
  const [chainData, setChainData] = useState<LedgerChainResponse | null>(null);
  const [signatureResult, setSignatureResult] = useState<{
    valid: boolean;
    status?: "signed" | "partial" | "unsigned";
    chain_valid?: boolean;
    signed?: number;
    errors?: string[];
    note?: string;
  } | null>(null);
  const [signatureBusy, setSignatureBusy] = useState(false);

  useEffect(() => {
    if (receipt) receiptRef.current?.focus({ preventScroll: true });
  }, [receipt]);

  // 每次有新回执或新复算结果时，若账本抽屉正展开则清空旧缓存以便刷新
  useEffect(() => {
    setChainData(null);
    setChainError("");
    setSignatureResult(null);
  }, [audit.audit_id, receipt?.seq]);

  async function toggleLedgerChain() {
    if (chainOpen) {
      setChainOpen(false);
      return;
    }
    setChainOpen(true);
    setChainLoading(true);
    setChainError("");
    try {
      const res = await api<LedgerChainResponse>(`/api/sessions/${sessionId}/sales-audit/ledger?limit=8`);
      setChainData(res);
    } catch (err) {
      setChainError(err instanceof Error ? err.message : "读取账本链失败");
    } finally {
      setChainLoading(false);
    }
  }

  async function checkSignature(mode: "signature-check" | "signature-tamper-demo") {
    setSignatureBusy(true);
    setSignatureResult(null);
    try {
      const res = await api<{
        valid: boolean;
        status?: "signed" | "partial" | "unsigned";
        chain_valid?: boolean;
        signed?: number;
        errors?: string[];
        note?: string;
      }>(
        `/api/sessions/${sessionId}/sales-audit/${mode}`,
      );
      setSignatureResult(res);
    } catch (err) {
      setSignatureResult({
        valid: false,
        errors: [err instanceof Error ? err.message : "验签失败"],
      });
    } finally {
      setSignatureBusy(false);
    }
  }

  const warnings = audit.warnings ?? [];
  const ledger = audit.ledger;
  const trace = audit.draft_trace_sha256 ?? draftTrace;
  return (
    <>
      {trace && (
        <p className="sales-audit-trace">已绑定起草轨迹 {trace.slice(0, 12)}…</p>
      )}
      {audit.policy && (
        <p className="sales-audit-trace">
          按主体 {audit.policy.principal_id} 核验
          {audit.policy.rls_column ? ` · 行级过滤 ${audit.policy.rls_column}` : ""}
          {audit.policy.pii_columns.length ? ` · PII 脱敏 ${audit.policy.pii_columns.length} 列` : ""}
        </p>
      )}
      {audit.lineage && (
        <p className="sales-audit-trace">
          来源 {audit.lineage.upstream_table ?? audit.lineage.catalog_urn ?? "未命名上游"}
          {audit.lineage.owner ? ` · ${audit.lineage.owner}` : ""}
          {` · 抽取于 ${audit.lineage.extracted_at.slice(0, 16).replace("T", " ")}`}
          {audit.lineage.stale ? " · 已超过新鲜度 SLA" : " · 新鲜度正常"}
        </p>
      )}
      {audit.schema_fingerprint && (
        <p className="sales-audit-trace" title={audit.schema_fingerprint}>
          核验口径指纹 {audit.schema_fingerprint.slice(0, 12)}… · {audit.schema_status === "approved" ? "已批准" : "草稿"}
        </p>
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
          <span>复算记录 #{ledger.seq}</span>
          <code title={ledger.hash}>{ledger.hash.slice(0, 16)}…</code>
          <button type="button" onClick={() => onCopyHash(ledger.hash)}>
            {copiedHash === ledger.hash ? <Check size={14} /> : <Copy size={14} />}
            {copiedHash === ledger.hash ? "已复制" : "复制 hash"}
          </button>
          <button type="button" className="sales-audit-chain-toggle" onClick={() => void toggleLedgerChain()}>
            <Link2 size={13} />
            {chainOpen ? "收起哈希链" : "查看哈希链账本"}
            {chainOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          </button>
          {copyHint && <small>{copyHint}</small>}
        </div>
      )}
      {audit.ledger_error && (
        <p className="sales-audit-error">审计账本写入失败：{audit.ledger_error}。本次核验结果仍然有效。</p>
      )}
      {receipt && (
        <section className="sales-audit-receipt" role="status" ref={receiptRef} tabIndex={-1}>
          <div className="sales-audit-receipt-head">
            <strong>确认记录</strong>
            <button type="button" className="sales-audit-chain-toggle" onClick={() => void toggleLedgerChain()}>
              <Link2 size={13} />
              {chainOpen ? "收起链式存证" : "实时核验哈希链"}
              {chainOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
          </div>
          <dl>
            <div><dt>序号</dt><dd>#{receipt.seq}</dd></div>
            <div><dt>时间</dt><dd>{formatReceiptTime(receipt.ts)}</dd></div>
            <div><dt>hash</dt><dd><code>{receipt.hash.slice(0, 16)}</code></dd></div>
          </dl>
          <small>服务端按原始 CSV 重算后写入；可在核验明细中核对。</small>
        </section>
      )}
      {chainOpen && (
        <section className="sales-audit-chain" aria-label="审计哈希链账本">
          <div className="sales-audit-chain-head">
            <span className="sales-audit-chain-title">
              <Link2 size={14} />
              审计哈希链账本（audit_ledger.jsonl）
            </span>
            {chainData && (
              <span className={`sales-audit-chain-status ${chainData.verification.valid ? "is-valid" : "is-broken"}`}>
                {chainData.verification.valid
                  ? `整链校验通过 · valid=true · 共 ${chainData.verification.entries} 条` +
                    (chainData.verification.signed
                      ? ` · Ed25519 已签名 ${chainData.verification.signed} 条`
                      : "")
                  : "链校验失败"}
              </span>
            )}
          </div>
          <div className="sales-audit-signature-actions">
            <button type="button" disabled={signatureBusy} onClick={() => void checkSignature("signature-check")}>
              校验签章
            </button>
            <button type="button" disabled={signatureBusy} onClick={() => void checkSignature("signature-tamper-demo")}>
              测试篡改拦截
            </button>
            {signatureResult && (
              <span
                className={`sales-audit-signature-result ${signatureResult.valid ? "is-valid" : "is-broken"}`}
                role="status"
              >
                {signatureResult.valid
                  ? `签名有效${signatureResult.signed ? ` · ${signatureResult.signed} 条` : ""}`
                  : signatureResult.status === "unsigned"
                    ? "账本尚未签名（历史会话）"
                    : signatureResult.status === "partial"
                      ? `部分记录已签名${signatureResult.signed ? ` · ${signatureResult.signed} 条` : ""}`
                      : `签名无效${signatureResult.errors?.[0] ? ` · ${signatureResult.errors[0]}` : ""}`}
                {signatureResult.note ? `（${signatureResult.note}）` : ""}
              </span>
            )}
          </div>
          {chainLoading && <p className="sales-audit-chain-loading"><LoaderCircle size={14} className="spin" />正在从服务端读取并校验哈希链…</p>}
          {chainError && <p className="sales-audit-error">{chainError}</p>}
          {chainData && chainData.entries.length > 0 && (
            <ol className="sales-audit-chain-list">
              {chainData.entries.map((entry, idx) => (
                <li key={entry.seq} className={`sales-audit-chain-node is-${entry.kind}`}>
                  <div className="sales-audit-chain-meta">
                    <span className="sales-audit-chain-seq">#{entry.seq}</span>
                    <span className="sales-audit-chain-kind">{entry.kind === "confirm" ? "服务端确认" : "独立复算"}</span>
                    <time>{formatReceiptTime(entry.ts)}</time>
                  </div>
                  <div className="sales-audit-chain-hashes">
                    <span><label>prev</label><code title={entry.prev}>{entry.prev.slice(0, 16)}…</code></span>
                    <span className="sales-audit-chain-arrow" aria-hidden="true">→</span>
                    <span><label>hash</label><code title={entry.hash}>{entry.hash.slice(0, 16)}…</code></span>
                  </div>
                  {entry.signature && (
                    <div className="sales-audit-chain-signature">
                      Ed25519 · {entry.signature_key_id?.slice(0, 8) ?? "已签名"}
                    </div>
                  )}
                  {idx < chainData.entries.length - 1 && <div className="sales-audit-chain-connector" aria-hidden="true" />}
                </li>
              ))}
            </ol>
          )}
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
  const [quickFailed, setQuickFailed] = useState(false);
  const [recordedNote, setRecordedNote] = useState(false);
  const [schemaSpecs, setSchemaSpecs] = useState<AuditSchemaSpec[]>([]);
  const [schemaText, setSchemaText] = useState(`{
  "metrics": []
}`);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [schemaLoaded, setSchemaLoaded] = useState(false);
  const [schemaHistory, setSchemaHistory] = useState<{ versions: number; consistent: boolean }>({ versions: 0, consistent: true });
  const [schemaMessage, setSchemaMessage] = useState("");
  const [schemaStatus, setSchemaStatus] = useState<{ status: string | null; approvedBy: string | null }>({ status: null, approvedBy: null });
  const [approver, setApprover] = useState("");
  const [lineageText, setLineageText] = useState("{}");
  const [lineageLoaded, setLineageLoaded] = useState(false);
  const [lineageBusy, setLineageBusy] = useState(false);
  const [lineageMessage, setLineageMessage] = useState("");
  const [policyText, setPolicyText] = useState("{}");
  const [policyLoaded, setPolicyLoaded] = useState(false);
  const [policyBusy, setPolicyBusy] = useState(false);
  const [policyMessage, setPolicyMessage] = useState("");
  const [draftTrace, setDraftTrace] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [sessionLost, setSessionLost] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [recoveryNote, setRecoveryNote] = useState("");
  const [copiedHash, setCopiedHash] = useState<string | null>(null);
  const [copyHint, setCopyHint] = useState("");
  const draftInput = useRef<HTMLTextAreaElement>(null);
  const draftRef = useRef(draft);
  const recoveryRef = useRef<HTMLHeadingElement>(null);
  const focusDraftAfterConfirm = useRef(false);

  useEffect(() => {
    if (!confirming && focusDraftAfterConfirm.current) {
      focusDraftAfterConfirm.current = false;
      draftInput.current?.focus({ preventScroll: true });
    }
  }, [confirming]);

  useEffect(() => {
    if (sessionLost) recoveryRef.current?.focus({ preventScroll: true });
  }, [sessionLost]);

  function updateDraft(value: string, isDemo = false) {
    const normalized = value.replace(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/g, "\n");
    draftRef.current = normalized;
    setDraft(normalized);
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
    const submittedDraft = draftRef.current;
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
      if (draftRef.current !== submittedDraft) {
        setError("确认期间草稿已被修改，本次回执没有采用；请重新复算核验后再确认。");
        return;
      }
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
        setAudit(null);
        setReceipt(null);
        setServerBrief(null);
        setError("草稿或数据在核验后发生变化，请重新复算核验。");
        focusDraftAfterConfirm.current = true;
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
    const submittedDraft = draftRef.current;
    setQuickBusy(true);
    setError("");
    try {
      const response = await api<QuickDraftResult>(`/api/sessions/${sessionId}/sales-audit/quick-draft`, {
        method: "POST",
        timeoutMs: 60000,
      });
      if (draftRef.current !== submittedDraft) {
        setError("起草期间草稿已被修改，本次模型输出没有覆盖你的输入。");
        return;
      }
      updateDraft(response.draft);
      setDraftTrace(response.trace_sha256);
      setQuickDraft(response);
      setQuickFailed(false);
      setRecordedNote(false);
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
      setQuickFailed(true);
      setError(err instanceof Error ? err.message : "快速起草失败，请重试。");
    } finally {
      setQuickBusy(false);
    }
  }

  async function loadSchema() {
    if (schemaLoaded) return;
    setSchemaLoaded(true);
    try {
      const payload = await api<{
        metrics?: AuditSchemaSpec[];
        status?: string | null;
        approved_by?: string | null;
      }>(`/api/sessions/${sessionId}/audit-schema`);
      const metrics = Array.isArray(payload?.metrics) ? payload.metrics : [];
      setSchemaSpecs(metrics);
      setSchemaStatus({
        status: typeof payload?.status === "string" ? payload.status : null,
        approvedBy: typeof payload?.approved_by === "string" ? payload.approved_by : null,
      });
      setSchemaText(JSON.stringify({ metrics }, null, 2));
      try {
        const history = await api<{ entries?: unknown[]; consistent?: boolean }>(
          `/api/sessions/${sessionId}/audit-schema/history`,
        );
        setSchemaHistory({
          versions: Array.isArray(history.entries) ? history.entries.length : 0,
          consistent: history.consistent !== false,
        });
      } catch {
        // 历史不可用时不影响当前口径。
      }
    } catch {
      // 口径接口不可用时保持内置销售口径，不阻断核验。
    }
  }

  async function saveSchemaPayload(payload: { metrics: AuditSchemaSpec[] }) {
    setSchemaBusy(true);
    setSchemaMessage("");
    try {
      const saved = await api<{ metrics: AuditSchemaSpec[] }>(
        `/api/sessions/${sessionId}/audit-schema`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      setSchemaSpecs(saved.metrics);
      setSchemaStatus({ status: "draft", approvedBy: null });
      setSchemaText(JSON.stringify({ metrics: saved.metrics }, null, 2));
      setSchemaMessage(
        saved.metrics.length
          ? `已保存 ${saved.metrics.length} 项核验口径。`
          : "已恢复内置销售口径。",
      );
      setAudit(null);
      setReceipt(null);
      setServerBrief(null);
    } catch (err) {
      setSchemaMessage(err instanceof Error ? err.message : "核验口径保存失败。");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function approveSchema() {
    if (!approver.trim()) {
      setSchemaMessage("请填写批准人。");
      return;
    }
    setSchemaBusy(true);
    setSchemaMessage("");
    try {
      const saved = await api<{ metrics: AuditSchemaSpec[]; approved_by?: string }>(
        `/api/sessions/${sessionId}/audit-schema/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved_by: approver.trim() }),
        },
      );
      setSchemaSpecs(saved.metrics);
      setSchemaStatus({ status: "approved", approvedBy: saved.approved_by ?? approver.trim() });
      setSchemaMessage(`口径已由 ${saved.approved_by ?? approver.trim()} 批准。`);
      setAudit(null);
      setReceipt(null);
      setServerBrief(null);
    } catch (err) {
      setSchemaMessage(err instanceof Error ? err.message : "批准失败。");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function loadLineage() {
    if (lineageLoaded) return;
    setLineageLoaded(true);
    try {
      const payload = await api<{ lineage?: Record<string, unknown> | null }>(
        `/api/sessions/${sessionId}/audit-lineage`,
      );
      setLineageText(JSON.stringify(payload.lineage ?? {}, null, 2));
    } catch {
      // 血缘接口不可用时保持未登记状态。
    }
  }

  async function loadPolicy() {
    if (policyLoaded) return;
    setPolicyLoaded(true);
    try {
      const payload = await api<{ policy?: Record<string, unknown> | null }>(
        `/api/sessions/${sessionId}/audit-policy`,
      );
      setPolicyText(JSON.stringify(payload.policy ?? {}, null, 2));
    } catch {
      // 策略接口不可用时保持未登记状态。
    }
  }

  async function savePolicy() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(policyText) as Record<string, unknown>;
    } catch {
      setPolicyMessage("JSON 格式不正确，请检查逗号与引号。");
      return;
    }
    setPolicyBusy(true);
    setPolicyMessage("");
    try {
      const saved = await api<{ policy: { principal_id: string } }>(
        `/api/sessions/${sessionId}/audit-policy`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(parsed),
        },
      );
      setPolicyMessage(`访问策略已保存：${saved.policy.principal_id}。`);
      setAudit(null);
      setReceipt(null);
      setServerBrief(null);
    } catch (err) {
      setPolicyMessage(err instanceof Error ? err.message : "访问策略保存失败。");
    } finally {
      setPolicyBusy(false);
    }
  }

  async function saveLineage() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(lineageText) as Record<string, unknown>;
    } catch {
      setLineageMessage("JSON 格式不正确，请检查逗号与引号。");
      return;
    }
    setLineageBusy(true);
    setLineageMessage("");
    try {
      const saved = await api<{ lineage: { upstream_table?: string | null } }>(
        `/api/sessions/${sessionId}/audit-lineage`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(parsed),
        },
      );
      setLineageMessage(
        `来源血缘已保存：${saved.lineage.upstream_table ?? "已登记"}。`,
      );
      setAudit(null);
      setReceipt(null);
      setServerBrief(null);
    } catch (err) {
      setLineageMessage(err instanceof Error ? err.message : "来源血缘保存失败。");
    } finally {
      setLineageBusy(false);
    }
  }

  function saveSchema() {
    let parsed: { metrics?: AuditSchemaSpec[] };
    try {
      parsed = JSON.parse(schemaText) as { metrics?: AuditSchemaSpec[] };
    } catch {
      setSchemaMessage("JSON 格式不正确，请检查逗号与引号。");
      return;
    }
    void saveSchemaPayload({ metrics: parsed.metrics ?? [] });
  }

  function applyRecordedDraft() {
    updateDraft(RECORDED_DRAFT, demoDraft);
    setDraftTrace(null);
    setQuickDraft(null);
    setQuickFailed(false);
    setRecordedNote(true);
    setError("");
  }

  async function reloadSample() {
    setReloading(true);
    setRecoveryNote("");
    try {
      const created = await api<{ id: string }>("/api/sessions/sample?audit=true", { method: "POST" });
      if (onReloadSession) onReloadSession(created.id);
      else setRecoveryNote(`示例会话已创建（${created.id}），请从左侧历史会话中打开。`);
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
  const evidenceView = audit && (
    <SourceEvidence
      source={audit.source}
      copiedHash={copiedHash}
      onCopyHash={(hash) => void copyHash(hash)}
    />
  );
  const noticesView = audit && (
    <ResultNotices
      sessionId={sessionId}
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

  const activeMetricNames = schemaSpecs.length
    ? schemaSpecs.map((spec) => spec.name)
    : ["销售额合计", "销量合计", "订单数", "客单价"];

  if (sessionLost) {
    return (
      <div className="sales-audit">
        <section className="sales-audit-recovery" role="alert">
          <h2 ref={recoveryRef} tabIndex={-1}>会话已失效</h2>
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
          quickBusy={quickBusy}
          quickDraft={quickDraft}
          quickFailed={quickFailed}
          recordedNote={recordedNote}
          onUseRecorded={applyRecordedDraft}
          canRun={Boolean(draft.trim()) && !tooLong}
          error={tooLong ? `草稿最多 ${MAX_DRAFT_LENGTH} 字符，请在工作台删减后重试。` : error}
          evidence={evidenceView}
          brief={briefView}
          details={noticesView}
          receipt={receipt}
          briefText={serverBrief}
          onRun={() => void runAudit()}
          onConfirm={() => void confirmBrief()}
          onQuickDraft={() => void runQuickDraft()}
          onDownloadEvidence={downloadAudit}
          onDownloadBrief={downloadBrief}
          onDraftChange={(value) => updateDraft(value, demoDraft)}
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
          <p>核对销售额、销量、订单数和客单价。确认后由服务端重算并记录。</p>
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
          <details
            className="sales-audit-schema"
            onToggle={(event) => {
              if (event.currentTarget.open) void loadSchema();
            }}
          >
            <summary>
              核验口径 · {schemaSpecs.length ? `${schemaSpecs.length} 项自定义` : "内置销售四项"}
              {schemaStatus.status === "approved" ? " · 已批准" : schemaStatus.status === "draft" ? " · 草稿" : ""}
              {schemaHistory.versions > 0 && ` · 历史 ${schemaHistory.versions} 版`}
              {!schemaHistory.consistent && " · 历史指纹异常"}
            </summary>
            <p>
              核验器按登记口径独立读取原始 CSV 复算。支持 sum / count / count_distinct / min / max / mean，
              以及用两个已登记指标定义 ratio；可声明 data_type、null_policy、rounding、sign_policy、
              filters 与 time_window。留空使用内置的销量、订单数、销售额与客单价口径。
            </p>
            <textarea
              aria-label="核验口径 JSON"
              placeholder={`{
  "metrics": [
    {"name": "收入合计", "column": "sales", "aggregation": "sum", "unit": "元"}
  ]
}`}
              value={schemaText}
              onChange={(event) => setSchemaText(event.target.value)}
              rows={8}
              disabled={schemaBusy}
            />
            <div className="sales-audit-actions">
              <button type="button" disabled={schemaBusy} onClick={() => saveSchema()}>
                {schemaBusy ? "保存中…" : "保存口径"}
              </button>
              <button
                type="button"
                disabled={schemaBusy}
                onClick={() => void saveSchemaPayload({ metrics: [] })}
              >
                恢复内置口径
              </button>
              <button
                type="button"
                disabled={schemaBusy}
                onClick={() => setSchemaText(JSON.stringify({ metrics: schemaSpecs }, null, 2))}
              >
                撤销修改
              </button>
              <input
                className="sales-audit-approver"
                aria-label="批准人"
                placeholder="批准人"
                value={approver}
                onChange={(event) => setApprover(event.target.value)}
                disabled={schemaBusy || !schemaSpecs.length}
              />
              <button
                type="button"
                disabled={schemaBusy || !schemaSpecs.length || !approver.trim()}
                onClick={() => void approveSchema()}
              >
                批准口径
              </button>
            </div>
            {schemaMessage && <p role="status">{schemaMessage}</p>}
          </details>
          <details
            className="sales-audit-schema sales-audit-lineage"
            onToggle={(event) => {
              if (event.currentTarget.open) void loadLineage();
            }}
          >
            <summary>来源血缘 · {audit?.lineage?.upstream_table ?? "未登记"}</summary>
            <p>
              登记上游表、抽取时间、负责人与新鲜度 SLA。抽取时间必须带时区；
              超过 SLA 时核验结果会标记 source_stale，指纹写入账本并可用
              <code>--lineage</code> 离线校验。
            </p>
            <textarea
              aria-label="来源血缘 JSON"
              value={lineageText}
              onChange={(event) => setLineageText(event.target.value)}
              rows={7}
              disabled={lineageBusy}
            />
            <div className="sales-audit-actions">
              <button type="button" disabled={lineageBusy} onClick={() => void saveLineage()}>
                {lineageBusy ? "保存中…" : "保存来源血缘"}
              </button>
            </div>
            {lineageMessage && <p role="status">{lineageMessage}</p>}
          </details>
          <details
            className="sales-audit-schema sales-audit-policy"
            onToggle={(event) => {
              if (event.currentTarget.open) void loadPolicy();
            }}
          >
            <summary>访问策略 · {audit?.policy?.principal_id ?? "未登记"}</summary>
            <p>
              登记主体、claims、PII 脱敏列与行级权限。核验前按主体过滤行，
              证据预览中的 PII 列显示为「已脱敏」，主体与过滤指纹写入账本。
            </p>
            <textarea
              aria-label="访问策略 JSON"
              value={policyText}
              onChange={(event) => setPolicyText(event.target.value)}
              rows={8}
              disabled={policyBusy}
            />
            <div className="sales-audit-actions">
              <button type="button" disabled={policyBusy} onClick={() => void savePolicy()}>
                {policyBusy ? "保存中…" : "保存访问策略"}
              </button>
            </div>
            {policyMessage && <p role="status">{policyMessage}</p>}
          </details>
          <div className="sales-audit-grid">
            <form className="sales-audit-card" onSubmit={(event) => { event.preventDefault(); void runAudit(); }}>
              <h3><label htmlFor="sales-audit-draft">简报草稿</label></h3>
              <p id="sales-audit-format">
                统计范围为原始 CSV 全表。当前口径：{activeMetricNames.join(" / ")}。每行一个指标，格式为「名称：数值 单位」；范围限定或口径不明的行会标为待复核。
              </p>
              <p id="sales-audit-format-short" className="sales-audit-format-short">
                当前口径：{activeMetricNames.join(" / ")}。
              </p>
              {demoDraft && <p className="sales-audit-demo-note">示例草稿有 1 处错数、1 条无依据结论。</p>}
              <div className="sales-audit-actions">
                {(filename === "sample_sales.csv" || filename === "sample_sales_audit.csv") && (
                  <button type="button" disabled={busy || quickBusy || confirming} onClick={() => updateDraft(DEMO_DRAFT, true)}>填入示例草稿</button>
                )}
                {onPrepareDraft && <button type="button" disabled={busy || quickBusy || confirming} onClick={onPrepareDraft}>准备起草任务</button>}
                {report && (
                  <button type="button" disabled={busy || quickBusy || confirming} onClick={() => updateDraft(extractDraft(report))}>
                    使用分析报告草稿
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || quickBusy || confirming || tooLong}
                  onClick={() => void runQuickDraft()}
                >
                  {quickBusy ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
                  {quickBusy ? "模型正在起草…" : "用模型直接起草"}
                </button>
                {quickFailed && (
                  <button type="button" disabled={busy || quickBusy || confirming} onClick={applyRecordedDraft}>
                    填入赛前录制输出
                  </button>
                )}
              </div>
              <textarea
                ref={draftInput}
                id="sales-audit-draft"
                aria-describedby="sales-audit-format sales-audit-format-short"
                aria-invalid={tooLong || undefined}
                value={draft}
                onChange={(event) => updateDraft(event.target.value)}
                placeholder={"销售额合计：… 元\n销量合计：… 件\n订单数：… 笔"}
                rows={7}
                disabled={busy || quickBusy || confirming}
              />
              {recordedNote && (
                <p className="sales-audit-recorded" role="status">
                  已填入赛前录制的模型输出，非现场生成；仅用于网络或额度不可用时继续演示。
                </p>
              )}
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
                <button className="primary" type="submit" disabled={busy || quickBusy || confirming || !draft.trim() || tooLong}>
                  {busy ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}
                  {busy ? "正在复算…" : "复算并核验"}
                </button>
              </div>
            </form>

            <section className="sales-audit-card" aria-label="核验结果">
              <h3>核验结果</h3>
              {!audit ? <p>核验后显示草稿原值、复算值与口径。</p> : (
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
                      <li key={claim.line} className={`sales-audit-claim is-${claim.status}`}>
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
                      <button type="button" onClick={() => void runAudit(true)}>确认按全表复算</button>
                    )}
                    {audit.corrected_brief && !confirmed && (
                      <button className="primary" type="button" disabled={confirming} onClick={() => void confirmBrief()}>
                        {confirming ? <LoaderCircle className="spin" size={16} /> : <FileCheck2 size={16} />}
                        {confirming
                          ? "正在确认…"
                          : audit.summary.conflict ? "确认并生成简报" : "生成简报"}
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
