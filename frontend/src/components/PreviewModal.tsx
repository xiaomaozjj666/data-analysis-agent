import { AlertTriangle, Columns2, Download, FileImage, Maximize2, Palette, RefreshCw, X } from "lucide-react";
import { useAppStore } from "../store/useAppStore";
import type { UseArtifactPreviewResult } from "../hooks/useArtifactPreview";
import type { Artifact } from "../types";

interface PreviewModalProps {
  // useArtifactPreview 的完整返回值：模态的开关/对比/全屏/编辑/PNG 状态与回调
  preview: UseArtifactPreviewResult;
  theme: "light" | "dark";
  onDownload: (item: Artifact) => Promise<void>;
}

// 产物预览模态：提取自 App.tsx 的 preview-backdrop JSX（图表预览 / 对比 /
// 全屏 / PNG 导出 / 内联编辑）。状态仍由 useArtifactPreview + Zustand store
// 驱动，仅做展示层拆分，行为与原实现一致。previewItem 为空时不渲染。
function PreviewModal({ preview, theme, onDownload }: PreviewModalProps) {
  const {
    session, previewItem, previewHtml, previewLoading, previewError,
    setPreviewLoading, setPreviewError,
  } = useAppStore();
  const {
    openArtifactPreview, closeArtifactPreview, loadCompareChart, downloadPng, editChart,
    onPreviewIframeLoaded,
    chartEditOpen, setChartEditOpen, chartEditTitle, setChartEditTitle,
    chartEditColor, setChartEditColor, chartEditSaving,
    previewFullscreen, setPreviewFullscreen, compareMode, setCompareMode,
    compareItem, compareHtml, compareLoading, pngDownloading,
  } = preview;

  if (!previewItem) return null;

  return (
    <div
      className="preview-backdrop"
      role="presentation"
      onClick={(event) => {
        // 用 onClick 而非 onMouseDown，同时覆盖鼠标点击和触摸结束，
        // 避免纯触屏设备上 mousedown 被 preventDefault 或延迟 300ms。
        if (event.target === event.currentTarget) closeArtifactPreview();
      }}
    >
      <section className={`preview-panel ${previewFullscreen ? "is-fullscreen" : ""}`} role="dialog" aria-modal="true" aria-label={`预览 ${previewItem.description || previewItem.name}`}>
        <header>
          <div>
            <span className="section-kicker">交互图表</span>
            <h2>{previewItem.description || previewItem.name}</h2>
          </div>
          <div className="preview-actions">
            <button type="button" onClick={() => onDownload(previewItem)}><Download size={15} />下载</button>
            {/* PNG 导出（#23）：通过 postMessage 让 iframe 内图表渲染为 PNG 后回传 dataURL */}
            <button type="button" title="下载为 PNG 图片" onClick={downloadPng} disabled={pngDownloading}>
              <FileImage size={15} />
              {pngDownloading ? "导出中…" : "PNG"}
            </button>
            {/* 图表对比（#22）：仅当存在其他可视化产物时可点击，并排展示两张图 */}
            {session?.artifacts?.filter(a => a.kind === "visualization" && a.name !== previewItem?.name).length ? (
              <button type="button" title="对比其他图表" onClick={() => setCompareMode(v => !v)}>
                <Columns2 size={15} />
                对比
              </button>
            ) : null}
            {/* 图表编辑：切换内联编辑面板，修改标题或主色后调用后端更新产物 */}
            <button type="button" onClick={() => setChartEditOpen(!chartEditOpen)} title="编辑图表" aria-label="编辑图表标题或主色">
              <Palette size={15} />
              编辑
            </button>
            {/* 全屏切换（#17）：撑满视口，配合响应式 resize 自适应图表尺寸 */}
            <button type="button" title="全屏" onClick={() => setPreviewFullscreen(v => !v)}>
              <Maximize2 size={15} />
            </button>
            <button type="button" className="icon-button" title="关闭预览 (Esc)" onClick={closeArtifactPreview}><X size={17} /></button>
          </div>
        </header>
        {chartEditOpen && (
          <div className="chart-edit-panel">
            <label>
              <span>标题</span>
              <input type="text" value={chartEditTitle} onChange={(e) => setChartEditTitle(e.target.value)} placeholder="图表标题" />
            </label>
            <label>
              <span>主色</span>
              <input type="color" value={chartEditColor} onChange={(e) => setChartEditColor(e.target.value)} aria-label="图表主色" />
            </label>
            <button type="button" onClick={editChart} disabled={chartEditSaving}>
              {chartEditSaving ? "保存中…" : "应用修改"}
            </button>
          </div>
        )}
        {/* 对比图表选择器：列出当前会话中除主图外的可视化产物 */}
        {compareMode && (
          <div className="compare-selector">
            <span>选择对比图表：</span>
            {session?.artifacts?.filter(a => a.kind === "visualization" && a.name !== previewItem?.name).map(a => (
              <button
                key={a.name}
                type="button"
                className={compareItem?.name === a.name ? "active" : ""}
                onClick={() => loadCompareChart(a)}
              >
                {a.description || a.name}
              </button>
            ))}
          </div>
        )}
        <div className={`preview-stage ${compareMode && compareItem ? "is-comparing" : ""}`}>
          {previewError && (
            <div className="preview-loading preview-error">
              <AlertTriangle size={18} />
              <span>{previewError}</span>
              <button type="button" className="retry-button" onClick={() => openArtifactPreview(previewItem)}>
                <RefreshCw size={13} />重试
              </button>
            </div>
          )}
          {/* 主图表 */}
          <div className="preview-frame">
            {/* 加载骨架屏（#26）：替代旋转 spinner，视觉上预告柱状图布局 */}
            {previewLoading && !previewError && (
              <div className="preview-skeleton">
                <div className="skeleton-chart">
                  <div className="skeleton-bar" style={{ width: "60%", height: "40%" }} />
                  <div className="skeleton-bar" style={{ width: "80%", height: "60%" }} />
                  <div className="skeleton-bar" style={{ width: "45%", height: "30%" }} />
                </div>
                <small>正在准备交互图表…</small>
              </div>
            )}
            {previewHtml && !previewError && (
              <iframe
                title={previewItem.description || previewItem.name}
                sandbox="allow-scripts"
                referrerPolicy="no-referrer"
                srcDoc={previewHtml}
                onLoad={(e) => {
                  // 清掉 openArtifactPreview 启动的兜底超时定时器并关闭 loading。
                  onPreviewIframeLoaded();
                  try {
                    const iframe = e.target as HTMLIFrameElement;
                    // 注意：可选链不能用于赋值左侧，这里直接访问 contentWindow
                    // 在 sandbox 跨域时访问 contentWindow.document 会抛错，
                    // 由外层 try/catch 捕获后 noop。
                    iframe.contentWindow!.document.documentElement.dataset.theme = theme;
                  } catch { /* sandbox 跨域时 noop，图表脚本回退到 prefers-color-scheme */ }
                }}
                onError={() => {
                  setPreviewLoading(false);
                  setPreviewError("图表加载失败，请检查网络或重新生成产物。");
                }}
              />
            )}
          </div>
          {/* 对比图表：并排在右侧展示，复用主预览的 LRU 缓存 */}
          {compareMode && compareItem && (
            <div className="preview-frame">
              {compareLoading && (
                <div className="preview-skeleton">
                  <div className="skeleton-chart">
                    <div className="skeleton-bar" style={{ width: "60%", height: "40%" }} />
                    <div className="skeleton-bar" style={{ width: "80%", height: "60%" }} />
                    <div className="skeleton-bar" style={{ width: "45%", height: "30%" }} />
                  </div>
                  <small>正在准备对比图表…</small>
                </div>
              )}
              {compareHtml && (
                <iframe title={compareItem.name} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={compareHtml} />
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

export default PreviewModal;
