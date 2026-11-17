import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
  apiMock.mockReset();
  store.state.setKeyOpen.mockReset();
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
});
