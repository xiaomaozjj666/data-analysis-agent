import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import HistoryPanel from "../HistoryPanel";
import type { HistorySessionItem } from "../../types";

const NOW = Math.floor(Date.now() / 1000);

const SESSIONS: HistorySessionItem[] = [
  {
    id: "s1",
    filename: "today.csv",
    analysis_status: "running",
    created_at: NOW - 5,
    has_result: true,
    artifact_count: 2,
  },
  {
    id: "s2",
    filename: "old.csv",
    analysis_status: "failed",
    current_task: "检查销售趋势",
    created_at: NOW - 7200,
  },
  {
    id: "s3",
    filename: "yesterday.csv",
    analysis_status: "completed",
    created_at: NOW - 90000,
    last_result: { response: "华东区报告关键词" },
  },
  {
    id: "s4",
    filename: "earlier.csv",
    analysis_status: "cancelled",
    created_at: NOW - 10 * 86400,
    title: "自定义标题",
  },
];

function renderPanel(overrides: Record<string, unknown> = {}) {
  const props = {
    sessions: SESSIONS,
    onSelect: vi.fn(),
    onRefresh: vi.fn(),
    onToggle: vi.fn(),
    expanded: true,
    ...overrides,
  };
  const utils = render(<HistoryPanel {...props} />);
  return { ...utils, props };
}

function itemFor(name: string): HTMLElement {
  const text = screen.getByText(name);
  const item = text.closest("li");
  if (!item) throw new Error(`未找到列表项：${name}`);
  return item as HTMLElement;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("HistoryPanel", () => {
  it("折叠时只保留搜索框，点击标题回调 onToggle", () => {
    const { props, container } = renderPanel({ expanded: false });
    fireEvent.click(container.querySelector(".history-toggle") as HTMLElement);
    expect(props.onToggle).toHaveBeenCalled();
    expect(screen.getByLabelText("搜索历史会话")).toBeInTheDocument();
    expect(screen.queryByText("today.csv")).toBeNull();
  });

  it("展开后按时间分组展示会话与状态", () => {
    renderPanel();
    expect(screen.getByText("今天")).toBeInTheDocument();
    expect(screen.getByText("昨天")).toBeInTheDocument();
    expect(screen.getByText("更早")).toBeInTheDocument();
    expect(screen.getByText("today.csv")).toBeInTheDocument();
    expect(screen.getByText("自定义标题")).toBeInTheDocument();
    expect(within(itemFor("today.csv")).getByText("运行中")).toBeInTheDocument();
    expect(within(itemFor("today.csv")).getByText("· 有报告")).toBeInTheDocument();
    expect(within(itemFor("today.csv")).getByText("2")).toBeInTheDocument();
  });

  it("搜索覆盖文件名 / 任务 / 报告正文，并给出无结果提示", () => {
    renderPanel();
    const search = screen.getByLabelText("搜索历史会话");
    fireEvent.change(search, { target: { value: "检查销售" } });
    expect(screen.getByText("old.csv")).toBeInTheDocument();
    expect(screen.queryByText("today.csv")).toBeNull();

    fireEvent.change(search, { target: { value: "报告关键词" } });
    expect(screen.getByText("yesterday.csv")).toBeInTheDocument();
    expect(screen.queryByText("old.csv")).toBeNull();

    fireEvent.change(search, { target: { value: "不存在的会话" } });
    expect(screen.getByText(/没有匹配「不存在的会话」的会话/)).toBeInTheDocument();

    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getByText("today.csv")).toBeInTheDocument();
  });

  it("状态筛选与排序切换", () => {
    const { container } = renderPanel();
    const filters = () => within(container.querySelector("[aria-label='按状态过滤']") as HTMLElement);
    fireEvent.click(filters().getByRole("button", { name: "失败" }));
    expect(screen.getByText("old.csv")).toBeInTheDocument();
    expect(screen.queryByText("today.csv")).toBeNull();

    fireEvent.click(filters().getByRole("button", { name: "已取消" }));
    expect(screen.getByText("自定义标题")).toBeInTheDocument();
    expect(screen.queryByText("old.csv")).toBeNull();

    fireEvent.click(filters().getByRole("button", { name: "全部" }));
    const today = within(container.querySelectorAll(".history-group")[0] as HTMLElement);
    expect(today.getAllByRole("button")[0]).toHaveTextContent("today.csv");
    fireEvent.click(screen.getByRole("button", { name: "最早" }));
    const sorted = within(container.querySelectorAll(".history-group")[0] as HTMLElement);
    expect(sorted.getAllByRole("button")[0]).toHaveTextContent("old.csv");
    fireEvent.click(screen.getByRole("button", { name: "最近" }));
    expect(
      within(container.querySelectorAll(".history-group")[0] as HTMLElement).getAllByRole("button")[0],
    ).toHaveTextContent("today.csv");
  });

  it("加载中展示骨架，刷新按钮禁用", () => {
    const { container } = renderPanel({ sessions: [], loading: true });
    expect(container.querySelector(".history-skeleton")).toBeTruthy();
    expect(screen.getByRole("button", { name: "刷新历史会话列表" })).toBeDisabled();
  });

  it("空列表与加载失败分别提示，失败可重新加载", () => {
    const { props, rerender } = renderPanel({ sessions: [] });
    expect(screen.getByText(/还没有历史会话/)).toBeInTheDocument();
    rerender(
      <HistoryPanel
        sessions={[]}
        onSelect={props.onSelect as never}
        onRefresh={props.onRefresh as never}
        onToggle={props.onToggle as never}
        historyError
        expanded
      />,
    );
    expect(screen.getByText(/历史会话加载失败/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(props.onRefresh).toHaveBeenCalled();
  });

  it("当前会话高亮，切换中禁用全部条目", () => {
    const { rerender, props } = renderPanel({ currentSessionId: "s1" });
    expect(itemFor("today.csv").className).toContain("is-active");

    rerender(
      <HistoryPanel
        sessions={SESSIONS}
        currentSessionId="s1"
        onSelect={props.onSelect as never}
        onRefresh={props.onRefresh as never}
        onToggle={props.onToggle as never}
        switchingSessionId="s3"
        expanded
      />,
    );
    expect(itemFor("yesterday.csv").className).toContain("is-pending");
    expect(within(itemFor("today.csv")).getByRole("button", { name: /today.csv/ })).toBeDisabled();
  });

  it("点击会话触发选择", () => {
    const { props } = renderPanel();
    fireEvent.click(within(itemFor("today.csv")).getByRole("button", { name: /today.csv/ }));
    expect(props.onSelect).toHaveBeenCalledWith(SESSIONS[0]);
  });

  it("导出按钮在导出中禁用并显示加载", () => {
    const onExportSession = vi.fn();
    const { rerender, props } = renderPanel({ onExportSession });
    fireEvent.click(screen.getByRole("button", { name: "导出会话 today.csv" }));
    expect(onExportSession).toHaveBeenCalledWith(SESSIONS[0]);

    rerender(
      <HistoryPanel
        sessions={SESSIONS}
        onSelect={props.onSelect as never}
        onRefresh={props.onRefresh as never}
        onToggle={props.onToggle as never}
        onExportSession={onExportSession}
        exportingSessionId="s1"
        expanded
      />,
    );
    const exporting = screen.getByRole("button", { name: "正在导出会话 today.csv" });
    expect(exporting).toBeDisabled();
    expect(exporting.querySelector(".spin")).toBeTruthy();
  });

  it("重命名：Enter 保存、Escape 取消、空值不提交", () => {
    const onRenameSession = vi.fn();
    renderPanel({ onRenameSession });

    fireEvent.click(screen.getByRole("button", { name: "重命名会话 today.csv" }));
    let input = screen.getByDisplayValue("today.csv");
    fireEvent.change(input, { target: { value: " 新名字 " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRenameSession).toHaveBeenCalledWith(SESSIONS[0], "新名字");

    fireEvent.click(screen.getByRole("button", { name: "重命名会话 today.csv" }));
    input = screen.getByDisplayValue("today.csv");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByDisplayValue("today.csv")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重命名会话 today.csv" }));
    input = screen.getByDisplayValue("today.csv");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onRenameSession).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "重命名会话 today.csv" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByDisplayValue("today.csv")).toBeNull();
  });

  it("删除需要二次确认，确认后回调并可取消", () => {
    const onDeleteSession = vi.fn();
    renderPanel({ onDeleteSession });

    fireEvent.click(screen.getByRole("button", { name: "删除会话 today.csv" }));
    expect(screen.getByText("删除此会话？")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByText("删除此会话？")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "删除会话 today.csv" }));
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(onDeleteSession).toHaveBeenCalledWith(SESSIONS[0]);
  });

  it("删除确认 4 秒后自动收起", () => {
    vi.useFakeTimers();
    renderPanel({ onDeleteSession: vi.fn() });
    fireEvent.click(screen.getByRole("button", { name: "删除会话 today.csv" }));
    expect(screen.getByText("删除此会话？")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.queryByText("删除此会话？")).toBeNull();
  });

  it("导入 ZIP 会回调 onImportSession，选择空文件不回调", () => {
    const onImportSession = vi.fn();
    const { container } = renderPanel({ onImportSession });
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.click(screen.getByRole("button", { name: "从 ZIP 文件导入会话" }));
    const file = new File(["zip"], "session.zip", { type: "application/zip" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(onImportSession).toHaveBeenCalledWith(file);
    fireEvent.change(input, { target: { files: [] } });
    expect(onImportSession).toHaveBeenCalledTimes(1);
  });
});
