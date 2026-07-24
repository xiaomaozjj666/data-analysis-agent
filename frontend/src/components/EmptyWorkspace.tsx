import React, { useCallback, useRef, useState } from "react";
import { Braces, FileSpreadsheet, LoaderCircle, Sparkles, Table2, Upload } from "lucide-react";
import DotField from "./rb/DotField";
import ShinyText from "./rb/ShinyText";

interface EmptyWorkspaceProps {
  uploading: boolean;
  onUpload: () => void;
  onFileDrop: (file: File) => void;
}

// 支持的文件扩展名，与 App.tsx 文件输入 accept 保持一致
const SUPPORTED_EXTENSIONS = [".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".parquet"];
function isSupportedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return SUPPORTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

function EmptyWorkspace({ uploading, onUpload, onFileDrop }: EmptyWorkspaceProps) {
  // 鼠标跟随光斑：跟踪鼠标在整个工作区的相对位置，更新 CSS 变量，
  // 由 empty-state.css 的 radial-gradient 渲染全屏柔和光晕。
  const sectionRef = useRef<HTMLElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);
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
      <DotField className="empty-grid-bg" gap={28} size={2} color="rgba(128, 128, 145, 0.2)" glowColor="rgba(91, 91, 214, 0.12)" />
      <div className="empty-copy">
        <span className="section-kicker">新建分析</span>
        <h2><ShinyText text="从一份数据开始" color="var(--text-primary)" shineColor="var(--accent-color)" speed={3} /></h2>
        {/* 支持格式徽章：纯装饰，告知用户可上传的文件类型 */}
        <div className="empty-formats" aria-hidden="true">
          <span className="format-badge"><FileSpreadsheet size={12} />CSV</span>
          <span className="format-badge"><FileSpreadsheet size={12} />Excel</span>
          <span className="format-badge"><Braces size={12} />JSON</span>
          <span className="format-badge"><Table2 size={12} />Parquet</span>
        </div>
        <p>CSV、Excel、JSON 或 Parquet{dragOver ? " · 松开以上传" : " · 或拖拽文件到此"}</p>
        <div className="empty-actions">
          <button className="primary empty-upload-button" onClick={onUpload} disabled={uploading}>
            {uploading ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}
            {uploading ? "正在读取" : "选择数据文件"}
          </button>
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
      </div>
    </section>
  );
}

export default React.memo(EmptyWorkspace);
