import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConversationThread from "../ConversationThread";
import type { FollowUpMessage } from "../../types";

const clipboardWrite = vi.fn();

function renderThread(overrides: Record<string, unknown> = {}) {
  const props = {
    messages: [] as FollowUpMessage[],
    input: "",
    onInputChange: vi.fn(),
    onSubmit: vi.fn(),
    onStop: vi.fn(),
    running: false,
    disabled: false,
    ...overrides,
  };
  const utils = render(<ConversationThread {...props} />);
  return { ...utils, props };
}

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

describe("ConversationThread", () => {
  it("空对话只展示输入框，shift+enter 不提交", () => {
    const { props, container } = renderThread();
    expect(container.querySelector(".conversation-thread")).toBeTruthy();
    const textarea = screen.getByPlaceholderText(/追问任何关于数据的问题/);
    fireEvent.change(textarea, { target: { value: "继续" } });
    expect(props.onInputChange).toHaveBeenCalledWith("继续");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(props.onSubmit).not.toHaveBeenCalled();
    // 空输入时发送按钮禁用
    const button = screen.getByRole("button", { name: /发送追问/ });
    expect(button).toBeDisabled();
  });

  it("Enter 提交，running 时改为停止按钮且不提交", () => {
    const { props, rerender } = renderThread({ input: "继续分析" });
    const textarea = screen.getByPlaceholderText(/追问任何关于数据的问题/);
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(props.onSubmit).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /发送追问/ }));
    expect(props.onSubmit).toHaveBeenCalledTimes(2);

    rerender(<ConversationThread {...props} input="继续分析" running />);
    fireEvent.keyDown(screen.getByPlaceholderText(/追问任何关于数据的问题/), { key: "Enter" });
    expect(props.onSubmit).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: /停止/ }));
    expect(props.onStop).toHaveBeenCalled();
  });

  it("未配置 Key 时禁用输入并提示先配置", () => {
    renderThread({ disabled: true });
    expect(screen.getByPlaceholderText("请先配置 API Key 后再追问")).toBeDisabled();
    expect(screen.getByRole("button", { name: /发送追问/ })).toBeDisabled();
  });

  it("用户气泡：复制成功切换对勾", async () => {
    renderThread({
      messages: [{ role: "user", content: "把图改成红色" }],
      input: "",
    });
    fireEvent.click(screen.getByRole("button", { name: "复制消息" }));
    await waitFor(() => expect(clipboardWrite).toHaveBeenCalledWith("把图改成红色"));
  });

  it("用户气泡：复制失败给出失败提示", async () => {
    clipboardWrite.mockRejectedValueOnce(new Error("denied"));
    renderThread({
      messages: [{ role: "user", content: "复制我" }],
      input: "",
    });
    fireEvent.click(screen.getByRole("button", { name: "复制消息" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "复制消息" })).toHaveAttribute(
        "title",
        "复制失败，请手动选择文本复制",
      ),
    );
  });

  it("编辑并重发：保存后回传截断后的新内容", () => {
    const onEditMessage = vi.fn();
    renderThread({
      messages: [{ role: "user", content: "旧问题" }],
      input: "",
      onEditMessage,
    });
    fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
    const editor = screen.getByDisplayValue("旧问题");
    fireEvent.change(editor, { target: { value: "  新问题  " } });
    fireEvent.click(screen.getByRole("button", { name: /保存并重发/ }));
    expect(onEditMessage).toHaveBeenCalledWith(0, "新问题");
    expect(screen.queryByDisplayValue("新问题")).toBeNull();
  });

  it("编辑内容未变化或为空时不重发，取消可退出编辑", () => {
    const onEditMessage = vi.fn();
    renderThread({
      messages: [{ role: "user", content: "旧问题" }],
      input: "",
      onEditMessage,
    });
    fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
    fireEvent.click(screen.getByRole("button", { name: /保存并重发/ }));
    expect(onEditMessage).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByDisplayValue("旧问题")).toBeNull();
  });

  it("running 或禁用时隐藏编辑入口", () => {
    renderThread({
      messages: [{ role: "user", content: "旧问题" }],
      input: "",
      onEditMessage: vi.fn(),
      running: true,
    });
    expect(screen.queryByRole("button", { name: "编辑消息" })).toBeNull();
  });

  it("助手气泡：Markdown、思考过程、工具 chip、用量与错误", () => {
    const { container } = renderThread({
      messages: [
        {
          role: "assistant",
          content: "结论是 **增长**",
          reasoning: "先看趋势",
          tools: [
            {
              call_id: "c1",
              name: "sql_query",
              status: "done",
              input_preview: '{"sql":"select 1"}',
            },
          ],
          usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
        },
      ],
      input: "",
    });
    expect(container.textContent).toContain("增长");
    // 非流式时思考过程默认折叠，点击后展开
    expect(container.textContent).toContain("4 字");
    fireEvent.click(screen.getByRole("button", { name: /思考过程/ }));
    expect(container.textContent).toContain("先看趋势");
    expect(container.querySelector(".chat-tool-chip")).toBeTruthy();
    expect(container.textContent).toContain("共");
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    expect(clipboardWrite).toHaveBeenCalledWith("结论是 **增长**");
  });

  it("助手流式空内容显示思考占位，错误单独提示", () => {
    renderThread({
      messages: [
        { role: "assistant", content: "", streaming: true },
        { role: "assistant", content: "已生成", error: "工具调用失败" },
      ],
      input: "",
    });
    expect(screen.getAllByText("正在思考…").length).toBeGreaterThan(0);
    expect(screen.getByText("工具调用失败")).toBeInTheDocument();
  });

  it("滚动监听与新增消息不会抛错", () => {
    const { container, rerender, props } = renderThread({
      messages: [{ role: "assistant", content: "第一段" }],
      input: "",
    });
    const list = container.querySelector(".conversation-list") as HTMLElement;
    Object.defineProperty(list, "scrollHeight", { value: 100, configurable: true });
    Object.defineProperty(list, "clientHeight", { value: 50, configurable: true });
    Object.defineProperty(list, "scrollTop", { value: 10, configurable: true, writable: true });
    fireEvent.scroll(list);
    expect(list.scrollTop).toBe(10);
    act(() => {
      rerender(
        <ConversationThread
          {...props}
          messages={[
            { role: "assistant", content: "第一段" },
            { role: "assistant", content: "第二段" },
          ]}
        />,
      );
    });
    expect(screen.getByText("第二段")).toBeInTheDocument();
  });
});
