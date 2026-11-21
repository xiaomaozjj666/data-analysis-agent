import React, { useMemo, useState } from "react";
import { Download, ExternalLink, FileSpreadsheet } from "lucide-react";
import { pickChartIcon } from "../constants";
import { formatBytes } from "../utils/format";
import type { Artifact, CandidateDataset } from "../types";
import AuthImage from "./AuthImage";
import EChartThumb from "./EChartThumb";
import EmptyState from "./EmptyState";
import PlotlyThumb from "./PlotlyThumb";
import SpotlightCard from "./rb/SpotlightCard";

interface ArtifactCenterProps {
  artifacts?: Artifact[] | null;
  onDownload: (item: Artifact) => void;
  onPreview: (item: Artifact) => void;
  // 批量下载回调：选中多张图表后一次性触发下载
  onBatchDownload?: (items: Artifact[]) => void;
  // 候选数据集（join 等派生、未采纳）：展示"设为主数据"横幅
  candidateDatasets?: CandidateDataset[];
  onAdoptDataset?: (name: string) => void;
  onDiscardDataset?: (name: string) => void;
  adoptBusy?: boolean;
  // 产物为空时提供"重新扫描"出口（修复手工/外部落盘文件不显示）
  onRescan?: () => void;
  rescanBusy?: boolean;
}

const ArtifactCenter = React.memo(function ArtifactCenter({
  artifacts: artifactsProp,
  onDownload,
  onPreview,
  onBatchDownload,
  candidateDatasets,
  onAdoptDataset,
  onDiscardDataset,
  adoptBusy,
  onRescan,
  rescanBusy,
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

  // 已实际存在的图表引擎：仅当某引擎有图表时才显示对应筛选标签，
  // 避免出现「点 Plotly 却什么都没有」——当前产物几乎全是 ECharts，
  // Plotly 标签若无图表则直接不渲染。
  const enginesPresent = useMemo(() => {
    const set = new Set(charts.map((c) => c.engine));
    return { echarts: set.has("echarts"), plotly: set.has("plotly") };
  }, [charts]);

  // 筛选与排序状态
  const [filter, setFilter] = useState<"all" | "plotly" | "echarts">("all");
  const [sortBy, setSortBy] = useState<"default" | "name" | "size">("default");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // 防御：若当前 filter 指向实际不存在的引擎（如历史选择了 Plotly 但本批无
  // Plotly 图），退化为 "all"，杜绝筛选后整块交互图表空白的情况。
  const effectiveFilter: "all" | "plotly" | "echarts" =
    filter === "all" ? "all" : enginesPresent[filter] ? filter : "all";

  // 按引擎筛选 + 按名称/大小排序
  const filteredCharts = useMemo(() => {
    let result = charts;
    if (effectiveFilter !== "all") {
      result = result.filter((c) => c.engine === effectiveFilter);
    }
    if (sortBy === "name") {
      result = [...result].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    } else if (sortBy === "size") {
      result = [...result].sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
    }
    return result;
  }, [charts, effectiveFilter, sortBy]);

  if (!artifacts.length) {
    return (
      <EmptyState
        icon={FileSpreadsheet}
        title="还没有产物"
        hint="分析完成后，最终图表和数据文件会出现在这里。如果产物文件是在会话外生成的（手工放置或脚本写入），可以重新扫描目录。"
        actionLabel={rescanBusy ? "扫描中…" : "扫描产物目录"}
        onAction={onRescan}
        actionDisabled={rescanBusy || !onRescan}
      />
    );
  }

  const hasBatchDownload = !!onBatchDownload;
  const candidates = candidateDatasets ?? [];

  return (
    <div className="artifact-center">
      {candidates.length > 0 && (
        <div className="candidate-dataset-banner" role="status">
          <div className="empty-state-body">
            <strong>
              有 {candidates.length} 个候选数据集待采纳
            </strong>
            <small>
              {candidates
                .map((item) => `${item.name}（${item.rows.toLocaleString()} 行）`)
                .join("、")}
              ——采纳后会替换当前主数据并重置行数基线；忽略则仅清除提示。
            </small>
          </div>
          <div className="candidate-dataset-banner-actions">
            {candidates.map((item) => (
              <React.Fragment key={item.name}>
                <button
                  type="button"
                  className="primary"
                  disabled={adoptBusy}
                  onClick={() => onAdoptDataset?.(item.name)}
                >
                  设为主数据
                </button>
                <button type="button" disabled={adoptBusy} onClick={() => onDiscardDataset?.(item.name)}>
                  忽略
                </button>
              </React.Fragment>
            ))}
          </div>
        </div>
      )}
      {charts.length > 0 && (
        <section className="artifact-section">
          <div className="artifact-section-label">
            <span>交互图表</span>
            <small>{charts.length} 张精选结果</small>
          </div>
          {/* 筛选与排序控件 */}
          <div className="artifact-controls" role="group" aria-label="图表筛选与排序">
            <button
              type="button"
              className={effectiveFilter === "all" ? "active" : ""}
              aria-pressed={effectiveFilter === "all"}
              onClick={() => setFilter("all")}
            >
              全部
            </button>
            {enginesPresent.plotly && (
              <button
                type="button"
                className={effectiveFilter === "plotly" ? "active" : ""}
                aria-pressed={effectiveFilter === "plotly"}
                onClick={() => setFilter("plotly")}
              >
                Plotly
              </button>
            )}
            {enginesPresent.echarts && (
              <button
                type="button"
                className={effectiveFilter === "echarts" ? "active" : ""}
                aria-pressed={effectiveFilter === "echarts"}
                onClick={() => setFilter("echarts")}
              >
                ECharts
              </button>
            )}
            <select value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)} aria-label="图表排序方式">
              <option value="default">默认排序</option>
              <option value="name">按名称</option>
              <option value="size">按大小</option>
            </select>
            {hasBatchDownload && selected.size > 0 && (
              <button
                type="button"
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
          {filteredCharts.length > 0 ? (
            <div className="chart-grid">
            {filteredCharts.map((item, index) => {
              const { Icon, label } = pickChartIcon(item.name);
              const isSelected = selected.has(item.name);
              return (
                <SpotlightCard key={item.name} className="chart-card-spotlight" spotlightColor="rgba(128, 128, 140, 0.10)" spotlightRadius={280} tiltMax={4} hoverScale={1.015}>
                <article className="chart-card" style={{ animationDelay: `${index * 60}ms` }}>
                  {hasBatchDownload && (
                    <input
                      type="checkbox"
                      className="chart-card-checkbox"
                      checked={isSelected}
                      aria-label={`选中 ${item.description || item.name}`}
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
                  {item.engine === "plotly" && item.preview_url ? (
                    /* Plotly 缩略图是服务端静态 PNG，悬停无法查看数据；
                       改为 plotly.js 原地渲染的交互迷你图（与 ECharts
                       卡片一致），失败时回退原有 PNG 缩略图 */
                    <div
                      className="chart-thumbnail"
                      role="button"
                      tabIndex={0}
                      aria-label={`预览 ${item.description || item.name}`}
                      onClick={() => onPreview(item)}
                      onKeyDown={(e) => {
                        // 键盘可达：Enter / Space 与鼠标点击等价，让不用鼠标的用户也能打开预览
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onPreview(item);
                        }
                      }}
                    >
                      <PlotlyThumb
                        previewUrl={item.preview_url}
                        fallbackSrc={item.thumbnail_url}
                        alt={item.description || item.name}
                      />
                    </div>
                  ) : item.thumbnail_url ? (
                    <div
                      className="chart-thumbnail"
                      role="button"
                      tabIndex={0}
                      aria-label={`预览 ${item.description || item.name}`}
                      onClick={() => onPreview(item)}
                      onKeyDown={(e) => {
                        // 键盘可达：Enter / Space 与鼠标点击等价，让不用鼠标的用户也能打开预览
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onPreview(item);
                        }
                      }}
                    >
                      <AuthImage
                        src={item.thumbnail_url}
                        alt={item.description || item.name}
                        loading="lazy"
                      />
                    </div>
                  ) : item.engine === "echarts" && item.preview_url ? (
                    /* ECharts 无服务端 PNG 缩略图（kaleido 仅支持 Plotly）：
                       内联渲染迷你交互图，与 Plotly 卡片一样无需点击即可预览 */
                    <div
                      className="chart-thumbnail"
                      role="button"
                      tabIndex={0}
                      aria-label={`预览 ${item.description || item.name}`}
                      onClick={() => onPreview(item)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onPreview(item);
                        }
                      }}
                    >
                      <EChartThumb previewUrl={item.preview_url} alt={item.description || item.name} />
                    </div>
                  ) : (
                    <div
                      className="chart-icon"
                      role="button"
                      tabIndex={0}
                      aria-label={`预览 ${item.description || item.name}`}
                      onClick={() => onPreview(item)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onPreview(item);
                        }
                      }}
                    >
                      <Icon size={28} />
                      <small>点击查看交互图</small>
                    </div>
                  )}
                  <div className="chart-card-info">
                    <div className="chart-index">{String(index + 1).padStart(2, "0")}</div>
                    <div className="chart-card-text">
                      <strong title={item.description || item.name}>{item.description || item.name}</strong>
                      <small>{label} · {formatBytes(item.size_bytes)} · 点击查看交互</small>
                    </div>
                  </div>
                  <div className="artifact-actions">
                    <button type="button" className="preview-button" onClick={() => onPreview(item)}>
                      <ExternalLink size={14} />在线查看
                    </button>
                    <button type="button" className="icon-button" title={`下载 ${item.name}`} aria-label={`下载 ${item.name}`} onClick={() => onDownload(item)}>
                      <Download size={15} />
                    </button>
                  </div>
                </article>
                </SpotlightCard>
              );
            })}
            </div>
          ) : (
            <EmptyState
              compact
              title="当前筛选条件下暂无图表"
              hint={`本批产物仅包含 ${
                enginesPresent.echarts && enginesPresent.plotly
                  ? "ECharts 与 Plotly"
                  : enginesPresent.echarts
                    ? "ECharts"
                    : "Plotly"
              } 图表。`}
            />
          )}
        </section>
      )}
      {files.length > 0 && (
        <section className="artifact-section artifact-files">
          <div className="artifact-section-label"><span>数据文件</span><small>仅保留最终版本</small></div>
          <div className="artifact-list">
            {files.map((item) => (
              <div key={item.name}>
                <FileSpreadsheet size={17} />
                <span><strong title={item.name}>{item.name}</strong><small>{item.description} {formatBytes(item.size_bytes)}</small></span>
                <button type="button" className="artifact-download" title={`下载 ${item.name}`} aria-label={`下载 ${item.name}`} onClick={() => onDownload(item)}><Download size={16} /></button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
});

export default ArtifactCenter;
