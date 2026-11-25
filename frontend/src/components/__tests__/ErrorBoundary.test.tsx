import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

// ErrorBoundary 行为测试：子树渲染异常时的兜底界面与恢复路径。
// 覆盖：捕获异常显示消息、componentDidCatch 记录日志、「尝试恢复」重挂子树、
// 「刷新页面」调用 location.reload，以及正常渲染时透传 children。

import ErrorBoundary from "../ErrorBoundary";

let shouldThrow = true;
let errorMessage = "查询结果渲染失败";

function Bomb(): ReactNode {
  if (shouldThrow) throw new Error(errorMessage);
  return <p>已恢复的内容</p>;
}

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  shouldThrow = true;
  errorMessage = "查询结果渲染失败";
  // React 会向 console.error 打印捕获的异常堆栈；测试里静音并断言调用
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  consoleErrorSpy.mockRestore();
  vi.restoreAllMocks();
});

describe("ErrorBoundary 渲染兜底", () => {
  it("子树抛错时展示错误消息，并记录到 console.error", () => {
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("heading", { name: "渲染出现异常" })).toBeInTheDocument();
    expect(screen.getByText("查询结果渲染失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新页面" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "尝试恢复" })).toBeInTheDocument();
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "工作台渲染失败：",
      expect.any(Error),
      expect.objectContaining({ componentStack: expect.any(String) }),
    );
  });

  it("「尝试恢复」清空错误并重挂子树（修复后正常显示）", () => {
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    expect(screen.queryByText("已恢复的内容")).not.toBeInTheDocument();

    // 模拟导致异常的数据已修正
    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "尝试恢复" }));

    expect(screen.getByText("已恢复的内容")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "渲染出现异常" })).not.toBeInTheDocument();
  });

  it("「刷新页面」调用 location.reload", () => {
    const reload = vi.fn();
    const originalLocation = window.location;
    // jsdom 的 location.reload 不可导航；用可写替身观察调用
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...originalLocation, reload },
    });

    try {
      render(
        <ErrorBoundary>
          <Bomb />
        </ErrorBoundary>,
      );
      fireEvent.click(screen.getByRole("button", { name: "刷新页面" }));
      expect(reload).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: originalLocation,
      });
    }
  });

  it("没有异常时原样渲染 children", () => {
    shouldThrow = false;
    render(
      <ErrorBoundary>
        <p>正常内容</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeInTheDocument();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it("抛出的错误没有 message 时回退为错误对象文本，不渲染空白页", () => {
    errorMessage = "";
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    // message 为空字符串时退化为 String(error)，至少能看出是 Error
    expect(screen.getByRole("heading", { name: "渲染出现异常" })).toBeInTheDocument();
    expect(screen.getByText(/Error/)).toBeInTheDocument();
  });
});
