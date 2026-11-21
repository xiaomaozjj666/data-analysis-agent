import React, { useCallback, useRef, useState } from "react";
import { Braces, FileSpreadsheet, LoaderCircle, ShieldCheck, Sparkles, Table2, Upload } from "lucide-react";
import { motion } from "motion/react";

interface EmptyWorkspaceProps {
  uploading: boolean;
  // 上传进度百分比（0-100）；null 表示无可展示进度（未在上传或长度不可计）
  uploadProgress?: number | null;
  onUpload: () => void;
  onFileDrop: (file: File) => void;
  // 取消进行中的上传：大文件上传耗时长，给用户反悔的机会
  onCancelUpload?: () => void;
}

// 支持的文件扩展名，需与后端 data_agent.workspace.SUPPORTED_EXTENSIONS 及
// App.tsx 文件输入的 accept 保持一致：表格/文本/PDF/Word 之外还有 SQLite
// （.db/.sqlite/.sqlite3，上传后默认载入行数最多的表）。
const SUPPORTED_EXTENSIONS = [
  ".csv",
  ".tsv",
  ".xlsx",
  ".xls",
  ".json",
  ".jsonl",
  ".parquet",
  ".db",
  ".sqlite",
  ".sqlite3",
  ".pdf",
  ".txt",
  ".docx",
];
function isSupportedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return SUPPORTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

function EmptyWorkspace({ uploading, uploadProgress, onUpload, onFileDrop, onCancelUpload }: EmptyWorkspaceProps) {
  // 鼠标跟随光斑：跟踪鼠标在整个工作区的相对位置，更新 CSS 变量，
  // 由 empty-state.css 的 radial-gradient 渲染全屏柔和光晕。
  const sectionRef = useRef<HTMLElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);
  // Magnetic button: track mouse offset for subtle pull effect
  const btnRef = useRef<HTMLButtonElement>(null);
  const handleBtnMouseMove = useCallback((e: React.MouseEvent<HTMLButtonElement>) => {
    const btn = btnRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const x = (e.clientX - rect.left - rect.width / 2) * 0.08;
    const y = (e.clientY - rect.top - rect.height / 2) * 0.08;
    btn.style.transform = `translate(${x}px, ${y}px)`;
  }, []);
  const handleBtnMouseLeave = useCallback(() => {
    const btn = btnRef.current;
    if (btn) btn.style.transform = "translate(0, 0)";
  }, []);
  const handleMouseMove = useCallback((event: React.MouseEvent<HTMLElement>) => {
    const el = sectionRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 100;
    const y = ((event.clientY - rect.top) / rect.height) * 100;
    el.style.setProperty("--mouse-x", `${x}%`);
    el.style.setProperty("--mouse-y", `${y}%`);
  }, []);

  const handleDragEnter = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current += 1;
    if (e.dataTransfer?.types?.includes("Files")) setDragOver(true);
  }, []);
  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current -= 1;
    if (dragCounter.current <= 0) { dragCounter.current = 0; setDragOver(false); }
  }, []);
  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => { e.preventDefault(); e.stopPropagation(); }, []);
  // 多文件拖入：首个支持文件走主上传流程，其余支持文件通过自定义事件
  // 交给 App.tsx 作为额外会话处理，避免阻塞主数据集的加载。
  const handleDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current = 0;
    setDragOver(false);
    const dropped = Array.from(e.dataTransfer?.files ?? []);
    const supported = dropped.filter(isSupportedFile);
    if (supported.length === 0) return;
    const [first, ...rest] = supported;
    onFileDrop(first);
    if (rest.length > 0) {
      window.dispatchEvent(new CustomEvent("empty-workspace:extra-files", { detail: { files: rest } }));
    }
  }, [onFileDrop]);

  // 整个工作区都可点击触发上传：点击空白区域等价于点击主按钮，
  // 让用户无需精准瞄准按钮即可发起分析。点击按钮自身时由按钮处理，
  // 这里通过 closest("button") 排除，避免重复弹出文件选择框。
  // 另排除"正在选择文字"的点击：落地页文案是可选中的，用户拖选一句话
  // 松开鼠标就弹系统文件框（而且系统框会打断选择）非常恼人。
  const handleSectionClick = useCallback((e: React.MouseEvent<HTMLElement>) => {
    if (uploading) return;
    if ((e.target as HTMLElement).closest("button")) return;
    const selection = window.getSelection?.();
    if (selection && selection.toString().trim().length > 0) return;
    onUpload();
  }, [onUpload, uploading]);

  // 键盘可达性：整块区域不再声明 role="button"（避免把内部真实按钮
  // 吞进 button 的语义树，导致读屏无法识别"体验简报核验"等按钮）。
  // 键盘用户通过区域内的原生按钮完成上传 / 示例载入，Enter / Space 由
  // 浏览器对按钮的默认行为处理。鼠标仍可点击空白区域快速上传。

  // 演示数据快速开始：通知 App.tsx 调用 /api/sessions/sample?kind=...
  const handleLoadSample = useCallback((kind: string, auditDemo: boolean) => {
    window.dispatchEvent(
      new CustomEvent("empty-workspace:load-sample", { detail: { kind, auditDemo } }),
    );
  }, []);

  return (
    <section
      ref={sectionRef}
      className={`empty-workspace${dragOver ? " is-drag-over" : ""}`}
      role="region"
      aria-label="上传数据文件开始分析"
      aria-describedby="empty-workspace-help"
      aria-busy={uploading}
      onMouseMove={handleMouseMove}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={handleSectionClick}
    >
      <p id="empty-workspace-help" className="sr-only">
        按 Tab 选择“选择数据文件”上传数据集，或选择“体验简报核验”载入销售样例并直接打开简报核验。
        全局快捷键可在顶栏的“查看键盘快捷键”中查阅。
      </p>
      <p className="sr-only" role="status" aria-live="polite">
        {uploading
          ? uploadProgress != null
            ? uploadProgress >= 100
              ? "上传完成，正在解析数据"
              : "正在上传数据文件"
            : "正在读取数据文件"
          : ""}
      </p>
      {/* 背景光晕交给 CSS 控制配色（empty-state.css 按主题给 --aurora-* ）：
          组件内联写死颜色会让暗色主题拿到同一组高饱和值，实测在深底上糊成
          一大片蓝紫、并盖住主文案的对比度。 */}
      <div className="empty-copy">
        <span className="section-kicker">新建分析</span>
        <h2>从一份数据开始</h2>
        {/* 场景词轮播：传达"能分析什么"，降低首次使用的想象成本；
            对屏幕阅读器隐藏（轮播文本反复播报是噪音） */}
        <p className="empty-scene">
          分析销售趋势、用户增长与成本结构，或直接核验一份经营简报。
        </p>
        {/* 支持格式徽章：带 stagger 入场动画 */}
        <motion.div
          className="empty-formats"
          aria-hidden="true"
          initial="hidden"
          animate="visible"
          variants={{ hidden: {}, visible: { transition: { staggerChildren: 0.08, delayChildren: 0.6 } } }}
        >
          {[
            { icon: <FileSpreadsheet size={12} />, label: "CSV" },
            { icon: <FileSpreadsheet size={12} />, label: "Excel" },
            { icon: <Braces size={12} />, label: "JSON" },
            { icon: <Table2 size={12} />, label: "Parquet" },
          ].map((item) => (
            <motion.span
              key={item.label}
              className="format-badge"
              variants={{ hidden: { opacity: 0, y: 8, scale: 0.9 }, visible: { opacity: 1, y: 0, scale: 1 } }}
            >
              {item.icon}{item.label}
            </motion.span>
          ))}
        </motion.div>
        <p>CSV、Excel、JSON 或 Parquet{dragOver ? " · 松开以上传" : " · 或拖拽文件到此"}</p>
        <div className="empty-actions">
          <div className="empty-primary-row">
            <button type="button"
                ref={btnRef}
                className="primary empty-upload-button"
                onClick={onUpload}
                disabled={uploading}
                onMouseMove={handleBtnMouseMove}
                onMouseLeave={handleBtnMouseLeave}
                style={{ transition: "transform 0.2s var(--ease-out), background 0.15s, box-shadow 0.15s" }}
              >
                {uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
                {uploading ? (uploadProgress != null ? `上传中 ${uploadProgress}%` : "正在读取") : "选择数据文件"}
              </button>
            <button
              type="button"
              className="empty-audit-button"
              onClick={() => handleLoadSample("sales_audit", true)}
              disabled={uploading}
              aria-describedby="empty-audit-hint"
            >
              <ShieldCheck size={15} aria-hidden="true" />
              体验简报核验
            </button>
            <span id="empty-audit-hint" className="sr-only">
              载入销售样例并切换到简报核验视图。进入后按 Tab 到“草稿工作台”编辑草稿，
              在草稿框内按 Ctrl 或 Command 加 Enter 直接复算。
            </span>
          </div>
          <details
            className="empty-demo-menu"
            onClick={(event) => event.stopPropagation()}
          >
            <summary className="empty-sample-button">
              <Sparkles size={13} />
              打开示例
            </summary>
            <div className="empty-demo-list">
              {[
                {
                  kind: "sales_audit",
                  title: "经营简报核验",
                  blurb: "草稿把 462 件当成 462 单，实际只有 20 单。",
                  audit: true,
                },
                {
                  kind: "regional_policy",
                  title: "区域权限与 PII",
                  blurb: "华东同事只看到 8 行，手机号还是脱敏的。",
                  audit: false,
                },
                {
                  kind: "quality_injection",
                  title: "脏数据与提示注入",
                  blurb: "37 行里，缺数、负数、1 亿级异常值都齐了。",
                  audit: false,
                },
              ].map((demo) => (
                <button
                  key={demo.kind}
                  type="button"
                  className="empty-demo-item"
                  disabled={uploading}
                  onClick={() => handleLoadSample(demo.kind, demo.audit)}
                >
                  <strong>{demo.title}</strong>
                  <small>{demo.blurb}</small>
                </button>
              ))}
              {/* 次级演示：默认折叠。全量目录曾把首屏选择变成 7 选 1 的
                  决策负担（收敛前实测首屏 F 加上扫描时间显著变长），
                  这里保 3 个主推，其余收进二级目录，入口保留不删数据。 */}
              <details className="empty-demo-more">
                <summary>更多示例（增长 / 财报 / 成绩 / 20 万行）</summary>
                {[
                  {
                    kind: "growth",
                    title: "多维增长月报",
                    blurb: "12 个月、3 个渠道、4 个品类，能拆出 10 张图。",
                    audit: false,
                  },
                  {
                    kind: "finance",
                    title: "公司财报分析",
                    blurb: "12 个月利润表、现金流和预算，拆开看钱去哪了。",
                    audit: false,
                  },
                  {
                    kind: "grades",
                    title: "年级成绩分析",
                    blurb: "3,000 名学生、60 个班，姓名和学号脱敏。",
                    audit: false,
                  },
                  {
                    kind: "bigdata",
                    title: "20 万行密度视图",
                    blurb: "20 万行聚成格子看，不卡。",
                    audit: false,
                  },
                ].map((demo) => (
                  <button
                    key={demo.kind}
                    type="button"
                    className="empty-demo-item"
                    disabled={uploading}
                    onClick={() => handleLoadSample(demo.kind, demo.audit)}
                  >
                    <strong>{demo.title}</strong>
                    <small>{demo.blurb}</small>
                  </button>
                ))}
              </details>
            </div>
          </details>
        </div>
        {/* 上传进度条 + 取消：仅在上传中且有真实进度时展示。
            100% 后服务端仍在解析文件（读取/探测编码），文案切换为"正在解析"。 */}
        {uploading && uploadProgress != null && (
          <div className="empty-upload-progress" role="progressbar" aria-valuenow={uploadProgress} aria-valuemin={0} aria-valuemax={100}>
            <div className="empty-upload-progress-track">
              <span style={{ width: `${uploadProgress}%` }} />
            </div>
            <div className="empty-upload-progress-meta">
              <small>{uploadProgress >= 100 ? "上传完成，正在解析数据…" : `已上传 ${uploadProgress}%`}</small>
              {onCancelUpload && uploadProgress < 100 && (
                <button type="button" className="empty-upload-cancel" onClick={(e) => { e.stopPropagation(); onCancelUpload(); }}>
                  取消上传
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

export default React.memo(EmptyWorkspace);
