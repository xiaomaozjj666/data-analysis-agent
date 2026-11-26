import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatToolChip, ToolTraceItem } from "../ToolTraceItem";

const TOOL = {
  call_id: "c1",
  name: "inspect_data",
  status: "done" as const,
  input_preview: '{"limit": 5}',
  output_preview: '{"rows": 2}',
  duration_ms: 12,
};

describe("ToolTraceItem", () => {
  it("无输入输出时不可展开", () => {
    const { container } = render(
      <ToolTraceItem tool={{ call_id: "c1", name: "unknown_tool", status: "done" }} />,
    );
    expect(container.querySelector(".tool-trace-row")).not.toHaveAttribute("role");
    expect(screen.getByText("unknown_tool")).toBeInTheDocument();
  });

  it("点击行展开输入输出，再点收起", () => {
    const { container } = render(<ToolTraceItem tool={TOOL} />);
    expect(container.querySelector(".tool-trace-detail")).toBeNull();
    fireEvent.click(container.querySelector(".tool-trace-row") as HTMLElement);
    expect(container.querySelector(".tool-trace-detail")).toBeTruthy();
    expect(container.textContent).toContain("输入");
    expect(container.textContent).toContain("输出");
    expect(container.textContent).toContain("12ms");
    fireEvent.click(container.querySelector(".tool-trace-row") as HTMLElement);
    expect(container.querySelector(".tool-trace-detail")).toBeNull();
  });

  it("键盘 Enter / 空格可展开，running 时显示加载图标", () => {
    const { container } = render(
      <ToolTraceItem tool={{ ...TOOL, status: "running" }} defaultExpanded />,
    );
    // 默认展开
    expect(container.querySelector(".tool-trace-detail")).toBeTruthy();
    expect(container.querySelector(".spin")).toBeTruthy();
    const row = container.querySelector(".tool-trace-row") as HTMLElement;
    fireEvent.keyDown(row, { key: "Enter" });
    expect(container.querySelector(".tool-trace-detail")).toBeNull();
    fireEvent.keyDown(row, { key: " " });
    expect(container.querySelector(".tool-trace-detail")).toBeTruthy();
    // 其他按键不触发
    fireEvent.keyDown(row, { key: "a" });
    expect(container.querySelector(".tool-trace-detail")).toBeTruthy();
  });
});

describe("ChatToolChip", () => {
  it("点击展开详情，点击详情不冒泡收起", () => {
    const { container } = render(<ChatToolChip tool={TOOL} />);
    const chip = container.querySelector(".chat-tool-chip") as HTMLElement;
    expect(container.querySelector(".chat-tool-detail")).toBeNull();
    fireEvent.click(chip);
    expect(container.querySelector(".chat-tool-detail")).toBeTruthy();
    fireEvent.click(container.querySelector(".chat-tool-detail") as HTMLElement);
    expect(container.querySelector(".chat-tool-detail")).toBeTruthy();
    fireEvent.keyDown(chip, { key: "Enter" });
    expect(container.querySelector(".chat-tool-detail")).toBeNull();
  });

  it("running 状态显示加载图标且无详情时不可展开", () => {
    const { container } = render(
      <ChatToolChip tool={{ call_id: "c2", name: "sql_query", status: "running" }} />,
    );
    const chip = container.querySelector(".chat-tool-chip") as HTMLElement;
    expect(chip.className).toContain("is-running");
    expect(chip).not.toHaveAttribute("role");
    expect(container.querySelector(".spin")).toBeTruthy();
    fireEvent.click(chip);
    expect(container.querySelector(".chat-tool-detail")).toBeNull();
  });
});
