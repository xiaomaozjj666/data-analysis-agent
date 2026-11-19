import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { CircleHelp, FilePlus2, Moon } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import CommandPalette from "../CommandPalette";
import type { CommandAction } from "../../types";

const ACTIONS: CommandAction[] = [
  { id: "new-analysis", icon: FilePlus2, title: "新建分析", subtitle: "上传新的数据集", section: "操作" },
  { id: "toggle-theme", icon: Moon, title: "切换主题", subtitle: "亮色 ↔ 暗色", section: "操作" },
  { id: "show-help", icon: CircleHelp, title: "查看键盘快捷键", subtitle: "全部快捷键列表", section: "帮助" },
];

// 触发器 + 面板的受控组合：关闭后触发按钮仍在 DOM 中，便于断言焦点归还。
function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开命令面板
      </button>
      <button type="button">面板后的按钮</button>
      {open && (
        <CommandPalette
          query=""
          onQueryChange={() => {}}
          actions={ACTIONS}
          sessions={[]}
          onAction={() => setOpen(false)}
          onSelectSession={() => setOpen(false)}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

describe("CommandPalette 无障碍", () => {
  it("焦点陷阱在面板内循环，Esc 关闭后焦点回到触发按钮", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "打开命令面板" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "命令面板" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby", "command-palette-title");

    const input = screen.getByRole("textbox", { name: "搜索命令或历史会话" });
    expect(input).toHaveFocus();

    // 面板内最后一个可聚焦元素：最后一条命令按钮。
    const focusables = dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    const last = focusables[focusables.length - 1];
    expect(last).not.toBe(input);
    expect(focusables[0]).toBe(input);

    // Shift+Tab 从第一项（输入框）回绕到最后一项。
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();

    // Tab 从最后一项回绕到输入框，焦点不会穿透到背景页面。
    fireEvent.keyDown(document, { key: "Tab" });
    expect(input).toHaveFocus();

    // Esc 在 document 级生效：即使焦点不留在输入框也能关闭。
    last.focus();
    expect(last).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "命令面板" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
