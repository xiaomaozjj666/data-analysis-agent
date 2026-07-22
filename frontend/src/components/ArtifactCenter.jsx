import React from "react";
import { Download, ExternalLink, FileSpreadsheet } from "lucide-react";
import { pickChartIcon } from "../constants";
import { formatBytes } from "../utils/format";

const ArtifactCenter = React.memo(function ArtifactCenter({ artifacts = [], onDownload, onPreview }) {
  const charts = artifacts.filter((item) => item.kind === "visualization");
  const files = artifacts.filter((item) => item.kind !== "visualization");
  if (!artifacts.length) return <div className="empty-row">分析完成后，最终图表和数据文件会出现在这里。</div>;
  return (
    <div className="artifact-center">
      {charts.length > 0 && (
        <section className="artifact-section">
          <div className="artifact-section-label"><span>交互图表</span><small>{charts.length} 张精选结果</small></div>
          <div className="chart-grid">
            {charts.map((item, index) => {
              const { Icon, label } = pickChartIcon(item.name);
              return (
                <article className="chart-card" key={item.name}>
                  <div className="chart-index">{String(index + 1).padStart(2, "0")}</div>
                  <Icon size={20} />
                  <div>
                    <strong>{item.description || item.name}</strong>
                    <small>{label} · {formatBytes(item.size_bytes)} · 点击查看交互</small>
                  </div>
                  <div className="artifact-actions">
                    <button className="preview-button" onClick={() => onPreview(item)}>
                      <ExternalLink size={14} />在线查看
                    </button>
                    <button className="icon-button" title={`下载 ${item.name}`} onClick={() => onDownload(item)}>
                      <Download size={15} />
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}
      {files.length > 0 && (
        <section className="artifact-section artifact-files">
          <div className="artifact-section-label"><span>数据文件</span><small>仅保留最终版本</small></div>
          <div className="artifact-list">
            {files.map((item) => (
              <div key={item.name}>
                <FileSpreadsheet size={17} />
                <span><strong>{item.name}</strong><small>{item.description} {formatBytes(item.size_bytes)}</small></span>
                <button className="artifact-download" title={`下载 ${item.name}`} onClick={() => onDownload(item)}><Download size={16} /></button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
});

export default ArtifactCenter;
