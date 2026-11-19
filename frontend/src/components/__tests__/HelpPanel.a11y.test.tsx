import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import HelpPanel from "../HelpPanel";

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        查看键盘快捷键
      </button>
      {open && <HelpPanel onClose={() => setOpen(false)} />}
    </>
  );
}

describe("HelpPanel 无障碍与快捷键登记", () => {
  it("打开时焦点进入，Shift+Tab 在面板内循环，Esc 关闭并归还焦点", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "查看键盘快捷键" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "键盘快捷键" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby", "help-panel-title");
    expect(dialog).toHaveFocus();

    const close = screen.getByRole("button", { name: "关闭" });
    // 初始焦点在对话框容器上，Shift+Tab 应回绕到面板内最后一个可聚焦元素。
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(close).toHaveFocus();
    // 面板只有一个可聚焦元素，Tab / Shift+Tab 都留在面板内。
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "键盘快捷键" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("登记 Ctrl/⌘+Enter 复算、F 全屏、T 主题切换，并说明输入态冲突", () => {
    render(<HelpPanel onClose={() => {}} />);

    const auditSection = screen.getByRole("heading", { name: "简报核验" }).parentElement as HTMLElement;
    expect(within(auditSection).getByText("复算并核验简报（草稿框内）")).toBeInTheDocument();
    expect(within(auditSection).getByText("全屏 / 退出全屏（路演视图）")).toBeInTheDocument();
    expect(within(auditSection).getByText("切换亮色 / 暗色主题")).toBeInTheDocument();
    expect(within(auditSection).getByText("Ctrl / ⌘")).toBeInTheDocument();
    expect(within(auditSection).getByText("Enter")).toBeInTheDocument();
    expect(within(auditSection).getByText("F")).toBeInTheDocument();
    expect(within(auditSection).getByText("T")).toBeInTheDocument();

    // 快捷键冲突说明：单字符快捷键在输入框内让位给文本输入。
    expect(screen.getByText(/单字符快捷键.*在输入框或草稿框内不生效/)).toBeInTheDocument();
  });
});
