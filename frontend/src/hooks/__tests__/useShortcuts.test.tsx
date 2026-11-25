import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import useShortcuts from "../useShortcuts";
import type { Artifact } from "../../types";

// useShortcuts 行为测试：把 App.tsx 里两处 keydown useEffect 的契约固化下来——
// Esc 关预览、Cmd+K/B/.(停止)、? 帮助、T 主题、1-5 切 Tab，
// 以及"输入框里打字不触发单字符快捷键"和"全屏时不抢按键"。

type Deps = Parameters<typeof useShortcuts>[0];
// deps 里的回调在测试里全部是 vi.fn()，但 hook 的公开签名只要求普通函数；
// 这里取交集，既满足 Deps，又能在断言里访问 .mock。
type MockDeps = {
  [K in keyof Deps]: Deps[K] extends (...args: never[]) => unknown ? Deps[K] & Mock : Deps[K];
};

function makeDeps(overrides: Partial<Deps> = {}): MockDeps {
  const deps = {
    previewItem: null,
    keyOpen: false,
    closeArtifactPreview: vi.fn(),
    running: false,
    chatRunning: false,
    stopAnalysis: vi.fn(),
    stopFollowUp: vi.fn(),
    toggleTheme: vi.fn(),
    setCommandOpen: vi.fn(),
    setHistoryExpanded: vi.fn(),
    setHelpOpen: vi.fn(),
    setActiveTab: vi.fn(),
    ...overrides,
  };
  return deps as unknown as MockDeps;
}

function setup(overrides: Partial<Deps> = {}) {
  const deps = makeDeps(overrides);
  const view = renderHook((props: Deps) => useShortcuts(props), { initialProps: deps });
  return { ...view, deps };
}

function press(key: string, init: KeyboardEventInit = {}, target?: HTMLElement): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  act(() => {
    (target ?? window).dispatchEvent(event);
  });
  return event;
}

// 功能键位断言辅助：setter 收到的是 functional updater，调用它验证取反结果。
function invert(updater: unknown): boolean {
  expect(typeof updater).toBe("function");
  return (updater as (v: boolean) => boolean)(false);
}

afterEach(() => {
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => null });
});

describe("useShortcuts - Esc 关闭预览", () => {
  it("有预览项且无弹层时 Esc 关闭预览", () => {
    const { deps } = setup({ previewItem: { name: "a.html" } as Artifact });
    const event = press("Escape");
    expect(deps.closeArtifactPreview).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(false);
  });

  it("帮助/命令面板打开时 Esc 交给原生 dialog 处理", () => {
    const { deps } = setup({ previewItem: { name: "a.html" } as Artifact, keyOpen: true });
    press("Escape");
    expect(deps.closeArtifactPreview).not.toHaveBeenCalled();
  });

  it("没有预览项时不监听/不关闭", () => {
    const { deps } = setup();
    press("Escape");
    expect(deps.closeArtifactPreview).not.toHaveBeenCalled();
  });

  it("全屏时不关闭预览", () => {
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => document.body,
    });
    const { deps } = setup({ previewItem: { name: "a.html" } as Artifact });
    press("Escape");
    expect(deps.closeArtifactPreview).not.toHaveBeenCalled();
  });
});

describe("useShortcuts - 全局快捷键", () => {
  it("Cmd+K 切换命令面板，Ctrl+K 等价", () => {
    const { deps } = setup();
    const first = press("k", { metaKey: true });
    expect(first.defaultPrevented).toBe(true);
    expect(invert(deps.setCommandOpen.mock.calls[0][0])).toBe(true);

    press("K", { ctrlKey: true });
    expect(deps.setCommandOpen).toHaveBeenCalledTimes(2);
  });

  it("Cmd+B 折叠/展开历史侧栏", () => {
    const { deps } = setup();
    const event = press("b", { metaKey: true });
    expect(event.defaultPrevented).toBe(true);
    expect(invert(deps.setHistoryExpanded.mock.calls[0][0])).toBe(true);
  });

  it("Cmd+. 优先停止正在运行的分析", () => {
    const { deps } = setup({ running: true, chatRunning: true });
    const event = press(".", { metaKey: true });
    expect(event.defaultPrevented).toBe(true);
    expect(deps.stopAnalysis).toHaveBeenCalledTimes(1);
    expect(deps.stopFollowUp).not.toHaveBeenCalled();
  });

  it("Cmd+. 在仅追问运行时停止追问", () => {
    const { deps } = setup({ chatRunning: true });
    press(".", { ctrlKey: true });
    expect(deps.stopFollowUp).toHaveBeenCalledTimes(1);
    expect(deps.stopAnalysis).not.toHaveBeenCalled();
  });

  it("Cmd+. 在没有进行中任务时不做任何事", () => {
    const { deps } = setup();
    const event = press(".", { metaKey: true });
    expect(event.defaultPrevented).toBe(false);
    expect(deps.stopAnalysis).not.toHaveBeenCalled();
    expect(deps.stopFollowUp).not.toHaveBeenCalled();
  });

  it("? 打开帮助，T 切换主题", () => {
    const { deps } = setup();
    const help = press("?");
    expect(help.defaultPrevented).toBe(true);
    expect(invert(deps.setHelpOpen.mock.calls[0][0])).toBe(true);

    press("t");
    expect(deps.toggleTheme).toHaveBeenCalledTimes(1);

    // Alt+T 不是主题快捷键
    press("t", { altKey: true });
    expect(deps.toggleTheme).toHaveBeenCalledTimes(1);
  });

  it("数字 1-5 切换五个 Tab", () => {
    const { deps } = setup();
    for (const key of ["1", "2", "3", "4", "5"]) press(key);
    expect(deps.setActiveTab.mock.calls.map((c) => c[0])).toEqual([
      "analysis",
      "data",
      "artifacts",
      "audit",
      "comments",
    ]);
  });

  it("输入框内打字不触发单字符快捷键，但 Cmd 组合键仍生效", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    try {
      const { deps } = setup();
      press("?", {}, input);
      press("t", {}, input);
      press("1", {}, input);
      expect(deps.setHelpOpen).not.toHaveBeenCalled();
      expect(deps.toggleTheme).not.toHaveBeenCalled();
      expect(deps.setActiveTab).not.toHaveBeenCalled();

      press("k", { metaKey: true }, input);
      expect(deps.setCommandOpen).toHaveBeenCalledTimes(1);
    } finally {
      input.remove();
    }
  });

  it("keyOpen 时不绑定全局监听", () => {
    const { deps } = setup({ keyOpen: true });
    press("k", { metaKey: true });
    press("?");
    expect(deps.setCommandOpen).not.toHaveBeenCalled();
    expect(deps.setHelpOpen).not.toHaveBeenCalled();
  });

  it("全屏时忽略所有全局快捷键", () => {
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => document.body,
    });
    const { deps } = setup({ running: true });
    press("k", { metaKey: true });
    press("?");
    press("1");
    press(".", { metaKey: true });
    expect(deps.setCommandOpen).not.toHaveBeenCalled();
    expect(deps.setHelpOpen).not.toHaveBeenCalled();
    expect(deps.setActiveTab).not.toHaveBeenCalled();
    expect(deps.stopAnalysis).not.toHaveBeenCalled();
  });

  it("卸载后移除监听", () => {
    const { deps, unmount } = setup();
    unmount();
    press("k", { metaKey: true });
    expect(deps.setCommandOpen).not.toHaveBeenCalled();
  });
});
