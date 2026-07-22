import React, { useMemo, useState } from "react";
import {
  ChevronRight,
  FileSpreadsheet,
  History,
  LoaderCircle,
  RefreshCw,
  Search,
} from "lucide-react";
import {
  describeHistoryStatus,
  formatRelativeTime,
  groupSessionsByTime,
} from "../utils/format";
import type { HistorySessionItem } from "../types";

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
}: HistoryPanelProps) {
  const [searchQuery, setSearchQuery] = useState("");
  // 搜索过滤：按文件名匹配，匹配不到时显示空状态。本地过滤即可，
  // 不需要后端 query 参数——历史列表通常 ≤ 30 条，前端 filter 毫秒级。
  const filtered = useMemo<HistorySessionItem[]>(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return sessions || [];
    return (sessions || []).filter((s) => {
      const filename = (s.filename || "").toLowerCase();
      const task = (s.current_task || s.task || "").toLowerCase();
      const response = ((s.last_result?.response as string | undefined) || "").toLowerCase();
      return filename.includes(q) || task.includes(q) || response.includes(q);
    });
  }, [sessions, searchQuery]);
  const groups = useMemo(() => groupSessionsByTime(filtered), [filtered]);
  const isEmpty = !sessions?.length && !loading;
  const isSearching = searchQuery.trim().length > 0;
  const noResults = isSearching && filtered.length === 0 && !loading;

  return (
    <div className="sidebar-section history-section">
      <button type="button" className="history-toggle" onClick={onToggle} aria-expanded={expanded}>
        <History size={14} />
        <span className="sidebar-label">历史会话</span>
        {sessions && sessions.length > 0 && <em className="history-total">{sessions.length}</em>}
        <ChevronRight size={13} className={expanded ? "rot-90" : ""} />
      </button>
      {expanded && (
        <>
          <button type="button" className="history-refresh" onClick={onRefresh} disabled={loading} title="刷新历史" aria-label="刷新历史会话列表">
            <RefreshCw size={12} className={loading ? "spin" : ""} />
            {loading ? "加载中" : "刷新"}
          </button>
          {sessions && sessions.length > 0 && (
            <div className="history-search">
              <div className="history-search-wrap">
                <Search size={12} />
                <input
                  type="search"
                  className="history-search-input"
                  placeholder="搜索文件名…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  aria-label="搜索历史会话"
                />
              </div>
            </div>
          )}
          {noResults ? (
            <div className="history-search-empty">没有匹配「{searchQuery.trim()}」的会话</div>
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
                  {group.items.map((item) => {
                    const active = item.id === currentSessionId;
                    const status = describeHistoryStatus(item.analysis_status);
                    return (
                      <li key={item.id} className={active ? "is-active" : ""}>
                        <button type="button" onClick={() => onSelect(item)} disabled={switchingSessionId != null}>
                          <FileSpreadsheet size={14} />
                          <span>
                            <strong>{item.filename}</strong>
                            <small>
                              <span className={`history-status-dot ${status.dot}`} aria-hidden="true" />
                              <span className="history-status-text">{status.label}</span>
                              {item.has_result && <span className="history-result">· 有报告</span>}
                              <span className="history-time">· {formatRelativeTime(item.created_at)}</span>
                            </small>
                          </span>
                          {switchingSessionId === item.id ? (
                            <LoaderCircle size={13} className="spin" />
                          ) : (item.artifact_count || 0) > 0 ? (
                            <em className="history-count">{item.artifact_count}</em>
                          ) : null}
                        </button>
                      </li>
                    );
                  })}
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
