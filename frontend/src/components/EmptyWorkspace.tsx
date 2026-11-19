import React, { useCallback, useRef, useState } from "react";
import {
  Braces,
  FileCheck2,
  FileSpreadsheet,
  Link2,
  LoaderCircle,
  ShieldCheck,
  Sparkles,
  Table2,
  Upload,
} from "lucide-react";

interface EmptyWorkspaceProps {
  uploading: boolean;
  // 上传进度百分比（0-100）；null 表示无可展示进度（未在上传或长度不可计）
  uploadProgress?: number | null;
  onUpload: () => void;
  onFileDrop: (file: File) => void;
  // 取消进行中的上传：大文件上传耗时长，给用户反悔的机会
  onCancelUpload?: () => void;
}

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
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);

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

  const handleSectionClick = useCallback((e: React.MouseEvent<HTMLElement>) => {
    if (uploading) return;
    if ((e.target as HTMLElement).closest("button")) return;
    const selection = window.getSelection?.();
    if (selection && selection.toString().trim().length > 0) return;
    onUpload();
  }, [onUpload, uploading]);

  const handleSectionKeyDown = useCallback((e: React.KeyboardEvent<HTMLElement>) => {
    if (uploading || e.currentTarget !== e.target) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onUpload();
    }
  }, [onUpload, uploading]);

  const handleLoadSample = useCallback((auditDemo: boolean) => {
    window.dispatchEvent(new CustomEvent("empty-workspace:load-sample", { detail: { auditDemo } }));
  }, []);

  return (
    <section
      className={`empty-workspace${dragOver ? " is-drag-over" : ""}`}
      role="button"
      tabIndex={0}
      aria-label="上传数据文件开始分析，或拖拽文件到此区域"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={handleSectionClick}
      onKeyDown={handleSectionKeyDown}
    >
      <div className="empty-copy">
        <div className="empty-kicker-row">
          <span className="empty-kicker-dot" aria-hidden="true" />
          <span className="section-kicker">可信数据分析与核验工作台</span>
        </div>
        <h2>从一份数据开始</h2>
        <p className="empty-subtitle">
          先由程序独立复算原始数据，再交付经核验的洞察与经营简报。
        </p>

        <div className="empty-formats" aria-hidden="true">
          {[
            { icon: <FileSpreadsheet size={12} />, label: "CSV" },
            { icon: <FileSpreadsheet size={12} />, label: "Excel" },
            { icon: <Braces size={12} />, label: "JSON" },
            { icon: <Table2 size={12} />, label: "Parquet" },
          ].map((item) => (
            <span key={item.label} className="format-badge">
              {item.icon}{item.label}
            </span>
          ))}
        </div>

        <p className="empty-drop-hint">
          支持 CSV、Excel、JSON 或 Parquet{dragOver ? " · 松开以上传" : " · 点击空白处或拖拽文件到此"}
        </p>

        <div className="empty-actions">
          <div className="empty-primary-row">
            <button
              type="button"
              className="primary empty-upload-button"
              onClick={onUpload}
              disabled={uploading}
            >
              {uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
              {uploading ? (uploadProgress != null ? `上传中 ${uploadProgress}%` : "正在读取") : "选择数据文件"}
            </button>
            <button
              type="button"
              className="empty-audit-button"
              onClick={() => handleLoadSample(true)}
              disabled={uploading}
            >
              <ShieldCheck size={16} />
              体验简报核验
            </button>
          </div>
          <button
            type="button"
            className="empty-sample-button"
            onClick={() => handleLoadSample(false)}
            disabled={uploading}
            aria-label="加载示例数据体验"
          >
            <Sparkles size={13} />
            加载示例数据体验
          </button>
        </div>

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

        <div className="empty-trust-strip" aria-hidden="true">
          <span><FileCheck2 size={13} />原表独立复算</span>
          <span><ShieldCheck size={13} />范围门拦截越界口径</span>
          <span><Link2 size={13} />SHA-256 哈希链存证</span>
        </div>
      </div>
    </section>
  );
}

export default React.memo(EmptyWorkspace);
