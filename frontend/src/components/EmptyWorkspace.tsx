import React, { useCallback, useRef, useState } from "react";
import { LoaderCircle, Upload } from "lucide-react";

interface EmptyWorkspaceProps {
  uploading: boolean;
  onUpload: () => void;
  onFileDrop: (file: File) => void;
}

function EmptyWorkspace({ uploading, onUpload, onFileDrop }: EmptyWorkspaceProps) {
  // 鼠标跟随光斑：跟踪鼠标在 grid 上的相对位置，更新 CSS 变量，
  // 由 styles.css 的 radial-gradient 渲染柔和光晕。参考 Linear/Vercel
  // 空状态的 spotlight 效果——比静态装饰更有"活物感"。
  const gridRef = useRef<HTMLDivElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);
  const handleMouseMove = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    const grid = gridRef.current;
    if (!grid) return;
    const rect = grid.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 100;
    const y = ((event.clientY - rect.top) / rect.height) * 100;
    grid.style.setProperty("--mouse-x", `${x}%`);
    grid.style.setProperty("--mouse-y", `${y}%`);
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
  const handleDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current = 0;
    setDragOver(false);
    const file = e.dataTransfer?.files?.[0];
    if (file && onFileDrop) onFileDrop(file);
  }, [onFileDrop]);

  // 整个工作区都可点击触发上传：点击空白区域等价于点击主按钮，
  // 让用户无需精准瞄准按钮即可发起分析。点击按钮自身时由按钮处理，
  // 这里通过 closest("button") 排除，避免重复弹出文件选择框。
  const handleSectionClick = useCallback((e: React.MouseEvent<HTMLElement>) => {
    if (uploading) return;
    if ((e.target as HTMLElement).closest("button")) return;
    onUpload();
  }, [onUpload, uploading]);

  return (
    <section
      className={`empty-workspace${dragOver ? " is-drag-over" : ""}`}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={handleSectionClick}
    >
      <div
        className="empty-grid"
        aria-hidden="true"
        ref={gridRef}
        onMouseMove={handleMouseMove}
      >
        <span className="grid-tab" />
        {Array.from({ length: 20 }, (_, index) => (
          <i key={index} style={{ "--cell-index": index } as React.CSSProperties} />
        ))}
      </div>
      <div className="empty-copy">
        <span className="section-kicker">新建分析</span>
        <h2>从一份数据开始</h2>
        <p>CSV、Excel、JSON 或 Parquet{dragOver ? " · 松开以上传" : " · 或拖拽文件到此"}</p>
        <button className="primary" onClick={onUpload} disabled={uploading}>
          {uploading ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}
          {uploading ? "正在读取" : "选择数据文件"}
        </button>
      </div>
    </section>
  );
}

export default EmptyWorkspace;
