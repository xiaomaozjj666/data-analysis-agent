import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";

type Row = Record<string, unknown>;
type ColType = "number" | "date" | "text";
type SortDir = "asc" | "desc";
interface SortKey { col: string; dir: SortDir; }

interface DataTableProps {
  rows?: Row[] | null;
}

// 虚拟滚动参数：
// - ROW_HEIGHT: 每行固定高度（px），用于计算可视区域可容纳的行数和撑起总高度
// - OVERSCAN: 可视区域上下额外渲染的缓冲行数，避免快速滚动时出现白屏
// - HEADER_HEIGHT: 表头高度，滚动容器顶部偏移基准
const ROW_HEIGHT = 36;
const OVERSCAN = 8;
const HEADER_HEIGHT = 40;

// React.memo：rows 仅在 session 切换时变化，但 App 每次输入 task 或
// 刷新历史都会重渲染。memo 让 DataTable 跳过这些场景，避免重新生成 DOM。
const DataTable = React.memo(function DataTable({ rows }: DataTableProps) {
  const [search, setSearch] = useState("");
  // 多列排序：数组按优先级从高到低。普通点击重置为单列，Shift+点击追加次级键。
  const [sortKeys, setSortKeys] = useState<SortKey[]>([]);
  // 虚拟滚动状态：scrollTop 驱动渲染窗口计算
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(600);
  // 列宽（内存态，会话内保留）：Record<列名, 宽度px>。
  const [colWidths, setColWidths] = useState<Record<string, number>>({});
  const columns = useMemo(() => Object.keys(rows?.[0] || {}), [rows]);

  // 列类型推断：取第一个非空值判断 number / date / text。
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

  // 客户端搜索过滤：在全量数据范围内按任意列包含关键词匹配。
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

  // 搜索或数据切换时回到顶部，避免停留在已不存在的偏移上。
  useEffect(() => {
    setScrollTop(0);
  }, [search, rows]);

  const total = sorted.length;

  // 虚拟滚动核心：根据 scrollTop 和 viewportHeight 计算当前应渲染的行范围。
  // 只渲染 [startIndex, endIndex) 区间内的行 + OVERSCAN 缓冲，
  // 无论数据有多少行，DOM 中始终只有几十个 <tr>，保证流畅滚动。
  const { startIndex, endIndex, totalHeight, offsetY } = useMemo(() => {
    const startIndex = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
    const visibleCount = Math.ceil(viewportHeight / ROW_HEIGHT) + OVERSCAN * 2;
    const endIndex = Math.min(total, startIndex + visibleCount);
    return {
      startIndex,
      endIndex,
      totalHeight: total * ROW_HEIGHT,
      offsetY: startIndex * ROW_HEIGHT,
    };
  }, [scrollTop, viewportHeight, total]);

  const visibleRows = useMemo(
    () => sorted.slice(startIndex, endIndex),
    [sorted, startIndex, endIndex]
  );

  const wrapRef = useRef<HTMLDivElement>(null);

  // 监听滚动容器尺寸变化，更新 viewportHeight 用于虚拟窗口计算。
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const updateHeight = () => setViewportHeight(wrap.clientHeight - HEADER_HEIGHT);
    updateHeight();
    const observer = new ResizeObserver(updateHeight);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  // 滚动事件：用 rAF 节流避免频繁 setState 导致卡顿。
  const rafRef = useRef<number>(0);
  const onScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      setScrollTop(e.currentTarget.scrollTop);
    });
  }, []);

  useEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); }, []);

  const handleHeaderClick = (col: string, shift: boolean) => {
    setSortKeys((prev) => {
      if (!shift) {
        if (prev.length === 1 && prev[0].col === col) {
          return [{ col, dir: prev[0].dir === "asc" ? "desc" : "asc" }];
        }
        return [{ col, dir: "asc" }];
      }
      const existing = prev.find((k) => k.col === col);
      if (existing) {
        return prev.map((k) => (k.col === col ? { ...k, dir: k.dir === "asc" ? "desc" : "asc" } : k));
      }
      return [...prev, { col, dir: "asc" }];
    });
  };

  // 列宽拖拽：pointerdown 记录起点，pointermove 直接改 th 宽度，
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
          共 {total.toLocaleString()} 行
        </span>
      </div>
      <div className="table-wrap" ref={wrapRef} onScroll={onScroll}>
        {/* 虚拟滚动布局：外层 div 撑起总高度（total * ROW_HEIGHT），
            内层 tbody 用 translateY 偏移到当前渲染窗口的起始位置。
            table 本身不滚动，只有外层 wrap 滚动，确保表头 sticky 正常工作。 */}
        <div style={{ height: totalHeight, position: "relative" }}>
          <table
            className="data-table"
            style={{
              tableLayout: hasCustomWidths ? "fixed" : "auto",
              position: "absolute",
              top: 0,
              left: 0,
              width: "100%",
            }}
          >
            <thead>
              <tr>
                <th className="row-number" style={{ position: "sticky", top: 0, zIndex: 2 }}>#</th>
                {columns.map((column) => {
                  const sortIndex = sortKeys.findIndex((k) => k.col === column);
                  const sortKey = sortIndex >= 0 ? sortKeys[sortIndex] : null;
                  return (
                    <th
                      key={column}
                      style={{
                        width: colWidths[column] || undefined,
                        position: "sticky",
                        top: 0,
                        zIndex: 2,
                      }}
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
                  {/* 顶部占位：用空行撑起未渲染区域的高度 */}
                  {offsetY > 0 && (
                    <tr style={{ height: offsetY }} aria-hidden="true">
                      <td colSpan={columns.length + 1} style={{ padding: 0, border: "none" }} />
                    </tr>
                  )}
                  {visibleRows.map((row, i) => {
                    const rowIndex = startIndex + i;
                    return (
                      <tr key={rowIndex} style={{ height: ROW_HEIGHT }}>
                        <td className="row-number">{rowIndex + 1}</td>
                        {columns.map((column) => {
                          const cellValue = row[column];
                          const isEmpty = cellValue == null || cellValue === "" || (typeof cellValue === "number" && isNaN(cellValue));
                          return (
                            <td key={column} className={isEmpty ? "cell-empty" : ""}>
                              {isEmpty ? "—" : String(cellValue)}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </>
              )}
            </tbody>
            <tfoot>
              <tr className="data-table-footer-row">
                <td className="row-number" />
                <td colSpan={columns.length} className="data-table-footer">
                  {`显示 ${startIndex + 1}-${Math.min(endIndex, total)} / ${total.toLocaleString()} 行`}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
  );
});

export default DataTable;
