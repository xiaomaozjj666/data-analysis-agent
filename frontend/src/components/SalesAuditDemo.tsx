import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, CircleHelp, Download, FileCheck2, LoaderCircle, Maximize, Minimize, Pencil, RotateCcw, ShieldCheck } from "lucide-react";
import type { AuditResult, ConfirmReceipt } from "./SalesAudit";
import "./SalesAuditDemo.css";

interface SalesAuditDemoProps {
  draft: string;
  isExample: boolean;
  audit: AuditResult | null;
  busy: boolean;
  confirming: boolean;
  confirmed: boolean;
  canRun: boolean;
  error: string;
  evidence: ReactNode;
  brief: ReactNode;
  details?: ReactNode;
  receipt?: ConfirmReceipt | null;
  briefText?: string | null;
  onRun: () => void;
  onConfirm: () => void;
  onDownloadEvidence: () => void;
  onDownloadBrief: () => void;
  onDraftChange?: (value: string) => void;
  onEdit: () => void;
  onReset: () => void;
}

function orderCount(text?: string) {
  const match = text?.match(/^\s*([0-9]+)\s*笔\s*$/);
  return match ? BigInt(match[1]) : null;
}

const formatCount = new Intl.NumberFormat("zh-CN");

// 直接显示真实值：路演容器里滚动动画容易停在起点（0 笔），对核验页是错误信息。
function Figure({ value }: { value: bigint | null }) {
  if (value === null) return <>—</>;
  return <>{formatCount.format(value)}</>;
}

async function sha256Hex(text: string): Promise<string | null> {
  if (!globalThis.crypto?.subtle) return null;
  const data = new TextEncoder().encode(text);
  const buffer = await globalThis.crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(buffer))
    .map(byte => byte.toString(16).padStart(2, "0"))
    .join("");
}

// 和服务端 confirm 写入的 brief_sha256 用同一算法：改一个空格，哈希就对不上。
function VerifyExport({ brief, expected }: { brief: string; expected: string }) {
  const [tampered, setTampered] = useState(false);
  const [state, setState] = useState<"idle" | "ok" | "bad" | "unsupported">("idle");
  const [actual, setActual] = useState("");

  async function check(text: string) {
    const digest = await sha256Hex(text);
    if (digest === null) {
      setState("unsupported");
      return;
    }
    setActual(digest);
    setState(digest === expected ? "ok" : "bad");
  }

  function toggleTamper() {
    const next = !tampered;
    setTampered(next);
    void check(next ? `${brief} ` : brief);
  }

  return (
    <section className="sales-showcase-verify" aria-label="导出文件校验">
      <div className="sales-showcase-verify-head">
        <h3>导出校验</h3>
        <small>与 scripts/verify_audit_ledger.py 使用同一 SHA-256 比对</small>
      </div>
      <p className="sales-showcase-verify-hash">
        <span>回执 {expected.slice(0, 16)}…</span>
        <span>{actual ? `当前 ${actual.slice(0, 16)}…` : "尚未校验"}</span>
      </p>
      <div className="sales-showcase-tools">
        <button type="button" onClick={() => void check(brief)}>校验简报</button>
        <button type="button" onClick={toggleTamper}>
          {tampered ? "还原这 1 个字节" : "改动 1 个字节"}
        </button>
      </div>
      <p className={`sales-showcase-verify-state is-${state}`} role="status">
        {state === "ok" && <><Check size={15} />内容与回执一致</>}
        {state === "bad" && <>内容与回执不一致，导出文件已被改动</>}
        {state === "unsupported" && <>当前浏览器不支持 Web Crypto，请在本地运行离线脚本</>}
        {state === "idle" && <>当前简报 {brief.length} 个字符</>}
      </p>
    </section>
  );
}

export default function SalesAuditDemo({
  draft, isExample, audit, busy, confirming, confirmed, canRun, error, evidence, brief, details,
  receipt, briefText, onRun, onConfirm, onDownloadEvidence, onDownloadBrief, onDraftChange, onEdit, onReset,
}: SalesAuditDemoProps) {
  const stage = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const status = useRef<HTMLDivElement>(null);
  const editor = useRef<HTMLTextAreaElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [fullscreenError, setFullscreenError] = useState(false);
  const [editing, setEditing] = useState(false);
  const orderClaims = audit?.claims.filter(claim => claim.metric === "订单数") ?? [];
  const orderClaim = orderClaims.length === 1 ? orderClaims[0] : undefined;
  const order = orderClaim?.status === "unsupported" ? undefined : orderClaim;
  const previewLine = isExample ? draft.split("\n").find(line => /^订单数[：:]/.test(line)) : draft;
  const previewOrder = previewLine?.match(/^订单数[：:]([^\n]+)\n*$/)?.[1];
  const submitted = orderCount(audit ? orderClaim?.submitted : previewOrder);
  const recalculated = orderCount(order?.recalculated);
  const difference = submitted !== null && recalculated !== null ? submitted - recalculated : null;
  const held = audit?.claims.filter(claim => claim.status === "unsupported") ?? [];
  const additionalConflicts = audit?.claims.filter(claim => claim.status === "conflict" && claim !== order) ?? [];
  const quantity = audit?.claims.find(claim => claim.metric === "销量合计" && claim.status === "verified");
  const title = recalculated !== null
    ? `订单数：${formatCount.format(submitted ?? 0n)} → ${formatCount.format(recalculated)}`
    : submitted !== null
      ? `订单数：${formatCount.format(submitted)}，待复算`
      : "核对订单数";

  useEffect(() => {
    // 仅在舞台实际可见时抢焦点，避免会话恢复/面板隐藏时把焦点拉走。
    if (stage.current?.getClientRects().length) heading.current?.focus();
    function updateFullscreen() {
      setFullscreen(document.fullscreenElement === stage.current);
    }
    document.addEventListener("fullscreenchange", updateFullscreen);
    return () => document.removeEventListener("fullscreenchange", updateFullscreen);
  }, []);

  async function toggleFullscreen() {
    setFullscreenError(false);
    try {
      if (fullscreen) await document.exitFullscreen();
      else await stage.current?.requestFullscreen();
    } catch {
      setFullscreenError(true);
    }
  }

  async function editDraft() {
    if (document.fullscreenElement === stage.current) await document.exitFullscreen().catch(() => {});
    onEdit();
  }

  function toggleEditor() {
    setEditing((current) => !current);
    window.requestAnimationFrame(() => editor.current?.focus());
  }

  function advance(action: () => void) {
    status.current?.focus({ preventScroll: true });
    action();
  }

  return (
    <section className="sales-showcase" ref={stage} aria-label="有据经营简报路演">
      <div className="sales-showcase-layout">
        <div className="sales-showcase-hero">
          <header className="sales-showcase-bar">
            <span><ShieldCheck size={20} />有据经营简报</span>
            <div className="sales-showcase-tools">
              <button type="button" onClick={() => void editDraft()} disabled={busy}><Pencil size={15} />草稿工作台</button>
              {document.fullscreenEnabled && (
                <button type="button" onClick={() => void toggleFullscreen()}>
                  {fullscreen ? <Minimize size={15} /> : <Maximize size={15} />}
                  {fullscreen ? "退出全屏" : "全屏"}
                </button>
              )}
            </div>
          </header>

          <div className="sales-showcase-intro">
            <h2 ref={heading} tabIndex={-1}>{title}</h2>
            {isExample && <p>示例数据 · 草稿把销量合计当成了订单数</p>}
          </div>

          <nav className="sales-showcase-steps" aria-label="核验流程">
            {[
              { n: 1, label: "草稿", action: toggleEditor, enabled: Boolean(onDraftChange) },
              { n: 2, label: "复算", action: onRun, enabled: !audit && canRun && !busy },
              { n: 3, label: "确认", action: onConfirm, enabled: Boolean(audit?.corrected_brief) && !confirmed && !confirming },
              {
                n: 4,
                label: "验签",
                action: () => document.querySelector(".sales-showcase-verify")?.scrollIntoView({ block: "center", behavior: "smooth" }),
                enabled: confirmed,
              },
            ].map((step) => {
              const current = confirmed ? 4 : audit ? 3 : 1;
              return (
                <button
                  key={step.n}
                  type="button"
                  className={`sales-showcase-step${step.n < current ? " is-done" : ""}${step.n === current ? " is-active" : ""}`}
                  disabled={!step.enabled}
                  onClick={step.action}
                >
                  <span className="sales-showcase-step-dot">{step.n < current ? "✓" : step.n}</span>
                  {step.label}
                </button>
              );
            })}
          </nav>

          <div className="sales-showcase-compare" aria-live="polite">
            <div className="sales-showcase-figure sales-showcase-draft">
              <span>草稿</span>
              <strong><Figure value={submitted} />{submitted !== null && <small>笔</small>}</strong>
              <p>{orderClaim ? `第 ${orderClaim.line} 行` : submitted !== null ? "草稿中的订单数" : "从草稿中识别指标行"}</p>
            </div>

            <div className="sales-showcase-delta">
              {difference === null ? (
                <span className="sales-showcase-delta-pending">待复算</span>
              ) : difference === 0n ? (
                <span className="sales-showcase-delta-ok"><Check size={15} />一致</span>
              ) : (
                <span className="sales-showcase-delta-diff">
                  <b>{difference > 0n ? "−" : "+"}{formatCount.format(difference < 0n ? -difference : difference)}</b>
                  <small>笔</small>
                </span>
              )}
            </div>

            <div className="sales-showcase-figure sales-showcase-verified">
              <span>原始明细</span>
              {recalculated === null ? (
                <>
                  <strong className="is-pending">待复算</strong>
                  <p>按 COUNT(DISTINCT order_id) 计算</p>
                </>
              ) : (
                <>
                  <strong><Figure value={recalculated} />{<small>笔</small>}</strong>
                  <p>全表 {audit?.source.rows} 条明细</p>
                </>
              )}
            </div>
          </div>

          <div className="sales-showcase-editline">
            <button type="button" className="sales-showcase-link" onClick={toggleEditor} disabled={busy || !onDraftChange}>
              {editing ? "收起草稿" : "改一个数试试"}
            </button>
            <small>Ctrl / ⌘ + Enter 直接复算</small>
          </div>

          {editing && onDraftChange && (
            <textarea
              ref={editor}
              className="sales-showcase-editor"
              value={draft}
              disabled={busy || confirming}
              onChange={(event) => onDraftChange(event.target.value)}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && canRun && !busy) {
                  event.preventDefault();
                  advance(onRun);
                }
              }}
              rows={5}
              aria-label="简报草稿"
            />
          )}

          {additionalConflicts.length > 0 && (
            <div className="sales-showcase-other-conflicts">
              <h3>其他更正</h3>
              <ul>
                {additionalConflicts.map(claim => (
                  <li key={claim.line}>
                    <span>{claim.metric} · 第 {claim.line} 行</span>
                    <span>草稿 {claim.submitted} → 复算 <b>{claim.recalculated}</b></span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="sales-showcase-footer">
            <div className="sales-showcase-status" ref={status} tabIndex={-1} role="status">
              {confirmed ? <><Check size={18} />已生成简报</> :
                difference !== null && difference !== 0n
                  ? <>草稿{difference > 0n ? "多报" : "少报"} <b>{formatCount.format(difference < 0n ? -difference : difference)}</b> 笔</>
                  : audit ? `${audit.summary.verified} 项已核验 · ${audit.summary.conflict} 项冲突 · ${audit.summary.unsupported} 项待复核` : null}
            </div>
            <div className="sales-showcase-tools">
              {audit && (
                <button type="button" onClick={() => advance(onReset)} disabled={busy}>
                  <RotateCcw size={15} />重新演示
                </button>
              )}
              {!audit && (
                <button className="sales-showcase-primary" type="button" onClick={() => advance(onRun)} disabled={busy || !canRun}>
                  {busy ? <LoaderCircle size={17} className="spin" /> : <ShieldCheck size={17} />}
                  {busy ? "正在复算…" : "复算"}
                </button>
              )}
              {audit?.corrected_brief && !confirmed && (
                <button className="sales-showcase-primary" type="button" disabled={confirming} onClick={() => advance(onConfirm)}>
                  {confirming ? <LoaderCircle size={17} className="spin" /> : <FileCheck2 size={17} />}
                  {confirming ? "正在确认…" : "确认并生成简报"}
                </button>
              )}
              {confirmed && (
                <button className="sales-showcase-primary" type="button" onClick={onDownloadBrief}>
                  <Download size={17} />下载简报 (.md)
                </button>
              )}
            </div>
          </div>
          {error && <p className="sales-showcase-error" role="alert">{error}</p>}
          {fullscreenError && <p className="sales-showcase-error" role="status">浏览器暂时无法进入全屏。</p>}
        </div>

        {audit && (
          <div className="sales-showcase-results">
            <div className="sales-showcase-result-cards">
              {order?.formula && (
                <article className="sales-audit-card">
                  <h3><ShieldCheck size={17} />订单数口径</h3>
                  <p>{order.formula}</p>
                  {quantity?.recalculated && <small>同一份原表的销量为 {quantity.recalculated}，按 quantity 求和。</small>}
                </article>
              )}
              {held.length > 0 && (
                <article className="sales-audit-card">
                  <h3><CircleHelp size={17} />{held.length} 行未通过核验</h3>
                  {held.length === 1 && <p>第 {held[0].line} 行 · {held[0].text}</p>}
                  <small>保留在核验明细中，不进入发布。</small>
                </article>
              )}
              {audit.needs_scope_confirmation && (
                <article className="sales-audit-card">
                  <h3>需要先确认统计范围</h3>
                  <p>草稿含标题或说明，请先确认这些指标都描述全表。</p>
                </article>
              )}
            </div>
            {evidence}
            {details}
            {brief}
            {confirmed && briefText && receipt?.brief_sha256 && (
              <VerifyExport brief={briefText} expected={receipt.brief_sha256} />
            )}
            <div className="sales-audit-actions">
              <button type="button" onClick={onDownloadEvidence}><Download size={16} />下载核验明细 (.json)</button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
