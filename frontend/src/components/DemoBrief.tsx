import type { DemoChart, DemoInsights } from "../types";
import "./DemoBrief.css";

const DONUT_TOKENS = ["--donut-1", "--donut-2", "--donut-3", "--donut-4", "--donut-5", "--donut-6", "--donut-7", "--donut-8"];

function formatValue(value: number, unit?: string): string {
  const formatted = Math.abs(value) >= 1000
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return unit ? `${formatted} ${unit}` : formatted;
}

function LineChart({ chart }: { chart: DemoChart }) {
  const points = chart.points ?? [];
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
  const area = `M ${coords[0][0]},${height - pad} L ${path.replaceAll(",", " ").split(" ").join(" L ")} L ${coords[coords.length - 1][0]},${height - pad} Z`;
  return (
    <figure className="demo-chart demo-line">
      <figcaption>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}折线图`}>
        <path className="demo-line-area" d={area} />
        <polyline className="demo-line-path" points={path} />
        {coords.map(([x, y], index) => (
          <circle key={index} className="demo-line-dot" cx={x} cy={y} r={1.8} />
        ))}
      </svg>
      <div className="demo-line-axis">
        <span>{points[0]?.label}</span>
        <span>{points[points.length - 1]?.label}</span>
      </div>
    </figure>
  );
}

function Bars({ chart }: { chart: DemoChart }) {
  const items = chart.items ?? [];
  const max = Math.max(...items.map((item) => Math.abs(item.value)), 1);
  return (
    <figure className="demo-chart demo-bars">
      <figcaption>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <div className="demo-bar-list">
        {items.map((item) => (
          <div className="demo-bar-row" key={item.label}>
            <span className="demo-bar-label" title={item.label}>{item.label}</span>
            <span className="demo-bar-track">
              <span style={{ width: `${Math.max(2, (Math.abs(item.value) / max) * 100)}%` }} />
            </span>
            <span className="demo-bar-value">{formatValue(item.value, chart.unit)}</span>
          </div>
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
  const xs = all.map((point) => point[0]);
  const ys = all.map((point) => point[1]);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs) || 1;
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys) || 1;
  const map = (point: [number, number]) => [
    pad + ((point[0] - xMin) / (xMax - xMin || 1)) * (width - pad * 2),
    height - pad - ((point[1] - yMin) / (yMax - yMin || 1)) * (height - pad * 2),
  ] as const;
  return (
    <figure className="demo-chart demo-scatter">
      <figcaption>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${chart.title}散点图`}>
        <line className="demo-axis" x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} />
        <line className="demo-axis" x1={pad} y1={pad} x2={pad} y2={height - pad} />
        {points.map((point, index) => {
          const [x, y] = map(point);
          return <circle key={index} className="demo-scatter-dot" cx={x} cy={y} r={1.7} />;
        })}
        {outliers.map((point, index) => {
          const [x, y] = map(point);
          return <circle key={`o${index}`} className="demo-scatter-outlier" cx={x} cy={y} r={2.4} />;
        })}
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

function Donut({ chart }: { chart: DemoChart }) {
  const items = chart.items ?? [];
  const total = items.reduce((sum, item) => sum + Math.abs(item.value), 0) || 1;
  let cursor = 0;
  const stops = items.map((item, index) => {
    const start = cursor;
    cursor += (Math.abs(item.value) / total) * 100;
    return `var(${DONUT_TOKENS[index % DONUT_TOKENS.length]}) ${start}% ${cursor}%`;
  });
  return (
    <figure className="demo-chart demo-donut">
      <figcaption>
        <strong>{chart.title}</strong>
        {chart.subtitle && <small>{chart.subtitle}</small>}
      </figcaption>
      <div className="demo-donut-body">
        <span
          className="demo-donut-ring"
          style={{ background: `conic-gradient(${stops.join(", ")})` }}
          role="img"
          aria-label={`${chart.title}占比环`}
        />
        <ul className="demo-donut-legend">
          {items.map((item, index) => (
            <li key={item.label}>
              <i style={{ background: `var(${DONUT_TOKENS[index % DONUT_TOKENS.length]})` }} />
              <span>{item.label}</span>
              <small>{((Math.abs(item.value) / total) * 100).toFixed(0)}%</small>
            </li>
          ))}
        </ul>
      </div>
    </figure>
  );
}

function Chart({ chart }: { chart: DemoChart }) {
  if (chart.type === "line") return <LineChart chart={chart} />;
  if (chart.type === "scatter") return <Scatter chart={chart} />;
  if (chart.type === "heatmap") return <Heatmap chart={chart} />;
  if (chart.type === "donut") return <Donut chart={chart} />;
  return <Bars chart={chart} />;
}

export default function DemoBrief({ insights }: { insights: DemoInsights }) {
  return (
    <section className="demo-brief" aria-label="预分析速览">
      <header className="demo-brief-head">
        <span className="demo-brief-kicker">预分析 · 打开即有</span>
        <h2>{insights.headline}</h2>
        <p>{insights.subline}</p>
      </header>
      <div className="demo-kpis">
        {insights.kpis.map((kpi) => (
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
        {insights.charts.map((chart) => (
          <Chart chart={chart} key={chart.title} />
        ))}
      </div>
      {insights.findings.length > 0 && (
        <ul className="demo-findings">
          {insights.findings.map((finding) => (
            <li key={finding}>{finding}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
