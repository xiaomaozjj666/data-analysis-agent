import { useEffect, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../utils/api";
import type { DemoChart, DemoInsights } from "../types";
import { BoxPlot, Bubble, Funnel, Gauge, MultiLine, Radar, Stacked, Treemap, Waterfall } from "./DemoExtraCharts";
import "./DemoBrief.css";

const CHART_TYPES: Record<string, string> = {
  line: "趋势",
  bar: "结构",
  histogram: "分布",
  scatter: "关系",
  donut: "占比",
  heatmap: "密度",
  multiline: "多序列",
  stacked: "堆叠",
  area: "面积",
  treemap: "层级",
  funnel: "漏斗",
  radar: "对比",
  gauge: "目标",
  box: "分布",
  bubble: "气泡",
  waterfall: "瀑布",
};

const DONUT_TOKENS = ["--donut-1", "--donut-2", "--donut-3", "--donut-4", "--donut-5", "--donut-6", "--donut-7", "--donut-8"];

function formatValue(value: number, unit?: string): string {
  const formatted = Math.abs(value) >= 1000
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return unit ? `${formatted} ${unit}` : formatted;
}

function LineChart({ chart }: { chart: DemoChart }) {
  const points = chart.points ?? [];
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) return null;
  const width = 340;
  const height = 132;
  const pad = 12;
  const values = points.map((point) => point.value);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const coords = points.map((point, index) => {
    const x = pad + (index / (points.length - 1)) * (width - pad * 2);
    const y = height - pad - ((point.value - min) / span) * (height - pad * 2);
    return [x, y] as const;
  });
  const path = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const peak = values.reduce((best, value, index) => (value > values[best] ? index : best), 0);
  const area =
    `M ${coords[0][0]},${height - pad} L ` +
    coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L ") +
    ` L ${coords[coords.length - 1][0]},${height - pad} Z`;
  return (
    <figure className="demo-chart demo-line">
      <figcaption>
        <span className="demo-chart-kicker">{CHART_TYPES[chart.type] ?? chart.type}</span>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        tabIndex={0}
        aria-label={`${chart.title}折线图${hover !== null ? `，当前 ${points[hover]?.label} ${formatValue(points[hover]?.value ?? 0, chart.unit)}` : "，使用左右方向键查看数值"}`}
        onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          setHover((current) => {
            const base = current ?? points.length - 1;
            const next = event.key === "ArrowRight" ? base + 1 : base - 1;
            return Math.min(points.length - 1, Math.max(0, next));
          });
        }}
        onPointerMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const ratio = (event.clientX - rect.left) / rect.width;
          const plotX = Math.min(width - pad, Math.max(pad, ratio * width));
          const index = Math.round(
            ((plotX - pad) / (width - pad * 2)) * (points.length - 1),
          );
          setHover(Math.min(points.length - 1, Math.max(0, index)));
        }}
        onPointerLeave={() => setHover(null)}
      >
        <path className="demo-line-area" d={area} />
        <polyline className="demo-line-path" points={path} />
        {coords.map(([x, y], index) => (
          <circle key={index} className="demo-line-dot" cx={x} cy={y} r={1.8}>
            <title>{`${points[index]?.label} · ${formatValue(points[index]?.value ?? 0, chart.unit)}`}</title>
          </circle>
        ))}
        {peak >= 0 && coords[peak] && (
          <g className="demo-line-peak" aria-hidden="true">
            <text x={coords[peak][0]} y={Math.max(9, coords[peak][1] - 7)} textAnchor="middle">
              峰值
            </text>
          </g>
        )}
        {hover !== null && coords[hover] && (
          <g className="demo-line-hover" aria-hidden="true">
            <line x1={coords[hover][0]} y1={pad} x2={coords[hover][0]} y2={height - pad} />
            <circle cx={coords[hover][0]} cy={coords[hover][1]} r={3.2} />
            <text x={Math.min(width - 78, Math.max(4, coords[hover][0] - 32))} y={12}>
              {formatValue(points[hover]?.value ?? 0, chart.unit)}
            </text>
          </g>
        )}
      </svg>
      <div className="demo-line-axis">
        <span>{points[0]?.label}</span>
        <span>{hover !== null ? points[hover]?.label : points[points.length - 1]?.label}</span>
      </div>
    </figure>
  );
}

function Bars({
  chart,
  onSelect,
  showRaw,
  selectedLabel,
  selectedTitle,
}: {
  chart: DemoChart;
  onSelect?: (label: string, value: number) => void;
  showRaw?: boolean;
  selectedLabel?: string;
  selectedTitle?: string;
}) {
  const items = (showRaw && chart.raw_items) || chart.items || [];
  const linked = selectedTitle === chart.title;
  const max = Math.max(...items.map((item) => Math.abs(item.value)), 1);
  const peakIndex = items.findIndex((item) => Math.abs(item.value) === max);
  return (
    <figure className="demo-chart demo-bars">
      <figcaption>
        <span className="demo-chart-kicker">{CHART_TYPES[chart.type] ?? chart.type}</span>
        <strong>{chart.title}</strong>
        {chart.subtitle && (
          <small>
            {chart.subtitle}
            {showRaw && chart.raw_items ? " · 包含极端值" : ""}
          </small>
        )}
      </figcaption>
      <div className="demo-bar-list">
        {items.map((item, itemIndex) => (
          <button
            type="button"
            className={`demo-bar-row${linked && selectedLabel === item.label ? " is-selected" : ""}${linked && selectedLabel && selectedLabel !== item.label ? " is-dim" : ""}`}
            key={item.label}
            aria-pressed={linked && selectedLabel === item.label}
            aria-label={`${chart.title}：${item.label} ${formatValue(item.value, chart.unit)}`}
            title={`${item.label} · ${formatValue(item.value, chart.unit)}`}
            onClick={() => onSelect?.(item.label, item.value)}
          >
            <span className="demo-bar-label" title={item.label}>{item.label}</span>
            <span className="demo-bar-track">
              <span style={{ width: `${Math.max(2, (Math.abs(item.value) / max) * 100)}%` }} />
            </span>
            <span className="demo-bar-value">
              {itemIndex === peakIndex && <em className="demo-bar-flag">峰值</em>}
              {formatValue(item.value, chart.unit)}
            </span>
          </button>
        ))}
      </div>
    </figure>
  );
}

function Scatter({ chart }: { chart: DemoChart }) {
  const points = chart.samples ?? [];
  const outliers = chart.outliers ?? [];
  const all = [...points, ...outliers];
  if (!all.length) return null;
  const width = 340;
  const height = 150;
  const pad = 14;
  if (!points.length) {
    return (
      <figure className="demo-chart demo-scatter">
        <figcaption>
          <strong>{chart.title}</strong>
          {chart.subtitle && <small>{chart.subtitle}</small>}
        </figcaption>
        <p className="demo-chart-empty">暂无主体样本；离群点数值见 KPI 与明细。</p>
      </figure>
    );
  }
  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => point[1]);
  let xMin = Math.min(...xs);
  let xMax = Math.max(...xs);
  let yMin = Math.min(...ys);
  let yMax = Math.max(...ys);
  if (xMax === xMin) { xMin -= 1; xMax += 1; }
  if (yMax === yMin) { yMin -= 1; yMax += 1; }
  const map = (point: [number, number]) => {
    const x = pad + ((point[0] - xMin) / (xMax - xMin || 1)) * (width - pad * 2);
    const y = height - pad - ((point[1] - yMin) / (yMax - yMin || 1)) * (height - pad * 2);
    return [
      Math.min(width - pad, Math.max(pad, x)),
      Math.min(height - pad, Math.max(pad, y)),
    ] as const;
  };
  return (
    <figure className="demo-chart demo-scatter">
      <figcaption>
        <span className="demo-chart-kicker">{CHART_TYPES[chart.type] ?? chart.type}</span>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}散点图`}>
        <line className="demo-axis" x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} />
        <line className="demo-axis" x1={pad} y1={pad} x2={pad} y2={height - pad} />
        {points.map((point, index) => {
          const [x, y] = map(point);
          return (
            <circle key={index} className="demo-scatter-dot" cx={x} cy={y} r={1.7}>
              <title>{`${chart.x_label} ${point[0]} · ${chart.y_label} ${point[1]}`}</title>
            </circle>
          );
        })}
        {outliers.map((point, index) => {
          const [x, y] = map(point);
          return (
            <circle key={`o${index}`} className="demo-scatter-outlier" cx={x} cy={y} r={2.4}>
              <title>{`离群点 · ${chart.x_label} ${point[0]} · ${chart.y_label} ${point[1]}`}</title>
            </circle>
          );
        })}
        {outliers.length > 0 && (
          <text className="demo-chart-note" x={width - pad} y={pad + 10} textAnchor="end">
            离群 {outliers.length} 个
          </text>
        )}
      </svg>
      <div className="demo-scatter-axis">
        <span>{chart.x_label}</span>
        <span>{chart.y_label}</span>
      </div>
    </figure>
  );
}

function Heatmap({ chart }: { chart: DemoChart }) {
  const values = chart.values ?? [];
  if (!values.length) return null;
  return (
    <figure className="demo-chart demo-heatmap">
      <figcaption>
        <span className="demo-chart-kicker">{CHART_TYPES[chart.type] ?? chart.type}</span>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <div
        className="demo-heat-grid"
        style={{ gridTemplateColumns: `repeat(${chart.cols ?? values[0].length}, minmax(0, 1fr))` }}
        role="img"
        aria-label={`${chart.title}密度图`}
      >
        {values.flatMap((row, rowIndex) =>
          row.map((value, colIndex) => (
            <span
              key={`${rowIndex}-${colIndex}`}
              className="demo-heat-cell"
              style={{ opacity: 0.04 + value * 0.96 }}
              title={`密度 ${(value * 100).toFixed(0)}%`}
            />
          )),
        )}
      </div>
      <div className="demo-scatter-axis">
        <span>{chart.x_label}</span>
        <span>{chart.y_label}</span>
      </div>
    </figure>
  );
}

function Donut({
  chart,
  onSelect,
  selectedLabel,
  selectedTitle,
}: {
  chart: DemoChart;
  onSelect?: (label: string, value: number) => void;
  selectedLabel?: string;
  selectedTitle?: string;
}) {
  const items = chart.items ?? [];
  const total = items.reduce((sum, item) => sum + Math.abs(item.value), 0) || 1;
  const [hover, setHover] = useState<number | null>(null);
  let cursor = 0;
  const segments = items.map((item) => {
    const start = cursor;
    cursor += (Math.abs(item.value) / total) * 100;
    return { start, end: cursor };
  });
  const stops = items.map(
    (item, index) =>
      `var(${DONUT_TOKENS[index % DONUT_TOKENS.length]}) ${segments[index].start}% ${segments[index].end}%`,
  );
  const pickSegment = (event: ReactPointerEvent<HTMLSpanElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const dx = event.clientX - (rect.left + rect.width / 2);
    const dy = event.clientY - (rect.top + rect.height / 2);
    const angle = (Math.atan2(dy, dx) * 180) / Math.PI + 90;
    const normalized = (angle + 360) % 360;
    const index = segments.findIndex(
      (segment) => normalized >= segment.start && normalized < segment.end,
    );
    setHover(index >= 0 ? index : null);
  };
  return (
    <figure className="demo-chart demo-donut">
      <figcaption>
        <span className="demo-chart-kicker">{CHART_TYPES[chart.type] ?? chart.type}</span>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <div className="demo-donut-body">
        <span
          className="demo-donut-ring"
          style={{ background: `conic-gradient(${stops.join(", ")})`, cursor: "pointer" }}
          role="img"
          aria-label={`${chart.title}占比环`}
          onPointerMove={pickSegment}
          onPointerLeave={() => setHover(null)}
          onClick={() => {
            if (hover !== null && items[hover]) {
              onSelect?.(items[hover].label, items[hover].value);
            }
          }}
        >
          <span className="demo-donut-center">
            {hover !== null && items[hover]
              ? `${items[hover].label} ${((Math.abs(items[hover].value) / total) * 100).toFixed(0)}%`
              : ""}
          </span>
        </span>
        <ul className="demo-donut-legend">
          {items.map((item, index) => (
            <li key={item.label}>
              <button
                type="button"
                aria-pressed={selectedTitle === chart.title && selectedLabel === item.label}
                aria-label={`${chart.title}：${item.label} ${((Math.abs(item.value) / total) * 100).toFixed(0)}%`}
                onClick={() => onSelect?.(item.label, item.value)}
              >
                <i style={{ background: `var(${DONUT_TOKENS[index % DONUT_TOKENS.length]})` }} />
                <span>{item.label}</span>
                <small>{((Math.abs(item.value) / total) * 100).toFixed(0)}%</small>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </figure>
  );
}

function Chart({
  chart,
  onSelect,
  showRaw,
  selectedLabel,
  selectedTitle,
}: {
  chart: DemoChart;
  onSelect?: (label: string, value: number) => void;
  showRaw?: boolean;
  selectedLabel?: string;
  selectedTitle?: string;
}) {
  if (chart.type === "line") return <LineChart chart={chart} />;
  if (chart.type === "scatter") return <Scatter chart={chart} />;
  if (chart.type === "heatmap") return <Heatmap chart={chart} />;
  if (chart.type === "multiline" || chart.type === "area") return <MultiLine chart={chart} />;
  if (chart.type === "stacked") return <Stacked chart={chart} />;
  if (chart.type === "treemap") return <Treemap chart={chart} />;
  if (chart.type === "funnel") return <Funnel chart={chart} />;
  if (chart.type === "radar") return <Radar chart={chart} />;
  if (chart.type === "gauge") return <Gauge chart={chart} />;
  if (chart.type === "box") return <BoxPlot chart={chart} />;
  if (chart.type === "bubble") return <Bubble chart={chart} />;
  if (chart.type === "waterfall") return <Waterfall chart={chart} />;
  if (chart.type === "donut")
    return (
      <Donut
        chart={chart}
        onSelect={onSelect}
        selectedLabel={selectedLabel}
        selectedTitle={selectedTitle}
      />
    );
  return (
    <Bars
      chart={chart}
      onSelect={onSelect}
      showRaw={showRaw}
      selectedLabel={selectedLabel}
      selectedTitle={selectedTitle}
    />
  );
}

const TRUST_STEPS = [
  { key: "sha", label: "原始文件", detail: "原始 CSV 只读一次，哈希记下来。" },
  { key: "policy", label: "谁能看", detail: "先看你是谁，再决定给你看哪些行。" },
  { key: "replay", label: "再算一遍", detail: "拿原始文件和口径重算，数字对得上才继续。" },
  { key: "sign", label: "签名", detail: "每条记录都签个名，公钥跟证据一起给你。" },
  { key: "bundle", label: "打包", detail: "把明细、账本和公钥装进一个包，拿走也能验。" },
];

function principalFromScope(
  scope: DemoInsights["scope"],
  principals: { id: string }[],
): string | null {
  const id = scope?.principal_id;
  return id && principals.some((principal) => principal.id === id) ? id : null;
}

// 访问策略有两种：行级过滤（RLS）与字段脱敏（PII）。年级演示只有 PII，
// 旧逻辑只看 rls_column 会把「谁能看」错误置灰；这里按实际启用的能力写文案。
function policyDetail(scope: DemoInsights["scope"]): string {
  const rls = Boolean(scope?.rls_column);
  const pii = (scope?.pii_columns?.length ?? 0) > 0;
  if (rls && pii) {
    return "先看你是谁：按行级过滤（RLS）只给你该看的行，敏感字段再按字段脱敏（PII）显示。";
  }
  if (rls) return "先看你是谁：按行级过滤（RLS）只给你该看的行。";
  if (pii) return "先看你是谁：敏感字段按字段脱敏（PII）显示，其余行正常可见。";
  return TRUST_STEPS[1].detail;
}

export default function DemoBrief({
  insights,
  rows = [],
  principals = [],
  sessionId,
}: {
  insights: DemoInsights;
  rows?: Record<string, unknown>[];
  principals?: { id: string; label: string; policy: Record<string, unknown> }[];
  sessionId?: string;
}) {
  const [liveInsights, setLiveInsights] = useState(insights);
  const [liveRows, setLiveRows] = useState(rows);
  // 若后端在 scope.principal_id 里声明了当前主体，初始就选中它；
  // principals[0] 只是兜底，避免标题/KPI 与高亮的主体按钮不一致。
  const [activePrincipal, setActivePrincipal] = useState(() =>
    principalFromScope(insights.scope, principals) ?? principals[0]?.id ?? "",
  );
  const [switching, setSwitching] = useState(false);
  useEffect(() => {
    setLiveInsights(insights);
    // rows / principals 的默认值是字面量 []，每次渲染都是新数组；直接 setState
    // 会让 effect 每次提交都换引用 → 无限重渲染（2026-11-20 vitest 实测挂死）。
    // 内容逐项相同就保留旧引用，React 才会 bail out。
    setLiveRows((previous) => {
      if (previous === rows) return previous;
      if (
        previous.length === rows.length &&
        previous.every((row, index) => row === rows[index])
      ) {
        return previous;
      }
      return rows;
    });
    setActivePrincipal((previous) =>
      principalFromScope(insights.scope, principals) ??
      (principals.some((principal) => principal.id === previous)
        ? previous
        : principals[0]?.id ?? ""),
    );
  }, [insights, rows, principals]);
  const data = liveInsights;
  const [activeStep, setActiveStep] = useState(0);
  const [showRaw, setShowRaw] = useState(false);
  const [selected, setSelected] = useState<{
    label: string;
    value: number;
    title: string;
    unit?: string;
    dimension?: string;
  } | null>(null);
  const policyOn = Boolean(
    data.scope?.rls_column || (data.scope?.pii_columns?.length ?? 0) > 0,
  );
  const hasOutliers = data.charts.some(
    (chart) => (chart.excluded ?? 0) > 0 && Boolean(chart.raw_items),
  );
  const stepEnabled = (key: string) => key !== "policy" || policyOn;
  return (
    <section className="demo-brief" aria-label="预分析速览">
      <div className="demo-trust" aria-label="信任链">
        {TRUST_STEPS.map((step, index) => (
          <button
            key={step.key}
            type="button"
            aria-pressed={activeStep === index}
            className={`demo-trust-step${activeStep === index ? " is-active" : ""}${stepEnabled(step.key) ? "" : " is-off"}`}
            onClick={() => setActiveStep(index)}
          >
            <span className="demo-trust-index">0{index + 1}</span>
            <span className="demo-trust-label">{step.label}</span>
          </button>
        ))}
      </div>
      <p className="demo-trust-detail" role="status">
        {TRUST_STEPS[activeStep].key === "policy"
          ? policyDetail(data.scope)
          : TRUST_STEPS[activeStep].detail}
        {!stepEnabled(TRUST_STEPS[activeStep].key) && "（该演示未启用访问策略）"}
      </p>
      {principals.length > 1 && (
        <div
          className="demo-principals"
          role="group"
          aria-label="切换访问主体"
          aria-busy={switching}
        >
          {principals.map((principal) => (
            <button
              key={principal.id}
              type="button"
              aria-pressed={activePrincipal === principal.id}
              aria-disabled={switching || !sessionId}
              onClick={async () => {
                if (!sessionId || switching) return;
                setSwitching(true);
                try {
                  const res = await api<{
                    insights: DemoInsights;
                    preview: Record<string, unknown>[];
                  }>(`/api/sessions/${sessionId}/audit-policy/switch`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ principal_id: principal.id }),
                  });
                  setLiveInsights(res.insights);
                  setLiveRows(res.preview);
                  setActivePrincipal(principal.id);
                  setSelected(null);
                } finally {
                  setSwitching(false);
                }
              }}
            >
              {principal.label}
            </button>
          ))}
          <span role="status" className="sr-only">
            {switching ? "正在切换访问主体…" : ""}
          </span>
        </div>
      )}
      <header className="demo-brief-head">
        <span className="demo-brief-kicker">预分析</span>
        <h2>{data.headline}</h2>
        <p>{data.subline}</p>
        <p className="demo-brief-meta">
          {data.charts.length} 张视图 · {data.scope?.visible ?? liveRows.length} 条原始记录
          {data.scope?.rls_column ? " · 已按主体过滤" : ""}
        </p>
        {data.scope && data.scope.visible < data.scope.rows && (
          <p className="demo-scope">
            当前主体可见 {data.scope.visible} 行，共 {data.scope.rows} 行。
          </p>
        )}
        {hasOutliers && (
          <div className="demo-outlier-toggle" role="group" aria-label="极端值处理">
            <button
              type="button"
              aria-pressed={!showRaw}
              onClick={() => {
                setShowRaw(false);
                setSelected(null);
              }}
            >
              剔除异常值
            </button>
            <button
              type="button"
              aria-pressed={showRaw}
              onClick={() => {
                setShowRaw(true);
                setSelected(null);
              }}
            >
              保留全量值
            </button>
            <span>默认剔除异常值，免得几个大数把图压扁；切过去可以看全量值。</span>
          </div>
        )}
      </header>
      <div className="demo-kpis">
        {data.kpis.map((kpi) => (
          <div className="demo-kpi" key={kpi.label}>
            <span>{kpi.label}</span>
            <strong>
              {kpi.value}
              {kpi.unit && <small>{kpi.unit}</small>}
            </strong>
            {kpi.note && <em>{kpi.note}</em>}
          </div>
        ))}
      </div>
      <div className="demo-chart-grid">
        {data.charts.map((chart) => (
          <Chart
            chart={chart}
            key={`${chart.title}:${activePrincipal}:${showRaw}`}
            showRaw={showRaw}
            selectedLabel={selected?.label}
            selectedTitle={selected?.title}
            onSelect={(label, value) =>
              setSelected({
                label,
                value,
                title: chart.title,
                unit: chart.unit,
                dimension: chart.dimension,
              })
            }
          />
        ))}
      </div>
      {!selected && (
        <p className="demo-hint">点一下柱子或图例，下面会列出是哪几行。</p>
      )}
      <p className={`demo-selection${selected ? "" : " is-empty"}`} role="status">
        {selected ? (
          <>
            已选 <strong>{selected.label}</strong> · {formatValue(selected.value, selected.unit)}
            <span>来自「{selected.title}」，按当前可见行计算。</span>
            <button type="button" onClick={() => setSelected(null)}>清除</button>
          </>
        ) : (
          ""
        )}
      </p>
      {selected?.dimension && (
        <div className="demo-drilldown">
          {(() => {
            const matched = liveRows.filter(
              (row) => String(row[selected.dimension!] ?? "") === selected.label,
            );
            const columns = matched.length
              ? Object.keys(matched[0]).slice(0, 6)
              : [];
            return (
              <>
                <div className="demo-drilldown-head">
                  <strong>下钻明细</strong>
                  <small>
                    {matched.length
                      ? `当前预览中命中 ${matched.length} 行，显示前 ${Math.min(8, matched.length)} 行`
                      : "「其他」为合并项，不展开逐行明细"}
                  </small>
                </div>
                {matched.length > 0 && (
                  <div
                    className="demo-drilldown-scroll"
                    tabIndex={0}
                    role="region"
                    aria-label="下钻明细表格"
                  >
                    <table>
                      <caption className="sr-only">
                        {selected.label} 的下钻明细，前 {Math.min(8, matched.length)} 行
                      </caption>
                      <thead>
                        <tr>
                          {columns.map((column) => (
                            <th key={column} scope="col">{column}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {matched.slice(0, 8).map((row, index) => (
                          <tr key={index}>
                            {columns.map((column) => (
                              <td key={column}>{String(row[column] ?? "—")}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            );
          })()}
        </div>
      )}
      {data.findings.length > 0 && (
        <ul className="demo-findings">
          {data.findings.map((finding) => (
            <li key={finding}>{finding}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
