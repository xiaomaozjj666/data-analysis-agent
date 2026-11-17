import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowRight, Check, CircleHelp, Download, FileCheck2, LoaderCircle, Maximize, Minimize, Pencil, RotateCcw, ShieldCheck } from "lucide-react";
import type { AuditResult } from "./SalesAudit";
import "./SalesAuditDemo.css";

interface SalesAuditDemoProps {
  draft: string;
  isExample: boolean;
  audit: AuditResult | null;
  busy: boolean;
  confirmed: boolean;
  canRun: boolean;
  error: string;
  evidence: ReactNode;
  brief: ReactNode;
  onRun: () => void;
  onConfirm: () => void;
  onDownloadEvidence: () => void;
  onDownloadBrief: () => void;
  onEdit: () => void;
  onReset: () => void;
}

function orderCount(text?: string) {
  const match = text?.match(/^\s*([0-9]+)\s*笔\s*$/);
  return match ? BigInt(match[1]) : null;
}

const formatCount = new Intl.NumberFormat("zh-CN");

export default function SalesAuditDemo({
  draft, isExample, audit, busy, confirmed, canRun, error, evidence, brief,
  onRun, onConfirm, onDownloadEvidence, onDownloadBrief, onEdit, onReset,
}: SalesAuditDemoProps) {
  const stage = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const status = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [fullscreenError, setFullscreenError] = useState(false);
  const orderClaims = audit?.claims.filter(claim => claim.metric === "订单数") ?? [];
  const orderClaim = orderClaims.length === 1 ? orderClaims[0] : undefined;
  const order = orderClaim?.status === "unsupported" ? undefined : orderClaim;
  const previewLine = isExample ? draft.split("\n").find(line => /^订单数[：:]/.test(line)) : draft;
  const previewOrder = previewLine?.match(/^订单数[：:]([^\n]+)\n*$/)?.[1];
  const submitted = orderCount(audit ? orderClaim?.submitted : previewOrder);
  const recalculated = orderCount(order?.recalculated);
  const difference = submitted !== null && recalculated !== null ? submitted - recalculated : null;
  const magnitude = difference === null || difference >= 0n ? difference : -difference;
  const largest = submitted !== null && recalculated !== null
    ? (submitted > recalculated ? submitted : recalculated) : null;
  const width = (value: bigint | null) => largest && value !== null ? Number(value * 1000n / largest) / 10 : 0;
  const held = audit?.claims.filter(claim => claim.status === "unsupported") ?? [];
  const additionalConflicts = audit?.claims.filter(claim => claim.status === "conflict" && claim !== order) ?? [];
  const quantity = audit?.claims.find(claim => claim.metric === "销量合计" && claim.status === "verified");

  useEffect(() => {
    heading.current?.focus();
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
            <h2 ref={heading} tabIndex={-1}>这份简报里的订单数，算对了吗？</h2>
            {isExample && <p>合成订单明细 · 草稿含预设错误</p>}
          </div>

          <div className="sales-showcase-compare" aria-live="polite">
            <div className="sales-showcase-figure sales-showcase-draft">
              <span>简报草稿</span>
              <strong>{submitted === null ? "—" : formatCount.format(submitted)}
                {submitted !== null && <small>笔</small>}
              </strong>
              <p>{orderClaim ? `草稿第 ${orderClaim.line} 行`
                : submitted !== null ? "草稿中的订单数" : "核验时读取可识别的指标行"}</p>
              {largest !== null && <div className="sales-showcase-meter" aria-hidden="true"><span style={{ width: `${width(submitted)}%` }} /></div>}
            </div>
            <ArrowRight size={28} className="sales-showcase-arrow" aria-hidden="true" />
            <div className="sales-showcase-figure sales-showcase-verified">
              <span>原表独立复算</span>
              <strong>{recalculated === null ? "—" : formatCount.format(recalculated)}
                {recalculated !== null && <small>笔</small>}
              </strong>
              <p>{recalculated !== null ? `覆盖全表 ${audit?.source.rows} 条明细`
                : busy ? "正在读取原始 CSV" : audit ? "订单数待人工复核" : "运行核验后揭晓"}</p>
              {largest !== null && <div className="sales-showcase-meter" aria-hidden="true"><span style={{ width: `${width(recalculated)}%` }} /></div>}
            </div>
          </div>

          {additionalConflicts.length > 0 && (
            <div className="sales-showcase-other-conflicts">
              <h3>其他待确认的更正</h3>
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
              {confirmed ? <><Check size={18} />已确认生成核验版简报</> :
                difference !== null && magnitude !== null && difference !== 0n
                  ? <>草稿{difference > 0n ? "多报" : "少报"} <b>{formatCount.format(magnitude)}</b> 笔</>
                  : audit ? `${audit.summary.verified} 项已核验 · ${audit.summary.conflict} 项冲突 · ${audit.summary.unsupported} 项待复核` : null}
            </div>
            <div className="sales-showcase-tools">
              {audit && (
                <button type="button" onClick={() => advance(onReset)} disabled={busy}>
                  <RotateCcw size={15} />重开演示
                </button>
              )}
              {!audit && (
                <button className="sales-showcase-primary" type="button" onClick={() => advance(onRun)} disabled={busy || !canRun}>
                  {busy ? <LoaderCircle size={17} className="spin" /> : <ShieldCheck size={17} />}
                  {busy ? "正在复算…" : "复算并核验"}
                </button>
              )}
              {audit?.corrected_brief && !confirmed && (
                <button className="sales-showcase-primary" type="button" onClick={() => advance(onConfirm)}>
                  <FileCheck2 size={17} />{audit.summary.conflict ? "确认更正并生成简报" : "生成核验版简报"}
                </button>
              )}
              {confirmed && (
                <button className="sales-showcase-primary" type="button" onClick={onDownloadBrief}>
                  <Download size={17} />下载简报
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
                  <h3><ShieldCheck size={17} />订单数怎样算出来</h3>
                  <p>{order.formula}</p>
                  {quantity?.recalculated && <small>同一份原表的销量为 {quantity.recalculated}，按 quantity 求和。</small>}
                </article>
              )}
              {held.length > 0 && (
                <article className="sales-audit-card">
                  <h3><CircleHelp size={17} />待人工复核 {held.length} 项</h3>
                  {held.length === 1 && <p>第 {held[0].line} 行 · {held[0].text}</p>}
                  <small>这些内容保留在核验明细里。</small>
                </article>
              )}
              {audit.needs_scope_confirmation && (
                <article className="sales-audit-card">
                  <h3>先确认统计范围</h3>
                  <p>草稿带有标题或说明，可在草稿工作台确认全表范围后复算。</p>
                </article>
              )}
            </div>
            {evidence}
            {brief}
            <div className="sales-audit-actions">
              <button type="button" onClick={onDownloadEvidence}><Download size={16} />下载核验明细</button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
