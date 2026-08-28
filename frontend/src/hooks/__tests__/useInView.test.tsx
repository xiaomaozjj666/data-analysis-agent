import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import useInView from "../useInView";

// useInView 行为测试：进入/离开视口的状态翻转，以及无 IntersectionObserver
// 环境下的"立即可见"降级（产物网格迷你图懒加载依赖这两个语义）。

type Callback = (entries: { isIntersecting: boolean; target: Element }[], observer: unknown) => void;

class ControllableIO {
  static instances: ControllableIO[] = [];
  callback: Callback;
  constructor(callback: Callback) {
    this.callback = callback;
    ControllableIO.instances.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
  trigger(isIntersecting: boolean, target: Element = document.body) {
    this.callback([{ isIntersecting, target }], this);
  }
}

// Probe：把 ref 真正挂到 DOM 上（ref.current 为 null 时 hook 不观测），
// 并把最新 inView 暴露给断言。
let latest = false;
function Probe() {
  const { ref, inView } = useInView<HTMLDivElement>();
  latest = inView;
  return <div ref={ref} data-testid="probe" />;
}

afterEach(() => {
  vi.unstubAllGlobals();
  ControllableIO.instances = [];
  latest = false;
});

describe("useInView", () => {
  it("IntersectionObserver 可用时随回调翻转 true/false", () => {
    vi.stubGlobal("IntersectionObserver", ControllableIO as unknown as typeof IntersectionObserver);
    render(<Probe />);
    expect(ControllableIO.instances.length).toBe(1);
    const io = ControllableIO.instances[0];
    act(() => io.trigger(true));
    expect(latest).toBe(true);
    act(() => io.trigger(false));
    expect(latest).toBe(false);
  });

  it("IntersectionObserver 不可用时降级为立即可见", () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    render(<Probe />);
    expect(latest).toBe(true);
  });
});
