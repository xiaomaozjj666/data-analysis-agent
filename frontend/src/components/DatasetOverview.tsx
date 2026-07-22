import React from "react";
import { Database } from "lucide-react";
import type { DatasetProfile, ColumnInfo } from "../types";

interface DatasetOverviewProps {
  profile?: DatasetProfile | null;
}

interface TopValue {
  value: string;
  count: number;
}

// 仅读取后端实际提供的可选统计字段（类型未声明，通过索引签名防御性读取）：
// 有则展示，无则优雅省略，不臆造字段。
function readNumber(col: ColumnInfo, key: string): number | null {
  const v = col[key];
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}
function readTopValues(col: ColumnInfo): TopValue[] | null {
  const v = col["top_values"];
  if (!Array.isArray(v) || v.length === 0) return null;
  const parsed: TopValue[] = [];
  for (const item of v) {
    if (item && typeof item === "object") {
      const value = (item as Record<string, unknown>)["value"];
      const count = (item as Record<string, unknown>)["count"];
      parsed.push({ value: String(value ?? ""), count: typeof count === "number" ? count : 0 });
    }
  }
  return parsed.length ? parsed : null;
}

// 数值类型判断：依据 dtype 关键字（pandas int/float/decimal 等）。
const isNumericDtype = (dtype: string) => /int|float|number|decimal|double/i.test(dtype);

type QualityLevel = "good" | "warn" | "bad";
// 缺失率分级：<5% 绿，5-30% 琥珀，>30% 红。
const levelFromRate = (rate: number): QualityLevel =>
  rate < 0.05 ? "good" : rate <= 0.3 ? "warn" : "bad";

const fmtNum = (n: number) =>
  Number.isInteger(n) ? n.toLocaleString("en-US") : Number(n.toFixed(2)).toLocaleString("en-US");

const DatasetOverview = React.memo(function DatasetOverview({ profile }: DatasetOverviewProps) {
  const columns = profile?.column_info ?? [];
  const rowTotal = profile?.row_count ?? profile?.rows ?? 0;

  // 整体完整度 = 各列非空率均值。
  const completeness = columns.length
    ? columns.reduce((sum, c) => {
        const missing = c.missing ?? Math.max(0, rowTotal - (c.non_null ?? rowTotal));
        return sum + (rowTotal > 0 ? 1 - missing / rowTotal : 1);
      }, 0) / columns.length
    : 0;
  const scorePct = Math.round(completeness * 100);

  return (
    <section className="dataset-overview">
      <div className="overview-title">
        <span className="section-kicker">数据概览</span>
        <h2>字段质量</h2>
      </div>

      <div className="quality-summary">
        <div className="quality-summary-head">
          <span className="quality-summary-label">整体完整度</span>
          <span className="quality-summary-score">{scorePct}%</span>
        </div>
        <div
          className="quality-summary-bar"
          role="progressbar"
          aria-valuenow={scorePct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="数据整体完整度"
        >
          <div className="quality-summary-bar-fill" style={{ width: `${scorePct}%` }} />
        </div>
      </div>

      <div className="quality-table-wrap">
        <table className="quality-table">
          <thead>
            <tr>
              <th>字段</th>
              <th>类型</th>
              <th>缺失</th>
              <th>缺失率</th>
              <th>唯一值</th>
              <th>质量</th>
              <th>统计概要</th>
            </tr>
          </thead>
          <tbody>
            {columns.length === 0 ? (
              <tr><td colSpan={7} className="quality-empty">暂无字段信息</td></tr>
            ) : columns.map((column) => {
              const missing = column.missing ?? Math.max(0, rowTotal - (column.non_null ?? rowTotal));
              const rate = rowTotal > 0 ? missing / rowTotal : 0;
              const ratePct = Math.round(rate * 100);
              const level = levelFromRate(rate);
              const numeric = isNumericDtype(column.dtype);
              const min = readNumber(column, "min");
              const max = readNumber(column, "max");
              const mean = readNumber(column, "mean");
              const topValues = readTopValues(column);
              return (
                <tr key={column.name}>
                  <td>
                    <span className="quality-field"><Database size={13} />{column.name}</span>
                  </td>
                  <td className="quality-dtype">{column.dtype}</td>
                  <td className="quality-num">{fmtNum(missing)}</td>
                  <td className="quality-num">{ratePct}%</td>
                  <td className="quality-num">{fmtNum(column.unique)}</td>
                  <td className="quality-dot-cell">
                    <span className={`quality-dot is-${level}`} title={`缺失率 ${ratePct}%`} />
                  </td>
                  <td className="quality-stats">
                    {numeric && (min != null || max != null || mean != null) ? (
                      <span className="quality-stats-line">
                        {min != null && <span>min {fmtNum(min)}</span>}
                        {max != null && <span>max {fmtNum(max)}</span>}
                        {mean != null && <span>均值 {fmtNum(mean)}</span>}
                      </span>
                    ) : topValues && topValues.length > 0 ? (
                      <span className="quality-stats-line">
                        {topValues.slice(0, 3).map((t, i) => (
                          <span key={`${t.value}-${i}`} className="quality-top-value" title={`${t.value} · ${fmtNum(t.count)}`}>
                            {t.value}<i>{fmtNum(t.count)}</i>
                          </span>
                        ))}
                      </span>
                    ) : (
                      <span className="quality-stats-muted">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
});

export default DatasetOverview;
