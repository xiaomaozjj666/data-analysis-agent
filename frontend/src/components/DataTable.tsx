import React, { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";

type Row = Record<string, unknown>;
type ColType = "number" | "date" | "text";
type SortDir = "asc" | "desc";
interface SortKey { col: string; dir: SortDir; }

interface DataTableProps {
  rows?: Row[] | null;
}

// 增量加载：首屏 100 行，滚动到底部自动追加 100 行；
// 上限 5000 行避免 DOM 膨胀，超出部分引导用户搜索或导出。
const INITIAL_VISIBLE = 100;
const INCREMENT = 100;
const MAX_VISIBLE = 5000;

// React.memo：rows 仅在 session 切换时变化，但 App 每次输入 task 或
// 刷新历史都会重渲染。memo 让 DataTable 跳过这些场景，避免重新生成
// 几百个 <td>。
const DataTable = React.memo(function DataTable({ rows }: DataTableProps) {
  const [search, setSearch] = useState("");
  // 多列排序：数组按优先级从高到低。普通点击重置为单列，Shift+点击追加次级键。
  const [sortKeys, setSortKeys] = useState<SortKey[]>([]);
  const [visible, setVisible] = useState(INITIAL_VISIBLE);
  const [loadingMore, setLoadingMore] = useState(false);
  // 列宽（内存态，会话内保留）：Record<列名, 宽度px>。
  const [colWidths, setColWidths] = useState<Record<string, number>>({});
  const columns = useMemo(() => Object.keys(rows?.[0] || {}), [rows]);

  // 列类型推断：取第一个非空值判断 number / date / text。
  // date 检测附加 /[-/:]/ 前置过滤，避免 "2023" 这类纯数字字符串被
  // Date.parse 误判为合法日期（V8 会把 "2023" 当作年份解析）。
  const colTypes = useMemo<Record<string, ColType>>(() => {
    const types: Record<string, ColType> = {};
    for (const col of columns) {
      const sample = (rows || []).find((r) => r[col] != null)?.[col];
      if (typeof sample === "number") types[col] = "number";
      else if (typeof sample === "string" && /[-/:]/.test(sample) && !isNaN(Date.parse(sample))) types[col] = "date";
      else types[col] = "text";
    }
    return types;
  }, [rows, columns]);

  // 客户端搜索过滤：在 preview 范围内按任意列包含关键词匹配。
  const filtered = useMemo<Row[]>(() => {
    if (!rows?.length) return [];
    if (!search.trim()) return rows;
    const q = search.toLowerCase();
    return rows.filter((row) =>
      columns.some((col) => String(row[col] ?? "").toLowerCase().includes(q))
    );
  }, [rows, search, columns]);

  // 多列排序：按优先级顺序逐键比较，null 统一沉底。
  const sorted = useMemo<Row[]>(() => {
    if (sortKeys.length === 0) return filtered;
    return [...filtered].sort((a, b) => {
      for (const key of sortKeys) {
        const av = a[key.col], bv = b[key.col];
        let cmp: number;
        if (av == null && bv == null) cmp = 0;
        else if (av == null) cmp = 1;
        else if (bv == null) cmp = -1;
        else if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
        else cmp = String(av).localeCompare(String(bv));
        if (cmp !== 0) return key.dir === "asc" ? cmp : -cmp;
      }
      return 0;
    });
  }, [filtered, sortKeys]);

  // 搜索或数据切换时回到首屏 100 行，避免停留在已不存在的偏移上。
  useEffect(() => {
    setVisible(INITIAL_VISIBLE);
  }, [search, rows]);

  const total = sorted.length;
  const cappedTotal = Math.min(total, MAX_VISIBLE);
  const hasMore = visible < cappedTotal;
  const reachedCap = total > MAX_VISIBLE && visible >= MAX_VISIBLE;
  const visibleRows = useMemo(
    () => sorted.slice(0, Math.min(visible, cappedTotal)),
    [sorted, visible, cappedTotal]
  );

  const wrapRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLTableRowElement>(null);
  const loadingMoreRef = useRef(false);

  // 滚动接近底部时增量加载：sentinel 行进入视口即追加 INCREMENT 行。
  useEffect(() => {
    const wrap = wrapRef.current;
    const sentinel = sentinelRef.current;
    if (!wrap || !sentinel || !hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries[0].isIntersecting || loadingMoreRef.current) return;
        loadingMoreRef.current = true;
        setLoadingMore(true);
        // 同步切片很快，延迟一帧展示"加载中…"反馈。
        window.setTimeout(() => {
          setVisible((v) => Math.min(v + INCREMENT, cappedTotal));
          setLoadingMore(false);
          loadingMoreRef.current = false;
        }, 60);
      },
      { root: wrap, rootMargin: "0px 0px 240px 0px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, cappedTotal]);

  const handleHeaderClick = (col: string, shift: boolean) => {
    setSortKeys((prev) => {
      if (!shift) {
        // 普通点击：重置为单列排序；同列则切换方向。
        if (prev.length === 1 && prev[0].col === col) {
          return [{ col, dir: prev[0].dir === "asc" ? "desc" : "asc" }];
        }
        return [{ col, dir: "asc" }];
      }
      // Shift+点击：追加次级排序键；已存在则切换方向。
      const existing = prev.find((k) => k.col === col);
      if (existing) {
        return prev.map((k) => (k.col === col ? { ...k, dir: k.dir === "asc" ? "desc" : "asc" } : k));
      }
      return [...prev, { col, dir: "asc" }];
    });
  };

  // 列宽拖拽：pointerdown 记录起点，pointermove 直接改 th 宽度（避免整表重渲染），
  // pointerup 提交到 state。最小宽度 60px。
  const dragRef = useRef<{ col: string; startX: number; startWidth: number; th: HTMLTableCellElement } | null>(null);

  const onHandlePointerDown = (e: React.PointerEvent, col: string) => {
    e.stopPropagation();
    const th = (e.currentTarget as HTMLElement).parentElement as HTMLTableCellElement | null;
    if (!th) return;
    dragRef.current = { col, startX: e.clientX, startWidth: th.offsetWidth, th };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onHandlePointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const newW = Math.max(60, d.startWidth + (e.clientX - d.startX));
    d.th.style.width = `${newW}px`;
  };
  const onHandlePointerUp = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const newW = Math.max(60, d.startWidth + (e.clientX - d.startX));
    setColWidths((prev) => ({ ...prev, [d.col]: newW }));
    dragRef.current = null;
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* 忽略已释放的情况 */
    }
  };

  // 列类型 → 列头小标签（# 数值 / A 文本 / 📅 日期）
  const typeLabel = (t: ColType | undefined) => (t === "number" ? "#" : t === "date" ? "📅" : "A");

  if (!rows?.length) return <div className="empty-row">没有可预览的数据</div>;

  const hasCustomWidths = Object.keys(colWidths).length > 0;

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
          共 {total} 行
        </span>
      </div>
      <div className="table-wrap" ref={wrapRef}>
        <table className="data-table" style={hasCustomWidths ? { tableLayout: "fixed" } : undefined}>
          <thead>
            <tr>
              <th className="row-number">#</th>
              {columns.map((column) => {
                const sortIndex = sortKeys.findIndex((k) => k.col === column);
                const sortKey = sortIndex >= 0 ? sortKeys[sortIndex] : null;
                return (
                  <th
                    key={column}
                    style={colWidths[column] ? { width: colWidths[column] } : undefined}
                    className={sortKey ? `is-sorted is-${sortKey.dir}` : ""}
                    onClick={(e) => handleHeaderClick(column, e.shiftKey)}
                    title="点击切换升序 / 降序 · Shift+点击追加排序键"
                  >
                    {column}
                    <span className="data-table-type-badge">{typeLabel(colTypes[column])}</span>
                    {sortKey && (
                      <>
                        <span className="data-table-sort-badge">{sortIndex + 1}</span>
                        <span className="data-table-arrow">{sortKey.dir === "asc" ? "▲" : "▼"}</span>
                      </>
                    )}
                    <span
                      className="col-resize-handle"
                      role="separator"
                      aria-orientation="vertical"
                      aria-label={`调整 ${column} 列宽`}
                      onPointerDown={(e) => onHandlePointerDown(e, column)}
                      onPointerMove={onHandlePointerMove}
                      onPointerUp={onHandlePointerUp}
                      onClick={(e) => e.stopPropagation()}
                    />
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {total === 0 ? (
              <tr><td className="row-number">—</td><td colSpan={columns.length} style={{ textAlign: "center", color: "var(--fg-muted)" }}>没有匹配的行</td></tr>
            ) : (
              <>
                {visibleRows.map((row, index) => (
                  <tr key={index}>
                    <td className="row-number">{index + 1}</td>
                    {columns.map((column) => {
                      // 空值高亮：null / undefined / 空字符串 / NaN 统一显示为 "—"，
                      // 并加 cell-empty 类用斜体灰色弱化，便于扫读缺失值分布。
                      const cellValue = row[column];
                      const isEmpty = cellValue == null || cellValue === "" || (typeof cellValue === "number" && isNaN(cellValue));
                      return (
                        <td key={column} className={isEmpty ? "cell-empty" : ""}>
                          {isEmpty ? "—" : String(cellValue)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
                {loadingMore && (
                  <tr className="data-table-loading-row">
                    <td className="row-number">—</td>
                    <td colSpan={columns.length}>加载中…</td>
                  </tr>
                )}
                {/* sentinel 常驻（只要还有更多），保证 IntersectionObserver 始终观察同一节点，
                    避免加载窗口内卸载导致观察失效、增量加载停滞。 */}
                {hasMore && (
                  <tr ref={sentinelRef} className="data-table-sentinel" aria-hidden="true">
                    <td className="row-number" />
                    <td colSpan={columns.length} />
                  </tr>
                )}
              </>
            )}
          </tbody>
          <tfoot>
            <tr className="data-table-footer-row">
              <td className="row-number" />
              <td colSpan={columns.length} className="data-table-footer">
                {reachedCap
                  ? `已显示前 ${MAX_VISIBLE} 行，使用搜索或导出查看完整数据`
                  : `显示 ${Math.min(visible, cappedTotal)} / ${total} 行`}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
});

export default DataTable;
