import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const store = vi.hoisted(() => {
  const state = {
    settings: { model: "deepseek-chat", configured: false },
    apiKey: "test-key",
    effort: "high",
    thinking: true,
    showKey: false,
    keyOpen: true,
    savingSettings: false,
    setSettings: vi.fn(),
    setApiKey: vi.fn(),
    setEffort: vi.fn(),
    setThinking: vi.fn(),
    setShowKey: vi.fn(),
    setKeyOpen: vi.fn(),
    setSavingSettings: vi.fn(),
    setError: vi.fn(),
  };
  return { state };
});

vi.mock("../../store/useAppStore", () => ({ useAppStore: () => store.state }));
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

import SettingsPanel from "../SettingsPanel";
import { useUIStore } from "../../store/useUIStore";

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
  apiMock.mockReset();
  store.state.setKeyOpen.mockReset();
  useUIStore.setState({ locale: "zh-CN" });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsPanel", () => {
  it("打开对话框并带 aria-expanded", () => {
    render(<SettingsPanel />);
    const trigger = screen.getByRole("button", { name: /deepseek-chat/ });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
  });

  it("测试连接只发请求、不保存设置", async () => {
    apiMock.mockResolvedValue({ message: "模型服务连接成功。" });
    render(<SettingsPanel />);

    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/settings/test",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const body = JSON.parse(apiMock.mock.calls[0][1].body as string) as Record<string, unknown>;
    expect(body).toEqual({ api_key: "test-key" });
    expect(await screen.findByRole("status")).toHaveTextContent("模型服务连接成功");
    expect(store.state.setSettings).not.toHaveBeenCalled();
  });

  it("关闭按钮收起设置", () => {
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "收起设置" }));
    expect(store.state.setKeyOpen).toHaveBeenCalledWith(false);
  });

  it("语言选择立即切换界面语言并记忆", () => {
    render(<SettingsPanel />);
    fireEvent.change(screen.getByLabelText("界面语言"), { target: { value: "en-US" } });
    expect(useUIStore.getState().locale).toBe("en-US");
    expect(window.localStorage.getItem("data-desk-locale")).toBe("en-US");
    expect(screen.getByRole("heading", { name: "Model settings" })).toBeInTheDocument();
  });

  it("保存成功写入设置，并把后端 warning 透出到全局错误", async () => {
    const payload = { model: "deepseek-chat", configured: true, warning: "密钥已保存，但未验证。" };
    apiMock.mockResolvedValue(payload);
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /保存设置/ }));
    await waitFor(() => expect(store.state.setSettings).toHaveBeenCalledWith(payload));
    const body = JSON.parse(apiMock.mock.calls[0][1].body as string) as Record<string, unknown>;
    expect(body).toEqual({
      api_key: "test-key",
      thinking_enabled: true,
      reasoning_effort: "high",
      persist_key: true,
    });
    expect(store.state.setError).toHaveBeenCalledWith("密钥已保存，但未验证。");
    expect(store.state.setSavingSettings).toHaveBeenCalledWith(false);
  });

  it("保存失败时提示错误并复位保存中状态", async () => {
    apiMock.mockRejectedValue(new Error("写入失败"));
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /保存设置/ }));
    await waitFor(() => expect(store.state.setError).toHaveBeenCalledWith("写入失败"));
    expect(store.state.setSavingSettings).toHaveBeenCalledWith(false);
  });

  it("连接测试失败时展示失败原因", async () => {
    apiMock.mockRejectedValue(new Error("鉴权失败"));
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByRole("status")).toHaveTextContent("鉴权失败");
    expect(screen.getByRole("status")).toHaveTextContent("✗");
  });

  it("连接测试超过 10 秒自动超时并提示重试", () => {
    vi.useFakeTimers();
    apiMock.mockImplementation(() => new Promise(() => {}));
    render(<SettingsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(screen.getByRole("status")).toHaveTextContent("测试连接超时，请重试。");
    vi.useRealTimers();
  });

  it("按键可见性切换、思考模式与推理强度写入 store", () => {
    render(<SettingsPanel />);
    const input = screen.getByLabelText("API Key") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByTitle("显示 Key"));
    expect(store.state.setShowKey).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(store.state.setThinking).toHaveBeenCalledWith(false);
    fireEvent.click(screen.getByRole("button", { name: "深度" }));
    expect(store.state.setEffort).toHaveBeenCalledWith("max");
  });

  it("Esc / 点击遮罩关闭对话框", () => {
    render(<SettingsPanel />);
    const dialog = document.querySelector("dialog") as HTMLDialogElement;
    fireEvent(dialog, new Event("cancel", { bubbles: true, cancelable: true }));
    expect(store.state.setKeyOpen).toHaveBeenCalledWith(false);
    store.state.setKeyOpen.mockClear();
    fireEvent.click(dialog);
    expect(store.state.setKeyOpen).toHaveBeenCalledWith(false);
  });
});
