import { useState, type CSSProperties } from "react";
import type { DemoChart } from "../types";
import "./DemoExtraCharts.css";

const SERIES = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
];

// 超过 6 条序列时颜色开始循环，用虚线区分同一颜色的不同序列。
// 第 7–9 条使用第一组虚线，第 10–12 条用第二组，依次类推。
const SERIES_DASHES = ["4 3", "2 2", "7 3 2 3"];

export interface SeriesStyle {
  color: string;
  dash?: string;
}

// 统一的多序列样式入口：图例、折线、面积、堆叠、气泡等都用它取色，
// 避免某处仍按 index % 4 取色导致图例与实际的线颜色对不上。
export function seriesStyle(index: number): SeriesStyle {
  const safeIndex = Number.isFinite(index) ? Math.max(0, Math.trunc(index)) : 0;
  const color = SERIES[safeIndex % SERIES.length];
  const cycle = Math.floor(safeIndex / SERIES.length);
  if (cycle === 0) return { color };
  return { color, dash: SERIES_DASHES[(cycle - 1) % SERIES_DASHES.length] };
}

// 图例色块：纯色代表实线，重复渐变代表循环后叠加的虚线。
function swatchStyle(style: SeriesStyle): CSSProperties {
  if (!style.dash) return { background: style.color };
  return {
    background: `repeating-linear-gradient(90deg, ${style.color} 0 3px, transparent 3px 5px)`,
  };
}

function fmt(value: number, unit?: string): string {
  const text = Math.abs(value) >= 1000
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })
    : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return unit ? `${text} ${unit}` : text;
}

function Caption({ chart }: { chart: DemoChart }) {
  return (
    <figcaption>
      <span className="demo-chart-kicker">{chart.type === "area" ? "面积" : chart.type === "multiline" ? "多序列" : chart.type === "stacked" ? "堆叠" : chart.type === "treemap" ? "层级" : chart.type === "funnel" ? "漏斗" : chart.type === "radar" ? "对比" : chart.type === "gauge" ? "目标" : chart.type === "box" ? "分布" : chart.type === "bubble" ? "气泡" : "瀑布"}</span>
      <strong>{chart.title}</strong>
      {chart.subtitle && <small>{chart.subtitle}</small>}
    </figcaption>
  );
}

function Legend({ series }: { series: { name: string }[] }) {
  return (
    <ul className="demo-legend">
      {series.map((item, index) => (
        <li key={item.name}>
          <i style={swatchStyle(seriesStyle(index))} />
          {item.name}
        </li>
      ))}
    </ul>
  );
}

export function MultiLine({ chart }: { chart: DemoChart }) {
  const labels = chart.labels ?? [];
  const series = chart.series ?? [];
  const [solo, setSolo] = useState<string | null>(null);
  const [hoverSeries, setHoverSeries] = useState<string | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  if (!labels.length || !series.length) return null;
  const width = 340;
  const height = 150;
  const pad = 16;
  const values = series.flatMap((item) => item.points ?? []);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const x = (index: number) => pad + (index / Math.max(1, labels.length - 1)) * (width - pad * 2);
  const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);
  const focused = solo ?? hoverSeries;
  return (
    <figure className="demo-chart demo-multiline">
      <Caption chart={chart} />
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${chart.title}多序列图`}
        onPointerMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const ratio = (event.clientX - rect.left) / rect.width;
          const index = Math.round(
            ((Math.min(width - pad, Math.max(pad, ratio * width)) - pad) /
              (width - pad * 2)) *
              (labels.length - 1),
          );
          setHoverIndex(Math.min(labels.length - 1, Math.max(0, index)));
        }}
        onPointerLeave={() => setHoverIndex(null)}
      >
        {series.map((item, index) => {
          const style = seriesStyle(index);
          const points = (item.points ?? [])
            .map((value, pointIndex) => `${x(pointIndex).toFixed(1)},${y(value).toFixed(1)}`)
            .join(" ");
          const dimmed = focused !== null && focused !== item.name;
          return (
            <g key={item.name} opacity={dimmed ? 0.18 : 1}>
              {chart.type === "area" && (
                <polygon
                  className="demo-area-fill"
                  style={{ fill: style.color }}
                  points={`${x(0)},${height - pad} ${points} ${x(labels.length - 1)},${height - pad}`}
                />
              )}
              <polyline
                className="demo-series-line"
                style={{ stroke: style.color, strokeDasharray: style.dash ?? (item.dash ? "4 3" : undefined) }}
                points={points}
              />
            </g>
          );
        })}
        {hoverIndex !== null && (
          <g className="demo-line-hover" aria-hidden="true">
            <line
              x1={x(hoverIndex)}
              y1={pad}
              x2={x(hoverIndex)}
              y2={height - pad}
            />
            {series.map((item, index) => {
              const value = item.points?.[hoverIndex];
              if (value === undefined) return null;
              return (
                <circle
                  key={item.name}
                  cx={x(hoverIndex)}
                  cy={y(value)}
                  r={2.4}
                  style={{ fill: seriesStyle(index).color }}
                />
              );
            })}
          </g>
        )}
      </svg>
      <div className="demo-line-axis">
        <span>{labels[0]}</span>
        <span>{hoverIndex !== null ? labels[hoverIndex] : labels[labels.length - 1]}</span>
      </div>
      {hoverIndex !== null && (
        <div className="demo-multi-readout" role="status">
          <span>{labels[hoverIndex]}</span>
          {series.map((item) => (
            <em key={item.name}>
              {item.name} {fmt(item.points?.[hoverIndex] ?? 0, chart.unit)}
            </em>
          ))}
        </div>
      )}
      <div className="demo-legend">
        {series.map((item, index) => (
          <button
            key={item.name}
            type="button"
            aria-pressed={solo === item.name}
            onMouseEnter={() => setHoverSeries(item.name)}
            onMouseLeave={() => setHoverSeries(null)}
            onClick={() => setSolo((current) => (current === item.name ? null : item.name))}
          >
            <i style={swatchStyle(seriesStyle(index))} />
            {item.name}
          </button>
        ))}
      </div>
    </figure>
  );
}

export function Stacked({ chart }: { chart: DemoChart }) {
  const labels = chart.labels ?? [];
  const series = chart.series ?? [];
  // locked 与 hover 分开：点击锁定后移开鼠标仍保持选中，与 MultiLine 的
  // solo 行为一致；aria-pressed 只反映 locked。
  const [hovered, setHovered] = useState<string | null>(null);
  const [locked, setLocked] = useState<string | null>(null);
  const focused = locked ?? hovered;
  const width = 340;
  const height = 150;
  const pad = 14;
  const totals = labels.map((_, month) => series.reduce((sum, item) => sum + (item.values?.[month] ?? 0), 0));
  const max = Math.max(...totals, 1);
  const slot = (width - pad * 2) / Math.max(1, labels.length);
  const barWidth = slot * 0.62;
  return (
    <figure className="demo-chart demo-stacked">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}堆叠图`}>
        {labels.map((label, month) => {
          let cursor = 0;
          return (
            <g key={label}>
              {series.map((item, index) => {
                const value = item.values?.[month] ?? 0;
                const barHeight = (value / max) * (height - pad * 2);
                const y = height - pad - cursor - barHeight;
                cursor += barHeight;
                return (
                  <rect
                    key={item.name}
                    x={pad + month * slot + (slot - barWidth) / 2}
                    y={y}
                    width={barWidth}
                    height={Math.max(0, barHeight)}
                    fill={seriesStyle(index).color}
                    opacity={focused !== null && focused !== item.name ? 0.22 : 1}
                    onPointerEnter={() => setHovered(item.name)}
                    onPointerLeave={() => setHovered(null)}
                  >
                    <title>{`${label} · ${item.name} ${fmt(value, chart.unit)}`}</title>
                  </rect>
                );
              })}
            </g>
          );
        })}
      </svg>
      <div className="demo-line-axis">
        <span>{labels[0]}</span>
        <span>{labels[labels.length - 1]}</span>
      </div>
      <div className="demo-legend">
        {series.map((item, index) => (
          <button
            key={item.name}
            type="button"
            aria-pressed={locked === item.name}
            onMouseEnter={() => setHovered(item.name)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => setLocked((current) => (current === item.name ? null : item.name))}
          >
            <i style={swatchStyle(seriesStyle(index))} />
            {item.name}
          </button>
        ))}
      </div>
    </figure>
  );
}

export function Treemap({ chart }: { chart: DemoChart }) {
  const items = [...(chart.items ?? [])].sort((a, b) => b.value - a.value);
  const total = items.reduce((sum, item) => sum + item.value, 0) || 1;
  const width = 340;
  const height = 140;
  let cursor = 0;
  return (
    <figure className="demo-chart demo-treemap">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}树图`}>
        {items.map((item, index) => {
          const w = (item.value / total) * width;
          const x = cursor;
          cursor += w;
          return (
            <g key={item.label}>
              <rect x={x} y={0} width={Math.max(1, w - 2)} height={height} fill={seriesStyle(index).color} opacity={0.9 - index * 0.08}>
                <title>{`${item.label} · ${fmt(item.value, chart.unit)}`}</title>
              </rect>
              {w > 64 && (
                <>
                  <text x={x + 7} y={20} className="demo-treemap-label">{item.label}</text>
                  <text x={x + 7} y={36} className="demo-treemap-value">{fmt(item.value, chart.unit)}</text>
                </>
              )}
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

export function Funnel({ chart }: { chart: DemoChart }) {
  const items = chart.items ?? [];
  const max = Math.max(...items.map((item) => item.value), 1);
  return (
    <figure className="demo-chart demo-funnel">
      <Caption chart={chart} />
      <div className="demo-funnel-list">
        {items.map((item, index) => (
          <div className="demo-funnel-row" key={item.label}>
            <span className="demo-funnel-label">{item.label}</span>
            <span className="demo-funnel-track">
              <span style={{ width: `${(item.value / max) * 100}%`, opacity: 1 - index * 0.18 }} />
            </span>
            <span className="demo-funnel-value">
              {fmt(item.value)}
              {index > 0 && items[index - 1].value > 0 && (
                <em>{((item.value / items[index - 1].value) * 100).toFixed(0)}%</em>
              )}
            </span>
          </div>
        ))}
      </div>
    </figure>
  );
}

export function Radar({ chart }: { chart: DemoChart }) {
  const indicators = chart.indicators ?? [];
  const series = chart.series ?? [];
  const size = 170;
  const center = size / 2;
  const radius = 60;
  const point = (index: number, value: number) => {
    const angle = (Math.PI * 2 * index) / Math.max(1, indicators.length) - Math.PI / 2;
    const scale = Math.max(0, Math.min(100, value)) / 100;
    return [center + Math.cos(angle) * radius * scale, center + Math.sin(angle) * radius * scale] as const;
  };
  return (
    <figure className="demo-chart demo-radar">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${chart.title}雷达图`}>
        {[0.25, 0.5, 0.75, 1].map((scale) => (
          <polygon
            key={scale}
            className="demo-radar-grid"
            points={indicators
              .map((_, index) => {
                const angle = (Math.PI * 2 * index) / Math.max(1, indicators.length) - Math.PI / 2;
                return `${center + Math.cos(angle) * radius * scale},${center + Math.sin(angle) * radius * scale}`;
              })
              .join(" ")}
          />
        ))}
        {indicators.map((indicator, index) => {
          const [x, y] = point(index, 100);
          return <text key={indicator} x={x} y={y} className="demo-radar-label" textAnchor="middle">{indicator}</text>;
        })}
        {series.map((item, seriesIndex) => {
          const style = seriesStyle(seriesIndex);
          return (
            <polygon
              key={item.name}
              points={(item.values ?? []).map((value, index) => point(index, value).join(",")).join(" ")}
              fill={style.color}
              fillOpacity={0.08}
              stroke={style.color}
              strokeWidth={1.5}
              strokeDasharray={style.dash}
            />
          );
        })}
      </svg>
      <Legend series={series} />
    </figure>
  );
}

export function Gauge({ chart }: { chart: DemoChart }) {
  const value = chart.value ?? 0;
  const target = chart.target ?? 0;
  const peak = Math.max(value, target) * 1.15 || 1;
  const ratio = Math.min(1, value / peak);
  const width = 200;
  const height = 110;
  const cx = width / 2;
  const cy = 96;
  const radius = 70;
  const arc = (fraction: number) =>
    `M ${cx - radius} ${cy} A ${radius} ${radius} 0 0 1 ${cx + radius} ${cy}`;
  return (
    <figure className="demo-chart demo-gauge">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}仪表图`}>
        <path d={arc(1)} className="demo-gauge-track" />
        <path
          d={arc(ratio)}
          className="demo-gauge-value"
          style={{ strokeDasharray: `${Math.PI * radius * ratio} ${Math.PI * radius}` }}
        />
        <line
          x1={cx + Math.cos(Math.PI - Math.PI * (target / peak)) * (radius - 6)}
          y1={cy - Math.sin(Math.PI * (target / peak)) * (radius - 6)}
          x2={cx + Math.cos(Math.PI - Math.PI * (target / peak)) * (radius + 6)}
          y2={cy - Math.sin(Math.PI * (target / peak)) * (radius + 6)}
          className="demo-gauge-target"
        >
          <title>{`目标 ${fmt(target, chart.unit)}`}</title>
        </line>
        <text x={cx} y={78} textAnchor="middle" className="demo-gauge-number">
          {target ? `${((value / target) * 100).toFixed(0)}%` : "—"}
        </text>
        <text x={cx} y={94} textAnchor="middle" className="demo-gauge-note">
          {fmt(value, chart.unit)} / {fmt(target, chart.unit)}
        </text>
      </svg>
    </figure>
  );
}

export function BoxPlot({ chart }: { chart: DemoChart }) {
  const groups = chart.groups ?? [];
  const values = groups.flatMap((group) => [group.min, group.max]);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 1);
  const span = max - min || 1;
  const width = 340;
  const height = 150;
  const pad = 18;
  const slot = (width - pad * 2) / Math.max(1, groups.length);
  const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);
  return (
    <figure className="demo-chart demo-box">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}箱线图`}>
        {groups.map((group, index) => {
          const cx = pad + slot * index + slot / 2;
          return (
            <g key={group.label}>
              <line x1={cx} y1={y(group.min)} x2={cx} y2={y(group.max)} className="demo-box-whisker" />
              <rect
                x={cx - slot * 0.26}
                y={y(group.q3)}
                width={slot * 0.52}
                height={Math.max(1, y(group.q1) - y(group.q3))}
                className="demo-box-body"
              >
                <title>{`${group.label} 中位数 ${fmt(group.median, chart.unit)}`}</title>
              </rect>
              <line x1={cx - slot * 0.26} y1={y(group.median)} x2={cx + slot * 0.26} y2={y(group.median)} className="demo-box-median" />
              <text x={cx} y={height - 4} textAnchor="middle" className="demo-box-label">{group.label}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

export function Bubble({ chart }: { chart: DemoChart }) {
  const items = chart.items ?? [];
  const width = 340;
  const height = 160;
  const pad = 18;
  const xs = items.map((item) => Number(item.x ?? 0));
  const ys = items.map((item) => Number(item.y ?? 0));
  const sizes = items.map((item) => Number(item.size ?? 1));
  const xMin = Math.min(...xs, 0);
  const xMax = Math.max(...xs, 1);
  const yMin = Math.min(...ys, 0);
  const yMax = Math.max(...ys, 1);
  const sizeMax = Math.max(...sizes, 1);
  return (
    <figure className="demo-chart demo-bubble">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}气泡图`}>
        {items.map((item, index) => {
          const x = pad + ((Number(item.x ?? 0) - xMin) / (xMax - xMin || 1)) * (width - pad * 2);
          const y = height - pad - ((Number(item.y ?? 0) - yMin) / (yMax - yMin || 1)) * (height - pad * 2);
          const r = 6 + Math.sqrt(Number(item.size ?? 1) / sizeMax) * 18;
          const style = seriesStyle(index);
          return (
            <g key={item.label}>
              <circle cx={x} cy={y} r={r} fill={style.color} opacity={0.35} stroke={style.color} strokeDasharray={style.dash} />
              <text x={x} y={y + 3} textAnchor="middle" className="demo-bubble-label">{item.label}</text>
              <title>{`${item.label} · 订单 ${fmt(Number(item.x ?? 0))} · 收入 ${fmt(Number(item.y ?? 0), chart.unit)}`}</title>
            </g>
          );
        })}
      </svg>
      <div className="demo-scatter-axis">
        <span>订单</span>
        <span>收入</span>
      </div>
    </figure>
  );
}

export function Waterfall({ chart }: { chart: DemoChart }) {
  const items = chart.items ?? [];
  const width = 340;
  const height = 150;
  const pad = 16;
  const slot = (width - pad * 2) / Math.max(1, items.length);
  let cumulative = 0;
  const steps = items.map((item) => {
    if (item.kind === "total") {
      cumulative = item.value;
      return { ...item, start: 0, end: item.value };
    }
    const start = cumulative;
    cumulative += item.value;
    return { ...item, start, end: cumulative };
  });
  const max = Math.max(...steps.map((step) => Math.max(step.start, step.end)), 1);
  const min = Math.min(...steps.map((step) => Math.min(step.start, step.end)), 0);
  const span = max - min || 1;
  const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);
  return (
    <figure className="demo-chart demo-waterfall">
      <Caption chart={chart} />
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}瀑布图`}>
        {steps.map((step, index) => {
          const x = pad + index * slot + slot * 0.18;
          const barWidth = slot * 0.64;
          const top = Math.min(y(step.start), y(step.end));
          const bottom = Math.max(y(step.start), y(step.end));
          return (
            <g key={step.label}>
              <rect
                x={x}
                y={top}
                width={barWidth}
                height={Math.max(1, bottom - top)}
                className={step.value < 0 ? "demo-waterfall-negative" : step.kind === "total" ? "demo-waterfall-total" : "demo-waterfall-positive"}
              >
                <title>{`${step.label} ${step.kind === "total" ? "总额" : step.value >= 0 ? "+" : ""}${fmt(step.value, chart.unit)}`}</title>
              </rect>
            </g>
          );
        })}
        <line x1={pad} y1={y(0)} x2={width - pad} y2={y(0)} className="demo-axis" />
      </svg>
      <div className="demo-line-axis">
        <span>{items[0]?.label}</span>
        <span>{items[items.length - 1]?.label}</span>
      </div>
    </figure>
  );
}
