import React, { useMemo, useState } from "react";
import { Search } from "lucide-react";

// React.memo：rows 仅在 session 切换时变化，但 App 每次输入 task 或
// 刷新历史都会重渲染。memo 让 DataTable 跳过这些场景，避免重新生成
// 几百个 <td>。
const DataTable = React.memo(function DataTable({ rows }) {
  const [search, setSearch] = useState("");
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("asc");
  const columns = useMemo(() => Object.keys(rows?.[0] || {}), [rows]);

  // 列类型推断：取第一个非空值判断 number / date / text。
  // date 检测附加 /[-/:]/ 前置过滤，避免 "2023" 这类纯数字字符串被
  // Date.parse 误判为合法日期（V8 会把 "2023" 当作年份解析）。
  const colTypes = useMemo(() => {
    const types = {};
    for (const col of columns) {
      const sample = (rows || []).find((r) => r[col] != null)?.[col];
      if (typeof sample === "number") types[col] = "number";
      else if (typeof sample === "string" && /[-/:]/.test(sample) && !isNaN(Date.parse(sample))) types[col] = "date";
      else types[col] = "text";
    }
    return types;
  }, [rows, columns]);

  // 客户端搜索过滤：在 preview（前 100 行）范围内按任意列包含关键词匹配。
  const filtered = useMemo(() => {
    if (!rows?.length) return [];
    if (!search.trim()) return rows;
    const q = search.toLowerCase();
    return rows.filter((row) =>
      columns.some((col) => String(row[col] ?? "").toLowerCase().includes(q))
    );
  }, [rows, search, columns]);

  // 列排序：null 值统一沉底，number 用数值比较，其余用 localeCompare。
  const sorted = useMemo(() => {
    if (!sortCol) return filtered;
    return [...filtered].sort((a, b) => {
      const av = a[sortCol], bv = b[sortCol];
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv));
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [filtered, sortCol, sortDir]);

  const toggleSort = (col) => {
    if (sortCol === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir("asc");
    }
  };

  // 列类型 → 列头小标签（# 数值 / A 文本 / 📅 日期）
  const typeLabel = (t) => (t === "number" ? "#" : t === "date" ? "📅" : "A");

  if (!rows?.length) return <div className="empty-row">没有可预览的数据</div>;

  return (
    <div>
      <div className="data-table-toolbar">
        <Search size={14} className="data-table-search-icon" />
        <input
          className="data-table-search"
          type="text"
          placeholder="搜索行内容…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="过滤数据预览行"
        />
        <span className="data-table-count">
          显示 {sorted.length}/{rows.length} 行
        </span>
      </div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th className="row-number">#</th>
              {columns.map((column) => (
                <th
                  key={column}
                  onClick={() => toggleSort(column)}
                  className={sortCol === column ? `is-sorted is-${sortDir}` : ""}
                  title="点击切换升序 / 降序"
                >
                  {column}
                  <span className="data-table-type-badge">{typeLabel(colTypes[column])}</span>
                  {sortCol === column ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr><td className="row-number">—</td><td colSpan={columns.length} style={{ textAlign: "center", color: "var(--fg-muted)" }}>没有匹配的行</td></tr>
            ) : sorted.map((row, index) => (
              <tr key={index}>
                <td className="row-number">{index + 1}</td>
                {columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
});

export default DataTable;
