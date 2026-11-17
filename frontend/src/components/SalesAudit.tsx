import { useRef, useState } from "react";
import { Download, FileCheck2, LoaderCircle, Presentation, ShieldCheck } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { api } from "../utils/api";
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

export interface AuditResult {
  source: {
    filename: string;
    sha256: string;
    rows: number;
    preview: { record: number; order_id: string; sales: string; quantity: string }[];
  };
  summary: Record<Verdict, number>;
  claims: {
    line: number;
    text: string;
    metric: string | null;
    status: Verdict;
    reason: string;
    submitted?: string;
    recalculated?: string;
    formula?: string;
  }[];
  needs_scope_confirmation: boolean;
  corrected_brief: string | null;
}

interface SalesAuditProps {
  sessionId: string;
  filename: string;
  initialDemo: boolean;
  report?: string;
  onPrepareDraft?: () => void;
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

function SourceEvidence({ source }: { source: AuditResult["source"] }) {
  return (
    <details className="sales-audit-card sales-audit-evidence">
      <summary>查看来源 · {source.filename} · 共 {source.rows} 条明细</summary>
      <p>复算覆盖全表，下方展示前 {source.preview.length} 条记录。</p>
      <pre>SHA-256 {source.sha256}</pre>
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
              <Download size={16} />下载简报
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

export default function SalesAudit({ sessionId, filename, initialDemo, report, onPrepareDraft }: SalesAuditProps) {
  const [draft, setDraft] = useState(initialDemo ? DEMO_DRAFT : "");
  const [demoDraft, setDemoDraft] = useState(initialDemo);
  const [showcase, setShowcase] = useState(initialDemo);
  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const draftInput = useRef<HTMLTextAreaElement>(null);

  function updateDraft(value: string, isDemo = false) {
    setDraft(value.replace(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/g, "\n"));
    setDemoDraft(isDemo);
    setAudit(null);
    setConfirmed(false);
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
    setConfirmed(false);
    try {
      const response = await api<AuditResult>(`/api/sessions/${sessionId}/sales-audit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft, confirm_full_table: confirmFullTable }),
      });
      setAudit(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "核验失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  const supportsCsv = filename.toLowerCase().endsWith(".csv");
  const tooLong = draft.length > MAX_DRAFT_LENGTH;
  const brief = confirmed ? audit?.corrected_brief : null;
  const evidenceView = audit && <SourceEvidence source={audit.source} />;
  const briefView = brief && <VerifiedBrief brief={brief} onDownload={showcase ? undefined : downloadBrief} />;

  function downloadBrief() {
    if (brief) download("经营简报-核验版.md", brief, "text/markdown;charset=utf-8");
  }

  function downloadAudit() {
    if (!audit) return;
    download("sales-audit.json", JSON.stringify({
      ...audit,
      corrected_brief: brief ?? null,
      brief_confirmed: Boolean(brief),
    }, null, 2), "application/json");
  }

  if (showcase && supportsCsv) {
    return (
      <div className="sales-audit">
        <SalesAuditDemo
          draft={draft}
          isExample={demoDraft}
          audit={audit}
          busy={busy}
          confirmed={confirmed}
          canRun={Boolean(draft.trim()) && !tooLong}
          error={tooLong ? `草稿最多 ${MAX_DRAFT_LENGTH} 字符，请在工作台删减后重试。` : error}
          evidence={evidenceView}
          brief={briefView}
          onRun={() => void runAudit()}
          onConfirm={() => setConfirmed(true)}
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
              </div>
              <textarea
                ref={draftInput}
                id="sales-audit-draft"
                aria-describedby="sales-audit-format"
                value={draft}
                onChange={(event) => updateDraft(event.target.value)}
                placeholder={"销售额合计：… 元\n销量合计：… 件\n订单数：… 笔"}
                rows={7}
                disabled={busy}
              />
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
                      <button className="primary" type="button" onClick={() => setConfirmed(true)}>
                        <FileCheck2 size={16} />
                        {audit.summary.conflict ? "确认更正并生成简报" : "生成核验版简报"}
                      </button>
                    )}
                    <button type="button" onClick={downloadAudit}>
                      <Download size={16} />下载核验明细
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
