import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SalesAudit from "../SalesAudit";
import { ApiError } from "../../utils/api";

const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

const AUDIT_ID = `aud-${"a".repeat(32)}`;

const VERIFIED = {
  schema_version: 1,
  audit_id: AUDIT_ID,
  source: {
    filename: "sales.csv",
    sha256: "a".repeat(64),
    rows: 2,
    encoding: "utf-8",
    preview: [],
    order_ids: 1,
  },
  metrics: { 销售额合计: "100.00", 销量合计: "2", 订单数: "1", 客单价: "100.00" },
  warnings: [],
  summary: { verified: 1, conflict: 0, unsupported: 0 },
  claims: [
    {
      line: 1,
      text: "订单数：1 笔",
      metric: "订单数",
      status: "verified",
      reason: "原值与明细复算一致。",
      submitted: "1 笔",
      recalculated: "1 笔",
      formula: "COUNT(DISTINCT order_id)",
    },
  ],
  needs_scope_confirmation: false,
  confirmed_full_table: false,
  corrected_brief: "# 经营简报（核验版）\n\n全表统计。\n",
  ledger: { seq: 3, hash: "e".repeat(64), prev: "f".repeat(64) },
};

const CONFIRMED = {
  confirmed: true,
  audit_id: AUDIT_ID,
  brief: "# 经营简报（核验版）\n\n服务端权威简报正文。\n",
  source: VERIFIED.source,
  summary: VERIFIED.summary,
  receipt: {
    seq: 7,
    ts: "2026-11-19T01:02:03Z",
    hash: "b".repeat(64),
    prev: "e".repeat(64),
    brief_sha256: "d".repeat(64),
  },
};

const QUICK = {
  draft: "销售额合计：100.00 元\n销量合计：2 件\n订单数：1 笔",
  model: "deepseek-chat",
  thinking: false,
  mode: "direct_no_tools" as const,
  elapsed_ms: 1234,
  rows_sent: 2,
  masked: true,
  masked_cells: 3,
  dropped: 1,
  disclaimer: "模型未调用任何工具，草稿尚未核验。",
  trace_sha256: "c".repeat(64),
  raw_output_sha256: "1".repeat(64),
  parsed_draft_sha256: "2".repeat(64),
};

afterEach(() => {
  apiMock.mockReset();
});

function fillDraft(value = "订单数：1 笔") {
  fireEvent.change(screen.getByLabelText("简报草稿"), { target: { value } });
}

async function runAudit() {
  fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));
  return screen.findByText("已核验 1");
}

describe("SalesAudit", () => {
  it("提交草稿并展示核验结果", async () => {
    apiMock.mockResolvedValue(VERIFIED);
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    await runAudit();

    expect(apiMock).toHaveBeenCalledWith(
      "/api/sessions/s1/sales-audit",
      expect.objectContaining({ method: "POST" }),
    );
    const body = JSON.parse(apiMock.mock.calls[0][1].body as string) as Record<string, unknown>;
    expect(body).toEqual({ draft: "订单数：1 笔", confirm_full_table: false });
  });

  it("核验失败时展示错误而不崩溃", async () => {
    apiMock.mockRejectedValue(new Error("CSV 必须各有一列 order_id、sales、quantity。"));
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("order_id");
  });

  it("超长草稿禁用提交并标记 aria-invalid", () => {
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);
    const textarea = screen.getByLabelText("简报草稿");
    fireEvent.change(textarea, { target: { value: "x".repeat(6001) } });

    expect(textarea).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: /复算并核验/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /让模型直接起草/ })).toBeDisabled();
  });

  it("范围未确认时二次请求带 confirm_full_table", async () => {
    apiMock
      .mockResolvedValueOnce({ ...VERIFIED, needs_scope_confirmation: true, summary: { verified: 0, conflict: 0, unsupported: 1 } })
      .mockResolvedValueOnce(VERIFIED);
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft("## 标题\n订单数：1 笔");
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));
    fireEvent.click(
      await screen.findByRole("button", { name: "确认指标范围为全表并复算" }),
    );

    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(apiMock.mock.calls[1][1].body as string) as Record<string, unknown>;
    expect(body.confirm_full_table).toBe(true);
  });

  it("确认时调用服务端确认接口，展示回执并采用服务端简报", async () => {
    apiMock.mockResolvedValueOnce(VERIFIED).mockResolvedValueOnce(CONFIRMED);
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    await runAudit();
    fireEvent.click(screen.getByRole("button", { name: /生成核验版简报/ }));

    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/sessions/s1/sales-audit/confirm",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const body = JSON.parse(apiMock.mock.calls[1][1].body as string) as Record<string, unknown>;
    expect(body).toEqual({
      draft: "订单数：1 笔",
      audit_id: AUDIT_ID,
      confirm_full_table: false,
    });

    expect(await screen.findByText("账本已记录")).toBeInTheDocument();
    expect(screen.getByText("#7")).toBeInTheDocument();
    expect(screen.getByText("b".repeat(16))).toBeInTheDocument();
    expect(screen.getByText(/服务端权威简报正文/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /下载核验版简报 \(\.md\)/ })).toBeInTheDocument();
  });

  it("确认返回 409 时清空结果、提示重新核验并把焦点放回草稿框", async () => {
    apiMock
      .mockResolvedValueOnce(VERIFIED)
      .mockRejectedValueOnce(new ApiError("草稿或数据在核验后发生变化，请重新核验再确认。", 409));
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    await runAudit();
    fireEvent.click(screen.getByRole("button", { name: /生成核验版简报/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "草稿或数据在核验后发生变化，请重新复算核验",
    );
    await waitFor(() => expect(screen.queryByText("已核验 1")).not.toBeInTheDocument());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByLabelText("简报草稿")),
    );
  });

  it("快速起草把返回草稿填入文本框，且不自动触发核验", async () => {
    apiMock.mockImplementation((url: string) =>
      url.endsWith("/sales-audit/quick-draft")
        ? Promise.resolve(QUICK)
        : Promise.reject(new Error(`unexpected ${url}`)),
    );
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.click(screen.getByRole("button", { name: /让模型直接起草（不调工具）/ }));

    await waitFor(() =>
      expect(screen.getByLabelText("简报草稿")).toHaveValue(QUICK.draft),
    );
    expect(await screen.findByText("模型原始输出，尚未核验。")).toBeInTheDocument();
    expect(screen.getByText(/deepseek-chat/)).toHaveTextContent("1.2 秒");
    expect(screen.getByText(/deepseek-chat/)).toHaveTextContent("已脱敏（3 格）");
    expect(screen.getByText(/deepseek-chat/)).toHaveTextContent("丢弃 1 行");
    // 只填草稿，不自动发起核验请求
    expect(apiMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("已核验 1")).not.toBeInTheDocument();
  });

  it("快速起草后核验请求携带轨迹哈希，并在结果区展示绑定", async () => {
    apiMock.mockImplementation((url: string) => {
      if (url.endsWith("/sales-audit/quick-draft")) return Promise.resolve(QUICK);
      if (url.endsWith("/sales-audit")) {
        return Promise.resolve({ ...VERIFIED, draft_trace_sha256: QUICK.trace_sha256 });
      }
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.click(screen.getByRole("button", { name: /让模型直接起草（不调工具）/ }));
    await waitFor(() => expect(screen.getByLabelText("简报草稿")).toHaveValue(QUICK.draft));
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    await screen.findByText("已核验 1");
    expect(apiMock).toHaveBeenCalledTimes(2);
    const body = JSON.parse(apiMock.mock.calls[1][1].body as string) as Record<string, unknown>;
    expect(body.draft_trace_sha256).toBe(QUICK.trace_sha256);
    expect(
      screen.getByText(`已绑定起草轨迹 ${QUICK.trace_sha256.slice(0, 12)}…`),
    ).toBeInTheDocument();
  });

  it("手动编辑草稿后核验请求不再携带轨迹哈希", async () => {
    apiMock.mockImplementation((url: string) => {
      if (url.endsWith("/sales-audit/quick-draft")) return Promise.resolve(QUICK);
      if (url.endsWith("/sales-audit")) return Promise.resolve(VERIFIED);
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.click(screen.getByRole("button", { name: /让模型直接起草（不调工具）/ }));
    await waitFor(() => expect(screen.getByLabelText("简报草稿")).toHaveValue(QUICK.draft));

    fireEvent.change(screen.getByLabelText("简报草稿"), {
      target: { value: `${QUICK.draft}\n市场份额：37%` },
    });
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    await screen.findByText("已核验 1");
    const body = JSON.parse(apiMock.mock.calls[1][1].body as string) as Record<string, unknown>;
    expect(body).not.toHaveProperty("draft_trace_sha256");
  });

  it("核验返回轨迹不一致时展示后端 detail 与处理提示", async () => {
    apiMock.mockRejectedValue(
      new ApiError(
        "草稿在快速起草后被修改过，与轨迹不一致；请重新起草或去掉轨迹绑定后再核验。",
        422,
      ),
    );
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("与轨迹不一致");
    expect(alert).toHaveTextContent("修改过草稿后需要重新起草，或去掉轨迹绑定");
  });

  it("快速起草 429 时显示需要等待的秒数", async () => {
    apiMock.mockRejectedValue(
      new ApiError("同一会话 10 秒内只能起草一次，请 9 秒后再试。", 429, 12),
    );
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.click(screen.getByRole("button", { name: /让模型直接起草（不调工具）/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("12 秒");
  });

  it("展示数据告警并明确订单数口径未改", async () => {
    apiMock.mockResolvedValue({
      ...VERIFIED,
      warnings: [
        {
          code: "near_duplicate_order_id",
          message: "订单号存在近似重复（见核验明细），订单数仍按订单号原样去重。",
          total_groups: 1,
        },
      ],
      claims: [
        {
          ...VERIFIED.claims[0],
          caveat: "订单号存在近似重复，订单数口径按原样去重，请人工复核。",
        },
      ],
    });
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    await runAudit();

    expect(screen.getByText(/只告警，订单数口径未改/)).toBeInTheDocument();
    expect(
      screen.getByText("订单号存在近似重复（见核验明细），订单数仍按订单号原样去重。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("订单号存在近似重复，订单数口径按原样去重，请人工复核。"),
    ).toBeInTheDocument();
    expect(screen.getByText("已写入审计账本 #3")).toBeInTheDocument();
  });

  it("核验返回 404 时展示恢复面板，未传回调时给出切换说明", async () => {
    apiMock.mockImplementation((url: string) =>
      url === "/api/sessions/sample?audit=true"
        ? Promise.resolve({ id: "s-new" })
        : Promise.reject(new ApiError("会话不存在或已过期", 404)),
    );
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fillDraft();
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    expect(await screen.findByText("会话已失效")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新载入示例" }));

    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/api/sessions/sample?audit=true", { method: "POST" }),
    );
    expect(await screen.findByText(/s-new/)).toHaveTextContent("请刷新页面后继续");
  });

  it("存在 onReloadSession 时把新会话 id 交给上层", async () => {
    const onReloadSession = vi.fn();
    apiMock.mockImplementation((url: string) =>
      url === "/api/sessions/sample?audit=true"
        ? Promise.resolve({ id: "s-new" })
        : Promise.reject(new ApiError("会话不存在或已过期", 404)),
    );
    render(
      <SalesAudit
        sessionId="s1"
        filename="sales.csv"
        initialDemo={false}
        onReloadSession={onReloadSession}
      />,
    );

    fillDraft();
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));
    await screen.findByText("会话已失效");
    fireEvent.click(screen.getByRole("button", { name: "重新载入示例" }));

    await waitFor(() => expect(onReloadSession).toHaveBeenCalledWith("s-new"));
  });
});
