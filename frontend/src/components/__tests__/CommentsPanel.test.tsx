import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
vi.mock("../../utils/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

import CommentsPanel from "../CommentsPanel";

beforeEach(() => {
  apiMock.mockReset();
  window.localStorage.clear();
});

describe("CommentsPanel", () => {
  it("加载并发表评论", async () => {
    apiMock.mockResolvedValueOnce({ comments: [] });
    render(<CommentsPanel sessionId="s1" />);
    expect(await screen.findByText(/还没有评论/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("署名"), { target: { value: "小王" } });
    fireEvent.change(screen.getByLabelText("评论"), { target: { value: "口径需要复核" } });
    apiMock.mockResolvedValueOnce({
      id: "c1",
      text: "口径需要复核",
      author: "小王",
      created_at: "2026-11-18T08:00:00+00:00",
    });
    fireEvent.click(screen.getByRole("button", { name: "发表评论" }));

    await waitFor(() => expect(screen.getByText("口径需要复核")).toBeInTheDocument());
    expect(screen.getByText("小王")).toBeInTheDocument();
    const [, options] = apiMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(options.body))).toEqual({ text: "口径需要复核", author: "小王" });
    expect(window.localStorage.getItem("data-agent-comment-author")).toBe("小王");
  });

  it("删除评论并从列表移除", async () => {
    apiMock.mockResolvedValueOnce({
      comments: [
        { id: "c1", text: "待删除", author: "小李", created_at: "2026-11-18T08:00:00+00:00" },
      ],
    });
    render(<CommentsPanel sessionId="s1" />);
    expect(await screen.findByText("待删除")).toBeInTheDocument();

    apiMock.mockResolvedValueOnce({ deleted: true });
    fireEvent.click(screen.getByRole("button", { name: "删除 小李 的评论" }));
    await waitFor(() => expect(screen.queryByText("待删除")).not.toBeInTheDocument());
    expect(apiMock.mock.calls[1][0]).toContain("/comments/c1");
    expect((apiMock.mock.calls[1][1] as RequestInit).method).toBe("DELETE");
  });

  it("接口错误会显示且不清空草稿", async () => {
    apiMock.mockResolvedValueOnce({ comments: [] });
    render(<CommentsPanel sessionId="s1" />);
    await screen.findByText(/还没有评论/);
    fireEvent.change(screen.getByLabelText("评论"), { target: { value: "保留我" } });
    apiMock.mockRejectedValueOnce(new Error("只读令牌不能执行写操作。"));
    fireEvent.click(screen.getByRole("button", { name: "发表评论" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("只读令牌");
    expect(screen.getByLabelText("评论")).toHaveValue("保留我");
  });
});
