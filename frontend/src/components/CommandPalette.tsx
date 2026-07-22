import React, { useEffect, useMemo, useRef, useState } from "react";
import { FileSpreadsheet, Search } from "lucide-react";
import { describeHistoryStatus, formatRelativeTime } from "../utils/format";
import type { CommandAction, HistorySessionItem } from "../types";

interface CommandPaletteProps {
  query: string;
  onQueryChange: (value: string) => void;
  actions: CommandAction[];
  sessions?: HistorySessionItem[] | null;
  onAction: (action: CommandAction) => void;
  onSelectSession: (item: HistorySessionItem) => void;
  onClose: () => void;
  theme?: "light" | "dark";
}

type FlatEntry =
  | { type: "action"; value: CommandAction }
  | { type: "session"; value: HistorySessionItem };

// 模糊匹配评分（纯函数）：子序列匹配 + 评分，未匹配返回 -Infinity。
// 评分规则：子串直接命中给高分；连续匹配加分、靠前匹配加分、字符串短加分。
function fuzzyScore(query: string, target: string): number {
  if (!query) return 0;
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  // 子串直接命中：起始越靠前分越高，完全匹配额外加分
  const subIdx = t.indexOf(q);
  if (subIdx >= 0) {
    let score = 1000 - subIdx;
    if (q.length === t.length) score += 100;
    return score;
  }
  // 子序列匹配：所有字符按顺序出现即算命中
  let qi = 0;
  let score = 0;
  let consecutive = 0;
  let prevMatch = -2;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      if (ti === prevMatch + 1) {
        consecutive += 1;
        score += 5 * consecutive;
      } else {
        consecutive = 0;
        score += 1;
      }
      score += Math.max(0, 8 - ti); // 靠前加分
      prevMatch = ti;
      qi += 1;
    }
  }
  if (qi < q.length) return -Infinity;
  score += Math.max(0, 30 - t.length); // 短串加分
  return score;
}

// 收集匹配字符索引（优先子串区间），用于 <mark> 高亮
function fuzzyIndices(query: string, target: string): number[] {
  if (!query) return [];
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  const subIdx = t.indexOf(q);
  if (subIdx >= 0) {
    return Array.from({ length: q.length }, (_, i) => subIdx + i);
  }
  const indices: number[] = [];
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      indices.push(ti);
      qi += 1;
    }
  }
  return indices;
}

// 按 action.id 映射到分类（无 category 字段，按 id 归类）
const CATEGORY_BY_ID: Record<string, string> = {
  "new-analysis": "操作",
  "tab-analysis": "导航",
  "tab-data": "导航",
  "tab-artifacts": "导航",
  "toggle-theme": "设置",
  "open-settings": "设置",
  "show-help": "设置",
};
const CATEGORY_ORDER = ["操作", "导航", "设置"];
function categoryOf(action: CommandAction): string {
  return CATEGORY_BY_ID[action.id] || action.section || "操作";
}

// 最近使用：useRef 在组件卸载后会丢失，用模块级数组跨面板开合保留。
const RECENT_MAX = 3;
const recentCommands: CommandAction[] = [];
function trackRecentCommand(action: CommandAction) {
  const without = recentCommands.filter((a) => a.id !== action.id);
  recentCommands.length = 0;
  recentCommands.push(action, ...without);
  if (recentCommands.length > RECENT_MAX) {
    recentCommands.length = RECENT_MAX;
  }
}

// 高亮匹配字符：连续命中合并为一个 <mark>，未命中部分原样输出
function Highlight({ text, indices }: { text: string; indices: number[] }) {
  if (!indices.length) return <>{text}</>;
  const set = new Set(indices);
  const parts: React.ReactNode[] = [];
  let buf = "";
  let inMark = false;
  let key = 0;
  for (let i = 0; i < text.length; i++) {
    const m = set.has(i);
    if (i === 0) {
      inMark = m;
      buf = text[i];
    } else if (m === inMark) {
      buf += text[i];
    } else {
      parts.push(inMark ? <mark key={key++}>{buf}</mark> : buf);
      inMark = m;
      buf = text[i];
    }
  }
  if (buf) parts.push(inMark ? <mark key={key++}>{buf}</mark> : buf);
  return <>{parts}</>;
}

// 命令面板（Cmd+K）：参考 Linear / Raycast / VSCode 的命令面板体验。
// 输入框 + 动作列表 + 会话搜索结果。键盘上下选择，Enter 执行，Esc 关闭。
const CommandPalette = React.memo(function CommandPalette({
  query, onQueryChange, actions, sessions, onAction, onSelectSession, onClose,
}: CommandPaletteProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => { setActiveIndex(0); }, [query]);

  const q = query.trim().toLowerCase();

  // 动作模糊匹配：对 title / subtitle 分别评分，取较高者，按分降序
  const scoredActions = useMemo(() => {
    if (!q) {
      return actions.map((a) => ({ action: a, score: 0, titleIndices: [] as number[], subIndices: [] as number[] }));
    }
    const scored: { action: CommandAction; score: number; titleIndices: number[]; subIndices: number[] }[] = [];
    for (const a of actions) {
      const titleScore = fuzzyScore(q, a.title);
      const subScore = fuzzyScore(q, a.subtitle);
      if (titleScore === -Infinity && subScore === -Infinity) continue;
      const useTitle = titleScore >= subScore;
      scored.push({
        action: a,
        score: Math.max(titleScore, subScore),
        titleIndices: useTitle ? fuzzyIndices(q, a.title) : [],
        subIndices: !useTitle ? fuzzyIndices(q, a.subtitle) : [],
      });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored;
  }, [actions, q]);

  // 会话模糊匹配：仅在有 query 时执行，取前 5 条
  const scoredSessions = useMemo(() => {
    if (!q) return [];
    const scored: { session: HistorySessionItem; score: number; indices: number[] }[] = [];
    for (const s of sessions || []) {
      const score = fuzzyScore(q, s.filename || "");
      if (score === -Infinity) continue;
      scored.push({ session: s, score, indices: fuzzyIndices(q, s.filename || "") });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 5);
  }, [sessions, q]);

  // 分区构建：最近使用（无 query 时）+ 分类动作 + 会话（有 query 时）
  const { sections, flat } = useMemo(() => {
    const recentItems = !q ? recentCommands.slice() : [];
    const recentIds = new Set(recentItems.map((a) => a.id));
    type ScoredAction = { action: CommandAction; score: number; titleIndices: number[]; subIndices: number[] };
    type ScoredSession = { session: HistorySessionItem; score: number; indices: number[] };
    type Section =
      | { kind: "actions"; label: string; items: ScoredAction[] }
      | { kind: "sessions"; label: string; items: ScoredSession[] };
    const secs: Section[] = [];
    // 按分类分组动作
    const byCategory = new Map<string, ScoredAction[]>();
    for (const item of scoredActions) {
      const cat = categoryOf(item.action);
      if (!byCategory.has(cat)) byCategory.set(cat, []);
      byCategory.get(cat)!.push(item);
    }
    if (recentItems.length > 0) {
      secs.push({
        kind: "actions",
        label: "最近使用",
        items: recentItems.map((a) => ({ action: a, score: 0, titleIndices: [], subIndices: [] })),
      });
    }
    for (const cat of CATEGORY_ORDER) {
      // 无 query 时把最近使用过的动作从分类中剔除，避免重复展示
      const items = (byCategory.get(cat) || []).filter((item) => q || !recentIds.has(item.action.id));
      if (items.length) secs.push({ kind: "actions", label: cat, items });
    }
    if (q && scoredSessions.length > 0) {
      secs.push({ kind: "sessions", label: "会话", items: scoredSessions });
    }
    const flatList: FlatEntry[] = [];
    for (const s of secs) {
      if (s.kind === "actions") {
        for (const item of s.items) flatList.push({ type: "action", value: item.action });
      } else {
        for (const item of s.items) flatList.push({ type: "session", value: item.session });
      }
    }
    return { sections: secs, flat: flatList };
  }, [q, scoredActions, scoredSessions]);

  const total = flat.length;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActiveIndex((i) => (i + 1) % Math.max(total, 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIndex((i) => (i - 1 + Math.max(total, 1)) % Math.max(total, 1)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const item = flat[activeIndex];
      if (!item) return;
      if (item.type === "action") {
        trackRecentCommand(item.value);
        onAction(item.value);
      } else {
        onSelectSession(item.value);
      }
    } else if (e.key === "Escape") { e.preventDefault(); onClose(); }
  };

  const handleActionClick = (action: CommandAction) => {
    trackRecentCommand(action);
    onAction(action);
  };

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
          {total === 0 && <div className="command-empty">没有匹配的命令</div>}
          {sections.map((section) => (
            <div key={section.label}>
              <div className="command-section-label">{section.label}</div>
              {section.kind === "actions" ? (
                section.items.map((item) => {
                  const idx = flat.findIndex((f) => f.type === "action" && f.value.id === item.action.id);
                  const Icon = item.action.icon;
                  return (
                    <button
                      key={item.action.id}
                      type="button"
                      className={`command-item ${idx === activeIndex ? "is-active" : ""}`}
                      onMouseEnter={() => setActiveIndex(idx)}
                      onClick={() => handleActionClick(item.action)}
                    >
                      <Icon size={16} className="command-item-icon" />
                      <span className="command-item-text">
                        <strong><Highlight text={item.action.title} indices={item.titleIndices} /></strong>
                        <small><Highlight text={item.action.subtitle} indices={item.subIndices} /></small>
                      </span>
                    </button>
                  );
                })
              ) : (
                section.items.map((item) => {
                  const idx = flat.findIndex((f) => f.type === "session" && f.value.id === item.session.id);
                  return (
                    <button
                      key={item.session.id}
                      type="button"
                      className={`command-item ${idx === activeIndex ? "is-active" : ""}`}
                      onMouseEnter={() => setActiveIndex(idx)}
                      onClick={() => onSelectSession(item.session)}
                    >
                      <FileSpreadsheet size={16} className="command-item-icon" />
                      <span className="command-item-text">
                        <strong><Highlight text={item.session.filename} indices={item.indices} /></strong>
                        <small>{formatRelativeTime(item.session.created_at)} · {describeHistoryStatus(item.session.analysis_status).label}</small>
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          ))}
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
