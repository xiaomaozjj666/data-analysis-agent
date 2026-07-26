import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight,
  Download,
  FileSpreadsheet,
  History,
  LoaderCircle,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import {
  describeHistoryStatus,
  formatRelativeTime,
  groupSessionsByTime,
} from "../utils/format";
import type { HistorySessionItem } from "../types";

// 状态过滤选项：全部 / 已完成 / 运行中 / 失败 / 已取消
type StatusFilter = "all" | "completed" | "running" | "failed" | "cancelled";
type SortOrder = "recent" | "earliest";
const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "completed", label: "已完成" },
  { value: "running", label: "运行中" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
];
// 状态值映射到过滤分类：running 与 cancelling 都归为"运行中"
function matchesStatus(filter: StatusFilter, status: string | undefined): boolean {
  switch (filter) {
    case "all": return true;
    case "completed": return status === "completed";
    case "running": return status === "running" || status === "cancelling";
    case "failed": return status === "failed";
    case "cancelled": return status === "cancelled";
  }
}

interface HistoryPanelProps {
  sessions?: HistorySessionItem[] | null;
  currentSessionId?: string | null;
  onSelect: (item: HistorySessionItem) => void;
  onRefresh: () => void;
  loading?: boolean;
  expanded?: boolean;
  onToggle: () => void;
  historyError?: boolean;
  switchingSessionId?: string | null;
  // Batch 4：会话导入/导出
  onExportSession?: (item: HistorySessionItem) => void;
  onImportSession?: (file: File) => void;
  onDeleteSession?: (item: HistorySessionItem) => void;
  onRenameSession?: (item: HistorySessionItem, title: string) => void;
}

// 历史会话面板：可折叠的侧边栏组件，按时间分组列出最近会话并允许切换。
// 关键设计：
//   1. 时间分组（今天/昨天/本周/更早）—— Linear / Notion / VSCode 都这么做，
//      人类记不住"5 小时前那次分析"，但能记住"今天上午那次"。
//   2. 骨架屏加载（而非"加载中"文字）—— 让用户立即看到列表骨架，
//      避免"什么都没有"的瞬间错愕。
//   3. 状态圆点 + 中文标签 —— running 圆点带脉冲动画，completed 是绿色，
//      failed 是红色，cancelled 是灰色，状态一眼可读。
//   4. 当前会话用左侧竖条 + 浅蓝底高亮，比单纯背景色更醒目。
const HistoryPanel = React.memo(function HistoryPanel({
  sessions, currentSessionId, onSelect, onRefresh, loading, expanded, onToggle, historyError, switchingSessionId,
  onExportSession, onImportSession, onDeleteSession, onRenameSession,
}: HistoryPanelProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sortOrder, setSortOrder] = useState<SortOrder>("recent");
  // 隐藏的文件 input：触发浏览器原生文件选择对话框，选中后回调 onImportSession
  const importInputRef = useRef<HTMLInputElement>(null);
  // 搜索 + 状态过滤 + 排序：文本匹配与状态筛选 AND 组合，先过滤再分组。
  // 本地过滤即可——历史列表通常 ≤ 30 条，前端 filter 毫秒级。
  const filtered = useMemo<HistorySessionItem[]>(() => {
    const q = searchQuery.trim().toLowerCase();
    const list = sessions || [];
    const matched = q
      ? list.filter((s) => {
          const filename = (s.filename || "").toLowerCase();
          const task = (s.current_task || s.task || "").toLowerCase();
          const response = ((s.last_result?.response as string | undefined) || "").toLowerCase();
          return filename.includes(q) || task.includes(q) || response.includes(q);
        })
      : list;
    const statusFiltered = statusFilter === "all"
      ? matched
      : matched.filter((s) => matchesStatus(statusFilter, s.analysis_status));
    // 排序：默认最近在前（created_at desc），可切换最早在前
    return [...statusFiltered].sort((a, b) => {
      const ta = a.created_at || 0;
      const tb = b.created_at || 0;
      return sortOrder === "recent" ? tb - ta : ta - tb;
    });
  }, [sessions, searchQuery, statusFilter, sortOrder]);
  const groups = useMemo(() => groupSessionsByTime(filtered), [filtered]);
  const isEmpty = !sessions?.length && !loading;
  const isFiltering = searchQuery.trim().length > 0 || statusFilter !== "all";
  const noResults = isFiltering && filtered.length === 0 && !loading;

  return (
    <div className="sidebar-section history-section">
      <button type="button" className="history-toggle" onClick={onToggle} aria-expanded={expanded}>
        <History size={14} />
        <span className="sidebar-label">历史会话</span>
        {sessions && sessions.length > 0 && <em className="history-total">{sessions.length}</em>}
        <ChevronRight size={13} className={expanded ? "rot-90" : ""} />
      </button>
      {/* 会话搜索框：始终可见（不随 expanded 切换挂载/卸载），
          让用户在折叠态也能直接输入关键词，展开后即看到过滤结果。
          placeholder 改为"搜索会话…"，因为匹配范围已涵盖任务文本与报告内容，
          不再局限于文件名。 */}
      <div className="history-search">
        <div className="history-search-wrap">
          <Search size={12} />
          <input
            type="search"
            className="history-search-input"
            placeholder="搜索会话…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            aria-label="搜索历史会话"
          />
        </div>
      </div>
      {expanded && (
        <>
          {/* 高级筛选行：状态 chip（单选）+ 排序 chip，仅展开态可见 */}
          <div className="history-filter-row">
            <div className="history-filter-chips" role="group" aria-label="按状态过滤">
              {STATUS_FILTERS.map((f) => (
                <button
                  key={f.value}
                  type="button"
                  className={`history-chip ${statusFilter === f.value ? "is-active" : ""}`}
                  onClick={() => setStatusFilter(f.value)}
                  aria-pressed={statusFilter === f.value}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <div className="history-sort-chips" role="group" aria-label="排序方式">
              <button
                type="button"
                className={`history-chip ${sortOrder === "recent" ? "is-active" : ""}`}
                onClick={() => setSortOrder("recent")}
                aria-pressed={sortOrder === "recent"}
              >
                最近
              </button>
              <button
                type="button"
                className={`history-chip ${sortOrder === "earliest" ? "is-active" : ""}`}
                onClick={() => setSortOrder("earliest")}
                aria-pressed={sortOrder === "earliest"}
              >
                最早
              </button>
            </div>
          </div>
          <button type="button" className="history-refresh" onClick={onRefresh} disabled={loading} title="刷新历史" aria-label="刷新历史会话列表">
            <RefreshCw size={12} className={loading ? "spin" : ""} />
            {loading ? "加载中" : "刷新"}
          </button>
          {/* 导入会话：选择 .zip 文件交给后端解析并恢复会话 */}
          <button type="button" className="history-import" onClick={() => importInputRef.current?.click()} title="导入会话" aria-label="从 ZIP 文件导入会话">
            <Upload size={12} />
            导入
          </button>
          <input
            ref={importInputRef}
            type="file"
            accept=".zip"
            style={{ display: "none" }}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file && onImportSession) onImportSession(file);
              // 重置 value 允许重复选择同一文件
              e.target.value = "";
            }}
          />
          {noResults ? (
            <div className="history-search-empty">
              {searchQuery.trim() ? `没有匹配「${searchQuery.trim()}」的会话` : "没有符合条件的会话"}
            </div>
          ) : isEmpty ? (
            <div className="history-empty">
              <History size={16} />
              {historyError ? (
                <>
                  <p>历史会话加载失败，请检查网络后重试。</p>
                  <button type="button" className="history-retry" onClick={onRefresh}>重新加载</button>
                </>
              ) : (
                <p>还没有历史会话，上传数据后会自动出现在这里。</p>
              )}
            </div>
          ) : loading && !sessions?.length ? (
            <ul className="history-list history-skeleton" aria-hidden="true">
              {[0, 1, 2].map((index) => (
                <li key={index}>
                  <div className="skeleton-row">
                    <span className="skeleton-icon" />
                    <span className="skeleton-lines">
                      <span className="skeleton-line skeleton-line-wide" />
                      <span className="skeleton-line skeleton-line-narrow" />
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            groups.map((group) => (
              <div key={group.label} className="history-group">
                <span className="history-group-label">{group.label}</span>
                <ul className="history-list">
                  {group.items.map((item) => (
                    <HistoryItem
                      key={item.id}
                      item={item}
                      active={item.id === currentSessionId}
                      switching={switchingSessionId === item.id}
                      switchingAny={switchingSessionId != null}
                      onSelect={onSelect}
                      onExport={onExportSession}
                      onDelete={onDeleteSession}
                      onRename={onRenameSession}
                    />
                  ))}
                </ul>
              </div>
            ))
          )}
        </>
      )}
    </div>
  );
});

export default HistoryPanel;

// 单个历史会话列表项：拆分为独立组件以管理内联删除确认态。
// 二次确认采用内联展开而非全局 modal：点击删除图标后该行变为
// "确认删除？[删除][取消]"，4 秒无操作自动收起，避免误删且不打断流程。
interface HistoryItemProps {
  item: HistorySessionItem;
  active: boolean;
  switching: boolean;
  switchingAny: boolean;
  onSelect: (item: HistorySessionItem) => void;
  onExport?: (item: HistorySessionItem) => void;
  onDelete?: (item: HistorySessionItem) => void;
  onRename?: (item: HistorySessionItem, title: string) => void;
}

const HistoryItem = React.memo(function HistoryItem({
  item, active, switching, switchingAny, onSelect, onExport, onDelete, onRename,
}: HistoryItemProps) {
  const status = describeHistoryStatus(item.analysis_status);
  // 显示名优先用自定义标题，没有则回退 filename
  const displayName = item.title || item.filename;
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(displayName);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);

  // 4 秒后自动收起确认态，避免用户点开删除后走开回来仍停留在高危态
  useEffect(() => {
    if (!confirming) return;
    confirmTimer.current = setTimeout(() => setConfirming(false), 4000);
    return () => {
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
    };
  }, [confirming]);

  // 进入编辑态时聚焦输入框并选中全部文字，方便直接覆盖
  useEffect(() => {
    if (editing) {
      setDraft(displayName);
      requestAnimationFrame(() => {
        editInputRef.current?.focus();
        editInputRef.current?.select();
      });
    }
  }, [editing, displayName]);

  const commitRename = () => {
    const trimmed = draft.trim();
    if (onRename && trimmed && trimmed !== displayName) {
      onRename(item, trimmed);
    }
    setEditing(false);
  };

  if (confirming) {
    return (
      <li className="is-confirming">
        <div className="history-confirm-bar">
          <span>删除此会话？</span>
          <button
            type="button"
            className="history-confirm-delete"
            onClick={() => { if (onDelete) onDelete(item); }}
          >
            删除
          </button>
          <button
            type="button"
            className="history-confirm-cancel"
            onClick={() => setConfirming(false)}
          >
            取消
          </button>
        </div>
      </li>
    );
  }

  if (editing) {
    return (
      <li className="is-editing">
        <div className="history-edit-bar">
          <input
            ref={editInputRef}
            className="history-edit-input"
            value={draft}
            maxLength={80}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); commitRename(); }
              if (e.key === "Escape") { e.preventDefault(); setEditing(false); }
            }}
          />
          <button type="button" className="history-edit-save" onClick={commitRename}>保存</button>
          <button type="button" className="history-edit-cancel" onClick={() => setEditing(false)}>取消</button>
        </div>
      </li>
    );
  }

  return (
    // 乐观 UI（#2）：switching 为 true 时（点击后立即置位，早于接口返回）先加
    // is-pending 高亮，用户点击即见反馈；接口返回后成为正式 is-active。
    <li className={active ? "is-active" : switching ? "is-pending" : ""}>
      <button type="button" onClick={() => onSelect(item)} disabled={switchingAny}>
        <FileSpreadsheet size={14} />
        <span>
          <strong>{displayName}</strong>
          <small>
            <span className={`history-status-dot ${status.dot}`} aria-hidden="true" />
            <span className="history-status-text">{status.label}</span>
            {item.has_result && <span className="history-result">· 有报告</span>}
            <span className="history-time">· {formatRelativeTime(item.created_at)}</span>
          </small>
        </span>
        {switching ? (
          <LoaderCircle size={13} className="spin" />
        ) : (item.artifact_count || 0) > 0 ? (
          <em className="history-count">{item.artifact_count}</em>
        ) : null}
      </button>
      {/* 操作按钮组：重命名 + 导出 + 删除并排，hover 列表项时淡入，不遮挡会话文字 */}
      <div className="history-actions">
        {onRename && (
          <button
            type="button"
            className="history-action-btn history-rename"
            onClick={(e) => { e.stopPropagation(); setEditing(true); }}
            title="重命名会话"
            aria-label={`重命名会话 ${displayName}`}
          >
            <Pencil size={12} />
          </button>
        )}
        {onExport && (
          <button
            type="button"
            className="history-action-btn history-export"
            onClick={(e) => { e.stopPropagation(); onExport(item); }}
            title="导出会话"
            aria-label={`导出会话 ${displayName}`}
          >
            <Download size={12} />
          </button>
        )}
        {onDelete && (
          <button
            type="button"
            className="history-action-btn history-delete"
            onClick={(e) => { e.stopPropagation(); setConfirming(true); }}
            title="删除会话"
            aria-label={`删除会话 ${displayName}`}
          >
            <Trash2 size={12} />
          </button>
        )}
      </div>
    </li>
  );
});
