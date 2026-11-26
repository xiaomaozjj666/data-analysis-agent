import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useUIStore } from "../../store/useUIStore";

const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

import SchedulePanel from "../SchedulePanel";
import { ApiError } from "../../utils/api";

const overview = {
  generated_at: "2026-11-26T10:00:00+08:00",
  summary: { total: 1, enabled: 1, due: 0, alerts: 2, errors: 0 },
  tasks: [
    {
      id: "daily-sales",
      name: "每日销售",
      enabled: true,
      schedule: { interval_minutes: 60 },
      due: false,
      next_due_at: "2026-11-26T11:00:00+08:00",
      last_run_at: "2026-11-26T09:00:00+08:00",
      last_status: "ok",
      last_alerts: 2,
      last_alert_headline: "销售额环比下降 25%",
      last_error: null,
      state_corrupt: false,
    },
  ],
};

function defaultApi(url: string, options?: { method?: string }) {
  if (url === "/api/automation/tasks" && !options?.method) return Promise.resolve(overview);
  if (url.startsWith("/api/automation/tasks/daily-sales/history")) {
    return Promise.resolve({
      task_id: "daily-sales",
      history: [{ ran_at: "2026-11-26T09:00:00+08:00", status: "ok", alerts: [] }],
    });
  }
  if (options?.method === "PATCH") return Promise.resolve(overview);
  if (url.includes("/run")) return Promise.resolve({ alerts: [{ metric: "sales" }] });
  return Promise.resolve({});
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(defaultApi);
  useUIStore.setState({ locale: "zh-CN" });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SchedulePanel", () => {
  it("加载并展示任务状态、告警与下次到期", async () => {
    render(<SchedulePanel onClose={vi.fn()} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(await screen.findByText("每日销售")).toBeInTheDocument();
    expect(screen.getByText("已启用")).toBeInTheDocument();
    expect(screen.getByText("正常")).toBeInTheDocument();
    expect(screen.getByText("告警 2 条")).toBeInTheDocument();
    expect(screen.getByText("销售额环比下降 25%")).toBeInTheDocument();
    expect(screen.getByText(/下次到期/)).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/automation/tasks");
  });

  it("试运行发 dry_run 请求且不刷新列表", async () => {
    render(<SchedulePanel onClose={vi.fn()} />);
    await screen.findByText("每日销售");
    apiMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "试运行" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/automation/tasks/daily-sales/run?dry_run=true",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText("试运行完成：1 条告警")).toBeInTheDocument();
  });

  it("立即运行后刷新概览", async () => {
    render(<SchedulePanel onClose={vi.fn()} />);
    await screen.findByText("每日销售");
    apiMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "立即运行" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/automation/tasks/daily-sales/run",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/automation/tasks"));
    expect(await screen.findByText("已运行：1 条告警")).toBeInTheDocument();
  });

  it("启停按钮发 PATCH 并更新状态", async () => {
    render(<SchedulePanel onClose={vi.fn()} />);
    await screen.findByText("每日销售");
    apiMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "已启用" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/automation/tasks/daily-sales",
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
    expect(JSON.parse(apiMock.mock.calls[0][1].body as string)).toEqual({ enabled: false });
  });

  it("展开历史按需拉取并渲染记录", async () => {
    render(<SchedulePanel onClose={vi.fn()} />);
    await screen.findByText("每日销售");
    fireEvent.click(screen.getByRole("button", { name: /历史/ }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/api/automation/tasks/daily-sales/history"),
    );
    expect(await screen.findByText("ok")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /收起历史/ })).toBeInTheDocument();
  });

  it("空任务显示引导文案", async () => {
    apiMock.mockResolvedValue({
      generated_at: "",
      summary: { total: 0, enabled: 0, due: 0, alerts: 0, errors: 0 },
      tasks: [],
    });
    render(<SchedulePanel onClose={vi.fn()} />);
    expect(await screen.findByText(/还没有定时任务/)).toBeInTheDocument();
  });

  it("加载失败显示错误", async () => {
    apiMock.mockRejectedValueOnce(new Error("服务不可用"));
    render(<SchedulePanel onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("加载定时任务失败：服务不可用");
  });

  it("运行返回 409 时提示任务正在运行", async () => {
    apiMock.mockImplementation((url: string) => {
      if (url.includes("/run")) return Promise.reject(new ApiError("locked", 409));
      return Promise.resolve(overview);
    });
    render(<SchedulePanel onClose={vi.fn()} />);
    await screen.findByText("每日销售");
    fireEvent.click(screen.getByRole("button", { name: "立即运行" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("任务正在运行，请稍后再试。");
  });

  it("关闭按钮与 Esc 触发 onClose", async () => {
    const onClose = vi.fn();
    render(<SchedulePanel onClose={onClose} />);
    await screen.findByText("每日销售");
    fireEvent.click(screen.getByRole("button", { name: "关闭定时任务" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("英文 locale 下渲染英文文案", async () => {
    useUIStore.setState({ locale: "en-US" });
    render(<SchedulePanel onClose={vi.fn()} />);
    expect(await screen.findByText("Schedules")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Enabled")).toBeInTheDocument();
  });
});
