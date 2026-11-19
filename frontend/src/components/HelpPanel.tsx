import React, { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { HELP_SHORTCUTS } from "../constants";
import type { ShortcutGroup } from "../types";

interface HelpPanelProps {
  onClose: () => void;
}

// 简报核验主流程的上下文快捷键。全局快捷键表（HELP_SHORTCUTS）只描述
// "通用/分析"两组，这里把路演页真实可用的 Ctrl/⌘+Enter、F、T 明确登记，
// 避免帮助文档与实际实现脱节。
const AUDIT_SHORTCUTS: ShortcutGroup[] = [
  {
    section: "简报核验",
    items: [
      { keys: ["Ctrl / ⌘", "Enter"], desc: "复算并核验简报（草稿框内）" },
      { keys: ["F"], desc: "全屏 / 退出全屏（路演视图）" },
      { keys: ["T"], desc: "切换亮色 / 暗色主题" },
    ],
  },
];

const SHORTCUT_GROUPS = [...HELP_SHORTCUTS, ...AUDIT_SHORTCUTS];

// 快捷键帮助面板（? 唤起）：完整列出所有可用快捷键。
// 弹层语义与焦点行为对齐 WAI-ARIA Dialog 模式：
//   - role="dialog" + aria-modal + aria-labelledby
//   - 打开时焦点进入面板，Tab / Shift+Tab 在面板内循环
//   - Esc 关闭，卸载后焦点回到触发按钮
const HelpPanel = React.memo(function HelpPanel({ onClose }: HelpPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  // onClose 在 App 中以内联函数传入，身份每次渲染都会变；用 ref 保存最新值，
  // 避免 effect 重跑导致焦点被重新捕获 / 面板再次抢焦点。
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    restoreFocusRef.current = document.activeElement as HTMLElement | null;
    // 聚焦对话框容器本身（tabIndex=-1），让读屏立即播报标题；
    // 首次按 Tab 再进入关闭按钮。
    panelRef.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const focusables = panelRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusables.length) {
        e.preventDefault();
        panelRef.current.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      // 焦点落在面板容器（初始状态）或已跑到面板外时，拉回面板内。
      if (!panelRef.current.contains(active) || active === panelRef.current) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restoreFocusRef.current?.focus?.();
    };
  }, []);

  return (
    <div
      className="help-panel-backdrop"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="help-panel"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-panel-title"
        aria-describedby="help-panel-note"
        tabIndex={-1}
      >
        <div className="help-panel-header">
          <h2 id="help-panel-title">键盘快捷键</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <div className="help-panel-body">
          <p id="help-panel-note" className="help-panel-note">
            单字符快捷键（T / F / 1–5）在输入框或草稿框内不生效，避免输入时误触；
            Ctrl / ⌘ 组合键不受输入态限制。
          </p>
          {SHORTCUT_GROUPS.map((group) => (
            <div key={group.section} className="help-section">
              <h3 className="help-section-title">{group.section}</h3>
              <div className="help-shortcut-list">
                {group.items.map((item, idx) => (
                  <div key={`${group.section}-${idx}`} className="help-shortcut">
                    <span>{item.desc}</span>
                    <span className="help-shortcut-keys">
                      {item.keys.map((k, i) => <kbd key={i}>{k}</kbd>)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
});

export default HelpPanel;
