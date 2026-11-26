import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { translate, type MessageKey } from "./i18n";
import WorkspaceTabs from "./components/WorkspaceTabs";
import { useUIStore } from "./store/useUIStore";

beforeEach(() => {
  window.localStorage.clear();
  useUIStore.setState({ locale: "zh-CN" });
  document.documentElement.lang = "";
});

afterEach(() => {
  window.localStorage.clear();
  useUIStore.setState({ locale: "zh-CN" });
});

describe("i18n 内核", () => {
  it("默认中文与改造前文案逐字一致", () => {
    expect(translate("zh-CN", "tabs.analysis")).toBe("分析");
    expect(translate("zh-CN", "cmd.newAnalysis")).toBe("新建分析");
    expect(translate("zh-CN", "shell.sessionLost")).toBe(
      "该历史会话已被服务端清理，请选择其他会话或重新上传数据。",
    );
  });

  it("英文增量与占位符插值", () => {
    expect(translate("en-US", "tabs.analysis")).toBe("Analysis");
    expect(translate("en-US", "schedules.alerts", { count: 3 })).toBe("3 alerts");
    expect(translate("zh-CN", "schedules.alerts", { count: 2 })).toBe("告警 2 条");
    // 缺失的变量保留占位符，避免渲染成 undefined。
    expect(translate("zh-CN", "shell.openSessionFailed")).toContain("{message}");
  });

  it("未知 key 回退到 key 本身", () => {
    expect(translate("en-US", "not.a.key" as MessageKey)).toBe("not.a.key");
  });

  it("切换 locale 写入 localStorage 与 html lang", () => {
    useUIStore.getState().setLocale("en-US");
    expect(window.localStorage.getItem("data-desk-locale")).toBe("en-US");
    expect(document.documentElement.lang).toBe("en-US");
    useUIStore.getState().setLocale("zh-CN");
    expect(window.localStorage.getItem("data-desk-locale")).toBe("zh-CN");
  });

  it("WorkspaceTabs 跟随 locale 切换标签与 aria-label", () => {
    useUIStore.setState({ locale: "en-US" });
    render(<WorkspaceTabs activeTab="analysis" artifactCount={2} onSelectTab={() => {}} />);
    expect(screen.getByRole("tablist", { name: "Workspace views" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Analysis/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Brief audit/ })).toBeInTheDocument();
  });
});
