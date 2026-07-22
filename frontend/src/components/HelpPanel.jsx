import React, { useEffect } from "react";
import { X } from "lucide-react";
import { HELP_SHORTCUTS } from "../constants";

// 快捷键帮助面板（? 唤起）：完整列出所有可用快捷键。
// 集中在 HELP_SHORTCUTS 常量维护，避免文档与实现脱节。
const HelpPanel = React.memo(function HelpPanel({ onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="help-panel-backdrop" role="dialog" aria-modal="true" aria-label="键盘快捷键" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="help-panel">
        <div className="help-panel-header">
          <h2>键盘快捷键</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X size={16} /></button>
        </div>
        <div className="help-panel-body">
          {HELP_SHORTCUTS.map((group) => (
            <div key={group.section} className="help-section">
              <h3 className="help-section-title">{group.section}</h3>
              <div className="help-shortcut-list">
                {group.items.map((item, idx) => (
                  <div key={idx} className="help-shortcut">
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
