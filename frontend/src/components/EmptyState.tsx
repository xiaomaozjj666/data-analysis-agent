import type { LucideIcon } from "lucide-react";
import React from "react";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  hint?: string;
  /** 主操作：有 action 时渲染一个按钮（如"重新扫描"）。 */
  actionLabel?: string;
  onAction?: () => void;
  actionDisabled?: boolean;
  /** compact：用于卡片内部的小号空态（如筛选后无结果）。 */
  compact?: boolean;
}

/**
 * 统一的空态/错误态出口。
 *
 * 此前每个页面各自手写"empty-row"文案，图表空数据时甚至静默 return null，
 * 用户分不清"没数据 / 加载失败 / 没权限"。统一走本组件：图标 + 一句话结论
 * + 可选的下一步动作，样式集中在 styles/empty-state.css。
 */
const EmptyState = React.memo(function EmptyState({
  icon: Icon,
  title,
  hint,
  actionLabel,
  onAction,
  actionDisabled,
  compact = false,
}: EmptyStateProps) {
  return (
    <div
      className={compact ? "empty-state empty-state-compact" : "empty-state"}
      role="status"
    >
      {Icon && <Icon size={compact ? 18 : 28} aria-hidden="true" />}
      <div className="empty-state-body">
        <strong>{title}</strong>
        {hint && <small>{hint}</small>}
      </div>
      {actionLabel && onAction && (
        <button type="button" className="empty-state-action" onClick={onAction} disabled={actionDisabled}>
          {actionLabel}
        </button>
      )}
    </div>
  );
});

export default EmptyState;
