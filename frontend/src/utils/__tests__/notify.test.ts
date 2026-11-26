import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { notifyAnalysisDone } from "../notify";

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
}

class NotificationStub {
  static permission: NotificationPermission = "granted";
  static instances: NotificationStub[] = [];
  constructor(public title: string, public options?: NotificationOptions) {
    NotificationStub.instances.push(this);
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("Notification", NotificationStub);
  NotificationStub.instances = [];
  NotificationStub.permission = "granted";
  document.title = "原始标题";
});

afterEach(() => {
  // 触发 focus 清理计时器（多次以消费掉各自注册的 once 监听），避免污染下一个用例
  for (let i = 0; i < 3; i += 1) window.dispatchEvent(new Event("focus"));
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.title = "";
});

describe("notifyAnalysisDone", () => {
  it("页面可见时不打扰用户", () => {
    setVisibility("visible");
    notifyAnalysisDone("分析已完成", "正文");
    expect(NotificationStub.instances).toHaveLength(0);
    expect(document.title).toBe("原始标题");
  });

  it("页面隐藏且已授权时发系统通知并闪烁标题", () => {
    setVisibility("hidden");
    notifyAnalysisDone("分析已完成", "正文");
    expect(NotificationStub.instances).toHaveLength(1);
    expect(NotificationStub.instances[0].title).toBe("分析已完成");
    // 首次 flash 先回到原标题，下一秒切换为完成标记
    expect(document.title).toBe("原始标题");
    vi.advanceTimersByTime(1000);
    expect(document.title).toBe("✅ 分析已完成");
    vi.advanceTimersByTime(1000);
    expect(document.title).toBe("原始标题");
  });

  it("未授权时只闪烁标题，不发通知", () => {
    setVisibility("hidden");
    NotificationStub.permission = "denied";
    notifyAnalysisDone("分析失败", "正文");
    expect(NotificationStub.instances).toHaveLength(0);
    vi.advanceTimersByTime(1000);
    expect(document.title).toBe("✅ 分析失败");
  });

  it("用户回到页面后停止闪烁并恢复标题", () => {
    setVisibility("hidden");
    notifyAnalysisDone("分析已完成", "正文");
    window.dispatchEvent(new Event("focus"));
    expect(document.title).toBe("原始标题");
    vi.advanceTimersByTime(3000);
    expect(document.title).toBe("原始标题");
  });

  it("连续调用会清理上一次的闪烁计时器", () => {
    setVisibility("hidden");
    notifyAnalysisDone("第一次", "正文");
    notifyAnalysisDone("第二次", "正文");
    // 只在第二次调用后继续闪烁，不会出现两个 interval 交替覆盖
    vi.advanceTimersByTime(1000);
    expect(document.title).toBe("✅ 第二次");
    vi.advanceTimersByTime(1000);
    expect(document.title).toBe("原始标题");
  });
});
