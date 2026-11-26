import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CodeBlock from "../CodeBlock";

const clipboardWrite = vi.fn();

beforeEach(() => {
  clipboardWrite.mockReset();
  clipboardWrite.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: clipboardWrite },
    configurable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CodeBlock", () => {
  it("展示语言标签并复制代码", async () => {
    const { container } = render(<CodeBlock language="sql" value="SELECT 1" theme="dark" />);
    expect(container.querySelector(".code-block-lang")).toHaveTextContent("sql");
    fireEvent.click(screen.getByRole("button", { name: "复制代码" }));
    await waitFor(() => expect(clipboardWrite).toHaveBeenCalledWith("SELECT 1"));
    expect(await screen.findByText("已复制")).toBeInTheDocument();
  });

  it("复制失败时给出失败状态", async () => {
    clipboardWrite.mockRejectedValueOnce(new Error("denied"));
    render(<CodeBlock value="print(1)" />);
    // 未传 language 时回退 text
    expect(screen.getByText("text")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制代码" }));
    expect(await screen.findByText("失败")).toBeInTheDocument();
  });
});
