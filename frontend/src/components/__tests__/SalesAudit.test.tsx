import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SalesAudit from "../SalesAudit";

const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

const VERIFIED = {
  schema_version: 1,
  source: { filename: "sales.csv", sha256: "a".repeat(64), rows: 2, preview: [] },
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
};

afterEach(() => {
  apiMock.mockReset();
});

describe("SalesAudit", () => {
  it("提交草稿并展示核验结果", async () => {
    apiMock.mockResolvedValue(VERIFIED);
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.change(screen.getByLabelText("简报草稿"), {
      target: { value: "订单数：1 笔" },
    });
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(
        "/api/sessions/s1/sales-audit",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const body = JSON.parse(apiMock.mock.calls[0][1].body as string) as Record<string, unknown>;
    expect(body).toEqual({ draft: "订单数：1 笔", confirm_full_table: false });
    expect(await screen.findByText("已核验 1")).toBeInTheDocument();
  });

  it("核验失败时展示错误而不崩溃", async () => {
    apiMock.mockRejectedValue(new Error("CSV 必须各有一列 order_id、sales、quantity。"));
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.change(screen.getByLabelText("简报草稿"), { target: { value: "订单数：1 笔" } });
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("order_id");
  });

  it("超长草稿禁用提交并标记 aria-invalid", () => {
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);
    const textarea = screen.getByLabelText("简报草稿");
    fireEvent.change(textarea, { target: { value: "x".repeat(6001) } });

    expect(textarea).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: /复算并核验/ })).toBeDisabled();
  });

  it("范围未确认时二次请求带 confirm_full_table", async () => {
    apiMock
      .mockResolvedValueOnce({ ...VERIFIED, needs_scope_confirmation: true, summary: { verified: 0, conflict: 0, unsupported: 1 } })
      .mockResolvedValueOnce(VERIFIED);
    render(<SalesAudit sessionId="s1" filename="sales.csv" initialDemo={false} />);

    fireEvent.change(screen.getByLabelText("简报草稿"), {
      target: { value: "## 标题\n订单数：1 笔" },
    });
    fireEvent.click(screen.getByRole("button", { name: /复算并核验/ }));
    fireEvent.click(
      await screen.findByRole("button", { name: "确认指标范围为全表并复算" }),
    );

    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(apiMock.mock.calls[1][1].body as string) as Record<string, unknown>;
    expect(body.confirm_full_table).toBe(true);
  });
});
