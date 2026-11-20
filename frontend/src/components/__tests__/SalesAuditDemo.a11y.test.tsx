import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SalesAudit from "../SalesAudit";

// 演示主流程（初始即路演视图）：草稿工作台 → Ctrl+Enter 复算 →
// 确认并生成简报（焦点落到回执）→ 展开实时哈希链 → 校验简报 → 改动 1 字节。
const apiMock = vi.fn();
vi.mock("../../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../../utils/api")>("../../utils/api");
  return { ...actual, api: (...args: unknown[]) => apiMock(...args) };
});

const AUDIT_ID = `aud-${"a".repeat(32)}`;
const BRIEF = "# 经营简报（核验版）\n\n订单数：8 笔\n销量合计：462 件\n";

async function sha256Hex(text: string): Promise<string> {
  const buffer = await webcrypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text),
  );
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

const VERIFIED = {
  schema_version: 1,
  audit_id: AUDIT_ID,
  source: {
    filename: "sample_sales_audit.csv",
    sha256: "a".repeat(64),
    rows: 21,
    encoding: "utf-8",
    preview: [],
    order_ids: 8,
  },
  metrics: {},
  warnings: [],
  summary: { verified: 1, conflict: 1, unsupported: 1 },
  claims: [
    {
      line: 1,
      text: "订单数：462 笔",
      metric: "订单数",
      status: "conflict",
      reason: "订单数需按 order_id 去重。",
      submitted: "462 笔",
      recalculated: "8 笔",
      formula: "COUNT(DISTINCT order_id)",
    },
    {
      line: 2,
      text: "销量合计：462 件",
      metric: "销量合计",
      status: "verified",
      reason: "原值与明细复算一致。",
      submitted: "462 件",
      recalculated: "462 件",
    },
    {
      line: 3,
      text: "市场份额：37%",
      metric: "市场份额",
      status: "unsupported",
      reason: "原始 CSV 中没有总量数据。",
    },
  ],
  needs_scope_confirmation: false,
  confirmed_full_table: false,
  corrected_brief: BRIEF,
  ledger: { seq: 3, hash: "e".repeat(64), prev: "f".repeat(64) },
};

const CHAIN = {
  ledger: "audit_ledger.jsonl",
  verification: { valid: true, entries: 2, tip: "b".repeat(64), errors: [] },
  entries: [
    {
      seq: 2,
      ts: "2026-11-19T01:02:03Z",
      kind: "confirm",
      prev: "a".repeat(64),
      hash: "b".repeat(64),
    },
    {
      seq: 1,
      ts: "2026-11-19T01:02:02Z",
      kind: "audit",
      prev: "0".repeat(64),
      hash: "a".repeat(64),
    },
  ],
};

beforeEach(() => {
  // jsdom 环境不保证提供 Web Crypto subtle；注入 Node 的 webcrypto，
  // 与 browser / 离线脚本使用同一 SHA-256 语义。
  vi.stubGlobal("crypto", webcrypto);
});

afterEach(() => {
  vi.unstubAllGlobals();
  apiMock.mockReset();
});

describe("有据经营简报演示流程", () => {
  it(
    "草稿 → 键盘复算 → 确认 → 哈希链 → 校验 → 篡改，焦点依次可见",
    async () => {
      const briefSha = await sha256Hex(BRIEF);
      const confirmed = {
        confirmed: true,
        audit_id: AUDIT_ID,
        brief: BRIEF,
        source: VERIFIED.source,
        summary: VERIFIED.summary,
        receipt: {
          seq: 7,
          ts: "2026-11-19T01:02:03Z",
          hash: "b".repeat(64),
          prev: "e".repeat(64),
          brief_sha256: briefSha,
        },
      };
      apiMock.mockImplementation((url: string, options?: { method?: string }) => {
        if (url.endsWith("/sales-audit/ledger?limit=8")) return Promise.resolve(CHAIN);
        if (url.endsWith("/sales-audit/confirm")) return Promise.resolve(confirmed);
        if (url.endsWith("/sales-audit")) return Promise.resolve(VERIFIED);
        return Promise.resolve({});
      });

      render(
        <SalesAudit
          sessionId="s-audit"
          filename="sample_sales_audit.csv"
          initialDemo
        />,
      );

      // 1) 在路演视图展开内联草稿框：焦点自动进入草稿框。
      //    （"草稿工作台"按钮会退出路演进入完整核验页，这里保持在路演流程内。）
      fireEvent.click(screen.getByRole("button", { name: "改一个数试试" }));
      const draft = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "简报草稿" });
      await waitFor(() => expect(draft).toHaveFocus());

      // 2) Ctrl+Enter 直接复算（不使用鼠标提交）。
      fireEvent.keyDown(draft, { key: "Enter", ctrlKey: true });
      const confirmButton = await screen.findByRole("button", { name: "签章并出具简报" });
      expect(apiMock).toHaveBeenCalledWith(
        "/api/sessions/s-audit/sales-audit",
        expect.objectContaining({ method: "POST" }),
      );

      // 3) 确认并生成简报：焦点落到回执区块（role=status，可见焦点环）。
      fireEvent.click(confirmButton);
      const receiptText = await screen.findByText("确认记录");
      const receipt = receiptText.closest("section");
      await waitFor(() => expect(document.activeElement).toBe(receipt));

      // 4) 展开实时哈希链：整链校验结果可见。
      fireEvent.click(screen.getByRole("button", { name: "实时核验哈希链" }));
      expect(await screen.findByText(/整链校验通过 · valid=true/)).toBeInTheDocument();

      // 5) 校验简报：与回执指纹一致。
      fireEvent.click(screen.getByRole("button", { name: "校验简报" }));
      expect(await screen.findByText("内容与回执一致")).toBeInTheDocument();

      // 6) 改动 1 个字节：雪崩差异被检出，状态改为不一致。
      fireEvent.click(screen.getByRole("button", { name: "改动 1 个字节" }));
      expect(
        await screen.findByText(/内容与回执不一致，导出文件已被改动/),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "还原这 1 个字节" })).toBeInTheDocument();
    },
    30000,
  );
});
