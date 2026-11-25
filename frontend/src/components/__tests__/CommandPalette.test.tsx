import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { FileSpreadsheet, Moon, Search, Settings2, Table2 } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import CommandPalette from "../CommandPalette";
import type { CommandAction, HistorySessionItem } from "../../types";

// CommandPalette 交互行为测试（补 a11y 测试之外的搜索/键盘/最近使用路径）。
// 覆盖：分类分组、模糊过滤与高亮、空结果、会话搜索与 Enter 打开、方向键
// 循环与 Enter 执行、最近使用置顶且上限 3 条、鼠标悬停/点击、遮罩关闭。
// 组件为纯展示（状态由父级受控），这里用 Harness 提供真实 query 流。

interface HarnessProps {
  actions: CommandAction[];
  sessions?: HistorySessionItem[] | null;
  onAction?: (action: CommandAction) => void;
  onSelectSession?: (item: HistorySessionItem) => void;
  onClose?: () => void;
}

function Harness({
  actions,
  sessions = [],
  onAction = () => {},
  onSelectSession = () => {},
  onClose = () => {},
}: HarnessProps) {
  const [query, setQuery] = useState("");
  return (
    <CommandPalette
      query={query}
      onQueryChange={setQuery}
      actions={actions}
      sessions={sessions}
      onAction={onAction}
      onSelectSession={onSelectSession}
      onClose={onClose}
    />
  );
}

function action(id: string, title: string, subtitle: string): CommandAction {
  return { id, icon: Search, title, subtitle, section: "操作" };
}

const ACTIONS: CommandAction[] = [
  { id: "t-nav-data", icon: Table2, title: "数据预览", subtitle: "查看工作表与字段", section: "导航" },
  { id: "t-theme", icon: Moon, title: "切换主题", subtitle: "亮色 ↔ 暗色", section: "设置" },
  { id: "t-settings", icon: Settings2, title: "模型设置", subtitle: "密钥与推理强度", section: "设置" },
  { id: "t-new", icon: FileSpreadsheet, title: "新建分析", subtitle: "上传新的数据集", section: "操作" },
];

function sessionItem(id: string, filename: string, status = "completed"): HistorySessionItem {
  return { id, filename, analysis_status: status };
}

describe("CommandPalette 搜索与分组", () => {
  it("无查询时按分类展示动作，输入框自动聚焦", () => {
    render(<Harness actions={ACTIONS} />);

    const dialog = screen.getByRole("dialog", { name: "命令面板" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("textbox", { name: "搜索命令或历史会话" })).toHaveFocus();
    expect(screen.getByText("操作")).toBeInTheDocument();
    expect(screen.getByText("导航")).toBeInTheDocument();
    expect(screen.getByText("设置")).toBeInTheDocument();
    for (const item of ACTIONS) {
      expect(screen.getByRole("button", { name: new RegExp(item.title) })).toBeInTheDocument();
    }
    // 底部操作提示
    expect(screen.getByText("执行")).toBeInTheDocument();
    expect(screen.getByText("关闭")).toBeInTheDocument();
  });

  it("输入关键字后只保留模糊命中项，并高亮命中字符", () => {
    render(
      <Harness
        actions={[
          action("f-1", "Export Report", "导出报告为 HTML"),
          action("f-2", "Open Settings", "模型与密钥"),
        ]}
      />,
    );

    // 非连续子序列 expt 命中 Export Report
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "expt" } });
    expect(screen.getByRole("button", { name: /Export Report/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open Settings/ })).not.toBeInTheDocument();
    const marks = Array.from(document.querySelectorAll("mark")).map((el) => el.textContent);
    // 高亮保留原文大小写（查询本身大小写不敏感）
    expect(marks.join("").toLowerCase()).toBe("expt");

    // 无命中时显示空态
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "zzzz" } });
    expect(screen.getByText("没有匹配的命令")).toBeInTheDocument();
  });

  it("会话按文件名模糊搜索，最多展示 5 条并显示状态；Enter 打开选中会话", () => {
    const sessions = Array.from({ length: 6 }, (_, i) =>
      sessionItem(`s-${i}`, `sales_${i}.csv`, i === 0 ? "running" : "completed"),
    );
    const onSelectSession = vi.fn();
    render(
      <Harness actions={ACTIONS} sessions={sessions} onSelectSession={onSelectSession} />,
    );

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "sales" } });
    expect(screen.getByText("会话")).toBeInTheDocument();
    const sessionButtons = screen.getAllByRole("button", { name: /sales_\d\.csv/ });
    expect(sessionButtons).toHaveLength(5);
    // 状态文案来自 describeHistoryStatus
    expect(sessionButtons.some((b) => b.textContent?.includes("已完成"))).toBe(true);

    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSelectSession).toHaveBeenCalledTimes(1);
    expect(onSelectSession.mock.calls[0][0]).toMatchObject({ id: expect.stringMatching(/^s-/) });
  });
});

describe("CommandPalette 键盘与最近使用", () => {
  it("方向键循环选择，Enter 执行动作；执行过的命令进入「最近使用」且上限 3 条", () => {
    const onAction = vi.fn();
    const own: CommandAction[] = [
      action("r-1", "命令甲", "task one"),
      action("r-2", "命令乙", "task two"),
      action("r-3", "命令丙", "task three"),
      action("r-4", "命令丁", "task four"),
    ];
    render(<Harness actions={own} onAction={onAction} />);
    const input = screen.getByRole("textbox");

    // 用非空查询 hidden 掉「最近使用」，让 flat 列表只含本用例的 4 条
    fireEvent.change(input, { target: { value: "task" } });
    expect(screen.getAllByRole("button", { name: /命令/ })).toHaveLength(4);

    const current = () => document.querySelector(".command-item.is-active")?.textContent;
    expect(current()).toContain("命令甲");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(current()).toContain("命令乙");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(current()).toContain("命令甲");
    // 从第一项向上回绕到最后一项
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(current()).toContain("命令丁");
    // 从最后一项向下回绕到第一项
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(current()).toContain("命令甲");

    // Enter 执行当前选中项
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][0]).toMatchObject({ id: "r-1" });

    // 依次执行另外三条（每次用精确查询让目标唯一）
    for (const item of own.slice(1)) {
      fireEvent.change(input, { target: { value: item.subtitle } });
      fireEvent.keyDown(input, { key: "Enter" });
    }
    expect(onAction.mock.calls.map((call) => (call[0] as CommandAction).id)).toEqual([
      "r-1", "r-2", "r-3", "r-4",
    ]);

    // 清空查询：最近使用置顶，最新在前，最多 3 条（最早的 r-1 被挤出）
    fireEvent.change(input, { target: { value: "" } });
    const recentLabel = screen.getByText("最近使用");
    const recentSection = recentLabel.parentElement as HTMLElement;
    const recentButtons = within(recentSection).getAllByRole("button");
    expect(recentButtons).toHaveLength(3);
    expect(recentButtons[0].textContent).toContain("命令丁");
    expect(recentButtons.map((b) => b.textContent).join(" ")).not.toContain("命令甲");
    // 被挤出最近使用外的命令仍在分类区，不会从面板消失
    expect(screen.getByRole("button", { name: /命令甲/ })).toBeInTheDocument();
  });
});

describe("CommandPalette 鼠标操作", () => {
  it("悬停切换高亮，点击动作执行，点击遮罩关闭而面板内部不关闭", () => {
    const onAction = vi.fn();
    const onClose = vi.fn();
    const { container } = render(
      <Harness
        actions={[action("m-1", "鼠标甲", "first"), action("m-2", "鼠标乙", "second")]}
        onAction={onAction}
        onClose={onClose}
      />,
    );
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "鼠标" } });

    const second = screen.getByRole("button", { name: /鼠标乙/ });
    fireEvent.mouseEnter(second);
    expect(document.querySelector(".command-item.is-active")?.textContent).toContain("鼠标乙");

    fireEvent.click(second);
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ id: "m-2" }));

    // 面板内部点击（非遮罩）不关闭
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();

    // 点击遮罩关闭
    const backdrop = container.querySelector(".command-palette-backdrop") as HTMLElement;
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("没有任何动作与会话时显示空态，方向键/Enter 安全无操作", () => {
    const onAction = vi.fn();
    render(<Harness actions={[]} sessions={[]} onAction={onAction} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "任意" } });

    expect(screen.getByText("没有匹配的命令")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAction).not.toHaveBeenCalled();
  });
});
