import { act, renderHook } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import useTimer from "../useTimer";

// useTimer 行为测试：覆盖 App 计时展示依赖的三个语义——
//   1) running=false / 无 startedAt 时不启动 interval
//   2) 250ms tick 只在显示秒数变化时 setState（避免无谓重渲染）
//   3) 后台 tab（visibilitychange hidden）暂停、回前台立即追一次
// 用 fake timers + setSystemTime 让 Date.now 与 interval 同步推进。

const originalHidden = Object.getOwnPropertyDescriptor(Document.prototype, "hidden");
const originalVisibility = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState");

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

function makeRef(startedAt: number | null) {
  const ref = createRef<number | null>();
  ref.current = startedAt;
  return ref;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
  setHidden(false);
});

afterEach(() => {
  vi.useRealTimers();
  // 还原 jsdom 原始 getter，避免影响同文件后续用例与其它测试文件。
  if (originalHidden) Object.defineProperty(Document.prototype, "hidden", originalHidden);
  if (originalVisibility) Object.defineProperty(Document.prototype, "visibilityState", originalVisibility);
});

describe("useTimer", () => {
  it("未运行时不计时，不写入 elapsed", () => {
    const setElapsed = vi.fn();
    const ref = makeRef(Date.now() / 1000);
    renderHook(() => useTimer(false, ref, setElapsed));

    act(() => vi.advanceTimersByTime(5000));

    expect(setElapsed).not.toHaveBeenCalled();
  });

  it("running=true 但 startedAt 为空时不启动计时", () => {
    const setElapsed = vi.fn();
    const ref = makeRef(null);
    renderHook(() => useTimer(true, ref, setElapsed));

    act(() => vi.advanceTimersByTime(5000));

    expect(setElapsed).not.toHaveBeenCalled();
  });

  it("按秒去重：同一秒内的四次 tick 只写入一次", () => {
    const setElapsed = vi.fn();
    const ref = makeRef(Date.now() / 1000 - 5.2);
    renderHook(() => useTimer(true, ref, setElapsed));

    // 挂载时立即 tick 一次：5.2s
    expect(setElapsed).toHaveBeenCalledTimes(1);
    expect(setElapsed.mock.calls[0][0]).toBeGreaterThanOrEqual(5.2);
    expect(setElapsed.mock.calls[0][0]).toBeLessThan(5.3);

    // 250/500/750ms 后仍是第 5 秒 → 不重复 setState
    act(() => vi.advanceTimersByTime(750));
    expect(setElapsed).toHaveBeenCalledTimes(1);

    // 越过 6.0s 边界才第二次写入
    act(() => vi.advanceTimersByTime(250));
    expect(setElapsed).toHaveBeenCalledTimes(2);
    expect(setElapsed.mock.calls[1][0]).toBeGreaterThanOrEqual(6);
  });

  it("卸载后停止 interval，不再写入", () => {
    const setElapsed = vi.fn();
    const ref = makeRef(Date.now() / 1000);
    const { unmount } = renderHook(() => useTimer(true, ref, setElapsed));

    expect(setElapsed).toHaveBeenCalledTimes(1);
    unmount();
    act(() => vi.advanceTimersByTime(10_000));

    expect(setElapsed).toHaveBeenCalledTimes(1);
  });

  it("startedAt 在运行中被置空时 tick 安全跳过", () => {
    const setElapsed = vi.fn();
    const ref = makeRef(Date.now() / 1000);
    renderHook(() => useTimer(true, ref, setElapsed));

    ref.current = null;
    act(() => vi.advanceTimersByTime(3000));

    expect(setElapsed).toHaveBeenCalledTimes(1);
  });

  it("切到后台暂停 tick，回到前台立即追一次并恢复", () => {
    const setElapsed = vi.fn();
    const ref = makeRef(Date.now() / 1000);
    renderHook(() => useTimer(true, ref, setElapsed));

    expect(setElapsed).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(1000));
    expect(setElapsed).toHaveBeenCalledTimes(2);

    // 后台：interval 被清掉，3s 内不再写入
    act(() => {
      setHidden(true);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => vi.advanceTimersByTime(3000));
    expect(setElapsed).toHaveBeenCalledTimes(2);

    // 回前台：立即 tick 追平（4s），之后 interval 恢复
    act(() => {
      setHidden(false);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(setElapsed).toHaveBeenCalledTimes(3);
    expect(setElapsed.mock.calls[2][0]).toBeGreaterThanOrEqual(4);
    expect(setElapsed.mock.calls[2][0]).toBeLessThan(4.1);

    act(() => vi.advanceTimersByTime(3000));
    expect(setElapsed.mock.calls.length).toBeGreaterThan(3);
  });
});
