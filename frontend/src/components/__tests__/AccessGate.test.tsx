import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// AccessGate 行为测试：部署访问令牌门禁。
// 覆盖：空令牌禁用提交、校验成功（写入 localStorage 并放行）、服务端判定
// 令牌无效（回退删除本地令牌并展示错误）、请求异常、验证中的 busy 态，
// 以及 Metric 展示组件。api 层被 mock，不发起真实请求。

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("../../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../utils/api")>();
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

import { AccessGate, Metric } from "../AccessGate";
import { ACCESS_TOKEN_KEY } from "../../constants";

beforeEach(() => {
  apiMock.mockReset();
  window.localStorage.clear();
});

describe("AccessGate 访问门禁", () => {
  it("未输入令牌时提交按钮禁用，点击表单不发请求", () => {
    const onAuthenticated = vi.fn();
    const { container } = render(<AccessGate onAuthenticated={onAuthenticated} />);

    expect(screen.getByRole("button", { name: "进入工作台" })).toBeDisabled();
    fireEvent.submit(container.querySelector("form")!);
    expect(apiMock).not.toHaveBeenCalled();
    expect(onAuthenticated).not.toHaveBeenCalled();
  });

  it("只输入空白也不允许提交", () => {
    const onAuthenticated = vi.fn();
    const { container } = render(<AccessGate onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByPlaceholderText("应用访问令牌"), {
      target: { value: "   " },
    });
    expect(screen.getByRole("button", { name: "进入工作台" })).toBeDisabled();
    fireEvent.submit(container.querySelector("form")!);
    expect(apiMock).not.toHaveBeenCalled();
  });

  it("校验成功后写入 localStorage 并放行", async () => {
    apiMock.mockResolvedValue({ required: true, authenticated: true });
    const onAuthenticated = vi.fn();
    render(<AccessGate onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByPlaceholderText("应用访问令牌"), {
      target: { value: "  secret-token  " },
    });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "进入工作台" }).closest("form")!);
    });

    expect(apiMock).toHaveBeenCalledWith("/api/auth");
    // 令牌去首尾空白后保存
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBe("secret-token");
    expect(onAuthenticated).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("访问令牌无效。")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "进入工作台" })).toBeEnabled();
  });

  it("服务端判定未认证时删除本地令牌并展示错误", async () => {
    apiMock.mockResolvedValue({ required: true, authenticated: false });
    const onAuthenticated = vi.fn();
    render(<AccessGate onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByPlaceholderText("应用访问令牌"), {
      target: { value: "wrong-token" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "进入工作台" }).closest("form")!);
    });

    expect(await screen.findByText("访问令牌无效。")).toBeInTheDocument();
    // 无效令牌不能留在 localStorage，否则下次启动会带着它重试
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(onAuthenticated).not.toHaveBeenCalled();
  });

  it("请求异常时展示异常消息并删除本地令牌", async () => {
    apiMock.mockRejectedValue(new Error("网络不可达"));
    const onAuthenticated = vi.fn();
    render(<AccessGate onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByPlaceholderText("应用访问令牌"), {
      target: { value: "token" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "进入工作台" }).closest("form")!);
    });

    expect(await screen.findByText("网络不可达")).toBeInTheDocument();
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(onAuthenticated).not.toHaveBeenCalled();
  });

  it("验证期间按钮显示「验证中」并禁用，防止重复提交", async () => {
    let release: ((value: unknown) => void) | undefined;
    apiMock.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const onAuthenticated = vi.fn();
    render(<AccessGate onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByPlaceholderText("应用访问令牌"), {
      target: { value: "token" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "进入工作台" }).closest("form")!);
    });

    const busyButton = screen.getByRole("button", { name: "验证中" });
    expect(busyButton).toBeDisabled();
    expect(apiMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      release?.({ authenticated: true });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "进入工作台" })).toBeEnabled(),
    );
    expect(onAuthenticated).toHaveBeenCalledTimes(1);
  });
});

describe("Metric 指标展示", () => {
  it("渲染标签、数值与可选单位", () => {
    const { container } = render(<Metric label="总销售额" value="17224" unit=" 元" />);
    expect(screen.getByText("总销售额")).toBeInTheDocument();
    expect(container.querySelector(".metric strong")).toHaveTextContent("17224 元");
  });

  it("没有单位时只渲染数值", () => {
    const { container } = render(<Metric label="订单数" value={462} />);
    expect(container.querySelector(".metric strong")?.textContent).toBe("462");
    expect(container.querySelector(".metric small")?.textContent).toBe("");
  });
});
