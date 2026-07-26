import React, { useCallback, useRef, useState } from "react";
import { Braces, FileSpreadsheet, LoaderCircle, Sparkles, Table2, Upload } from "lucide-react";
import { motion } from "motion/react";
import DotField from "./rb/DotField";
import Aurora from "./rb/Aurora";
import SplitText from "./rb/SplitText";
import ShinyText from "./rb/ShinyText";
import ClickSpark from "./rb/ClickSpark";
import RotatingText from "./rb/RotatingText";

interface EmptyWorkspaceProps {
  uploading: boolean;
  // 上传进度百分比（0-100）；null 表示无可展示进度（未在上传或长度不可计）
  uploadProgress?: number | null;
  onUpload: () => void;
  onFileDrop: (file: File) => void;
  // 取消进行中的上传：大文件上传耗时长，给用户反悔的机会
  onCancelUpload?: () => void;
}

// 支持的文件扩展名，与 App.tsx 文件输入 accept 保持一致
const SUPPORTED_EXTENSIONS = [".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".parquet"];
function isSupportedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return SUPPORTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

function EmptyWorkspace({ uploading, uploadProgress, onUpload, onFileDrop, onCancelUpload }: EmptyWorkspaceProps) {
  // 鼠标跟随光斑：跟踪鼠标在整个工作区的相对位置，更新 CSS 变量，
  // 由 empty-state.css 的 radial-gradient 渲染全屏柔和光晕。
  const sectionRef = useRef<HTMLElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [titleDone, setTitleDone] = useState(false);
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
  const handleSectionClick = useCallback((e: React.MouseEvent<HTMLElement>) => {
    if (uploading) return;
    if ((e.target as HTMLElement).closest("button")) return;
    onUpload();
  }, [onUpload, uploading]);

  // 键盘可达性：Enter / Space 触发上传，让不用鼠标的用户也能操作
  const handleSectionKeyDown = useCallback((e: React.KeyboardEvent<HTMLElement>) => {
    if (uploading) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onUpload();
    }
  }, [onUpload, uploading]);

  // 示例数据快速开始：通知 App.tsx 调用 /api/sessions/sample
  const handleLoadSample = useCallback(() => {
    window.dispatchEvent(new CustomEvent("empty-workspace:load-sample"));
  }, []);

  return (
    <section
      ref={sectionRef}
      className={`empty-workspace${dragOver ? " is-drag-over" : ""}`}
      role="button"
      tabIndex={0}
      aria-label="上传数据文件开始分析，或拖拽文件到此区域"
      onMouseMove={handleMouseMove}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={handleSectionClick}
      onKeyDown={handleSectionKeyDown}
    >
      <Aurora className="empty-aurora-bg" colorPrimary="rgba(91, 91, 214, 0.25)" colorSecondary="rgba(139, 92, 246, 0.15)" colorTertiary="rgba(59, 130, 246, 0.12)" speed={0.7} blur={80} opacity={0.8} />
      <DotField className="empty-grid-bg" dotRadius={2} dotSpacing={24} cursorRadius={460} bulgeStrength={88} gradientFrom="rgba(91, 91, 214, 0.32)" gradientTo="rgba(120, 120, 140, 0.22)" glowColor="transparent" />
      <div className="empty-copy">
        <span className="section-kicker">新建分析</span>
        <h2>
          {titleDone ? (
            <ShinyText text="从一份数据开始" color="var(--fg-default)" shineColor="var(--accent-fg)" speed={3} />
          ) : (
            <SplitText text="从一份数据开始" splitBy="char" stagger={0.04} delay={0.2} onComplete={() => setTitleDone(true)} />
          )}
        </h2>
        {/* 场景词轮播：传达"能分析什么"，降低首次使用的想象成本；
            对屏幕阅读器隐藏（轮播文本反复播报是噪音） */}
        <p className="empty-scene" aria-hidden="true">
          看清楚你的
          <RotatingText words={["销售趋势", "用户增长", "成本结构", "异常波动", "实验效果"]} />
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
          <ClickSpark sparkColor="var(--accent-fg)" sparkLength={14}>
            <button
              ref={btnRef}
              className="primary empty-upload-button"
              onClick={onUpload}
              disabled={uploading}
              onMouseMove={handleBtnMouseMove}
              onMouseLeave={handleBtnMouseLeave}
              style={{ transition: "transform 0.2s var(--ease-out), background 0.15s, box-shadow 0.15s" }}
            >
              {uploading ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}
              {uploading ? (uploadProgress != null ? `上传中 ${uploadProgress}%` : "正在读取") : "选择数据文件"}
            </button>
          </ClickSpark>
          <button
            type="button"
            className="empty-sample-button"
            onClick={handleLoadSample}
            disabled={uploading}
            aria-label="加载示例数据体验"
          >
            <Sparkles size={13} />
            加载示例数据体验
          </button>
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
