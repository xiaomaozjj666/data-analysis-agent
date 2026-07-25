import React, { useMemo, useState } from "react";
import { Download, ExternalLink, FileSpreadsheet } from "lucide-react";
import { API_URL, pickChartIcon } from "../constants";
import { formatBytes } from "../utils/format";
import SpotlightCard from "./rb/SpotlightCard";
import type { Artifact } from "../types";

interface ArtifactCenterProps {
  artifacts?: Artifact[] | null;
  onDownload: (item: Artifact) => void;
  onPreview: (item: Artifact) => void;
  // 批量下载回调：选中多张图表后一次性触发下载
  onBatchDownload?: (items: Artifact[]) => void;
}

const ArtifactCenter = React.memo(function ArtifactCenter({
  artifacts: artifactsProp,
  onDownload,
  onPreview,
  onBatchDownload,
}: ArtifactCenterProps) {
  const artifacts = artifactsProp || [];
  const charts = useMemo(
    () => artifacts.filter((item) => item.kind === "visualization"),
    [artifacts],
  );
  const files = useMemo(
    () => artifacts.filter((item) => item.kind !== "visualization"),
    [artifacts],
  );

  // 筛选与排序状态
  const [filter, setFilter] = useState<"all" | "plotly" | "echarts">("all");
  const [sortBy, setSortBy] = useState<"default" | "name" | "size">("default");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // 按引擎筛选 + 按名称/大小排序
  const filteredCharts = useMemo(() => {
    let result = charts;
    if (filter !== "all") {
      result = result.filter((c) => c.engine === filter);
    }
    if (sortBy === "name") {
      result = [...result].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    } else if (sortBy === "size") {
      result = [...result].sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
    }
    return result;
  }, [charts, filter, sortBy]);

  if (!artifacts.length) return <div className="empty-row">分析完成后，最终图表和数据文件会出现在这里。</div>;

  const hasBatchDownload = !!onBatchDownload;

  return (
    <div className="artifact-center">
      {filteredCharts.length > 0 && (
        <section className="artifact-section">
          <div className="artifact-section-label">
            <span>交互图表</span>
            <small>{filteredCharts.length} 张精选结果</small>
          </div>
          {/* 筛选与排序控件 */}
          <div className="artifact-controls">
            <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>
              全部
            </button>
            <button className={filter === "plotly" ? "active" : ""} onClick={() => setFilter("plotly")}>
              Plotly
            </button>
            <button className={filter === "echarts" ? "active" : ""} onClick={() => setFilter("echarts")}>
              ECharts
            </button>
            <select value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)}>
              <option value="default">默认排序</option>
              <option value="name">按名称</option>
              <option value="size">按大小</option>
            </select>
            {hasBatchDownload && selected.size > 0 && (
              <button
                className="batch-download-btn"
                onClick={() => {
                  onBatchDownload?.(filteredCharts.filter((c) => selected.has(c.name)));
                  setSelected(new Set());
                }}
              >
                下载选中 ({selected.size})
              </button>
            )}
          </div>
          <div className="chart-grid">
            {filteredCharts.map((item, index) => {
              const { Icon, label } = pickChartIcon(item.name);
              const isSelected = selected.has(item.name);
              return (
                <SpotlightCard key={item.name} className="chart-card-spotlight" spotlightColor="rgba(91, 91, 214, 0.12)" spotlightRadius={280} tiltMax={4} hoverScale={1.015}>
                <article className="chart-card" style={{ animationDelay: `${index * 60}ms` }}>
                  {hasBatchDownload && (
                    <input
                      type="checkbox"
                      className="chart-card-checkbox"
                      checked={isSelected}
                      onChange={(e) => {
                        const next = new Set(selected);
                        if (e.target.checked) {
                          next.add(item.name);
                        } else {
                          next.delete(item.name);
                        }
                        setSelected(next);
                      }}
                    />
                  )}
                  {item.engine && (
                    <span className="engine-badge">{item.engine === "plotly" ? "Plotly" : "ECharts"}</span>
                  )}
                  {item.thumbnail_url ? (
                    <div className="chart-thumbnail" onClick={() => onPreview(item)}>
                      <img
                        src={`${API_URL}${item.thumbnail_url}`}
                        alt={item.description || item.name}
                        loading="lazy"
                        onError={(e) => {
                          (e.target as HTMLImageElement).style.display = "none";
                        }}
                      />
                    </div>
                  ) : (
                    <div className="chart-icon" onClick={() => onPreview(item)}>
                      <Icon size={28} />
                    </div>
                  )}
                  <div className="chart-card-info">
                    <div className="chart-index">{String(index + 1).padStart(2, "0")}</div>
                    <div className="chart-card-text">
                      <strong>{item.description || item.name}</strong>
                      <small>{label} · {formatBytes(item.size_bytes)} · 点击查看交互</small>
                    </div>
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
                </SpotlightCard>
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
