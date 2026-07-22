import React, { useEffect, useMemo, useRef, useState } from "react";
import { FileSpreadsheet, Search } from "lucide-react";
import { describeHistoryStatus, formatRelativeTime } from "../utils/format";

// 命令面板（Cmd+K）：参考 Linear / Raycast / VSCode 的命令面板体验。
// 输入框 + 动作列表 + 会话搜索结果。键盘上下选择，Enter 执行，Esc 关闭。
const CommandPalette = React.memo(function CommandPalette({
  query, onQueryChange, actions, sessions, onAction, onSelectSession, onClose, theme,
}) {
  const inputRef = useRef(null);
  const [activeIndex, setActiveIndex] = useState(0);
  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => { setActiveIndex(0); }, [query]);

  const q = query.trim().toLowerCase();
  const filteredActions = !q ? actions : actions.filter((a) =>
    a.title.toLowerCase().includes(q) || a.subtitle.toLowerCase().includes(q));
  const filteredSessions = !q ? [] : (sessions || []).filter((s) =>
    (s.filename || "").toLowerCase().includes(q)).slice(0, 5);

  const flat = [
    ...filteredActions.map((a) => ({ type: "action", value: a })),
    ...filteredSessions.map((s) => ({ type: "session", value: s })),
  ];
  const total = flat.length;

  const handleKeyDown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActiveIndex((i) => (i + 1) % Math.max(total, 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIndex((i) => (i - 1 + Math.max(total, 1)) % Math.max(total, 1)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const item = flat[activeIndex];
      if (!item) return;
      if (item.type === "action") onAction(item.value);
      else onSelectSession(item.value);
    } else if (e.key === "Escape") { e.preventDefault(); onClose(); }
  };

  // 按 section 分组动作
  const actionGroups = useMemo(() => {
    const map = new Map();
    for (const a of filteredActions) {
      if (!map.has(a.section)) map.set(a.section, []);
      map.get(a.section).push(a);
    }
    return Array.from(map.entries());
  }, [filteredActions]);

  return (
    <div className="command-palette-backdrop" role="dialog" aria-modal="true" aria-label="命令面板" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="command-palette">
        <div className="command-input-row">
          <Search size={16} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="搜索操作或历史会话…"
          />
          <kbd className="command-item-kbd">ESC</kbd>
        </div>
        <div className="command-list">
          {total === 0 && <div className="command-empty">没有匹配项</div>}
          {actionGroups.map(([section, items]) => (
            <div key={section}>
              <div className="command-section-label">{section}</div>
              {items.map((a) => {
                const idx = flat.findIndex((f) => f.type === "action" && f.value.id === a.id);
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`command-item ${idx === activeIndex ? "is-active" : ""}`}
                    onMouseEnter={() => setActiveIndex(idx)}
                    onClick={() => onAction(a)}
                  >
                    <a.icon size={16} className="command-item-icon" />
                    <span className="command-item-text">
                      <strong>{a.title}</strong>
                      <small>{a.subtitle}</small>
                    </span>
                  </button>
                );
              })}
            </div>
          ))}
          {filteredSessions.length > 0 && (
            <div>
              <div className="command-section-label">历史会话</div>
              {filteredSessions.map((s) => {
                const idx = flat.findIndex((f) => f.type === "session" && f.value.id === s.id);
                return (
                  <button
                    key={s.id}
                    type="button"
                    className={`command-item ${idx === activeIndex ? "is-active" : ""}`}
                    onMouseEnter={() => setActiveIndex(idx)}
                    onClick={() => onSelectSession(s)}
                  >
                    <FileSpreadsheet size={16} className="command-item-icon" />
                    <span className="command-item-text">
                      <strong>{s.filename}</strong>
                      <small>{formatRelativeTime(s.created_at)} · {describeHistoryStatus(s.analysis_status).label}</small>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <div className="command-footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>Enter</kbd> 执行</span>
          <span><kbd>Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>
  );
});

export default CommandPalette;
