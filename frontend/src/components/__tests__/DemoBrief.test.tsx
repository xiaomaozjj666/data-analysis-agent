import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import DemoBrief from "../DemoBrief";
import type { DemoInsights } from "../../types";

const insights: DemoInsights = {
  kind: "sales_audit",
  headline: "订单只有 20 笔，销量却是 462 件",
  subline: "同一订单的多条明细只算一笔订单。",
  kpis: [
    { label: "订单数", value: "20", note: "订单去重" },
    { label: "销售额", value: "17,224.00", unit: "元" },
  ],
  charts: [
    {
      type: "line",
      title: "销售额趋势",
      points: [
        { label: "2024-01-15", value: 120 },
        { label: "2024-01-18", value: 180 },
      ],
    },
    {
      type: "bar",
      title: "区域对比",
      unit: "元",
      items: [{ label: "华东", value: 1200 }],
      raw_items: [{ label: "华东", value: 99999 }],
      excluded: 1,
    },
    {
      type: "scatter",
      title: "销售额与销量",
      samples: [[1, 2]],
      outliers: [[99, 99]],
    },
    {
      type: "histogram",
      title: "销售额分布",
      unit: "行",
      items: [{ label: "0–10", value: 3 }],
    },
    {
      type: "donut",
      title: "区域占比",
      items: [
        { label: "华东", value: 3 },
        { label: "华南", value: 1 },
      ],
    },
    {
      type: "heatmap",
      title: "密度网格",
      cols: 2,
      rows: 2,
      values: [[1, 0.5], [0.2, 0.1]],
    },
  ],
  findings: ["销量与订单数是两个口径。"],
};

describe("DemoBrief", () => {
  it("renders KPIs, every chart type and findings", () => {
    render(<DemoBrief insights={insights} />);
    expect(screen.getByRole("heading", { name: "订单只有 20 笔，销量却是 462 件" })).toBeInTheDocument();
    expect(screen.getByText("17,224.00")).toBeInTheDocument();
    for (const title of ["销售额趋势", "区域对比", "销售额与销量", "销售额分布", "区域占比", "密度网格"]) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
    expect(screen.getByText("销量与订单数是两个口径。")).toBeInTheDocument();
    for (const label of ["原始文件", "谁能看", "再算一遍", "签名", "打包"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeInTheDocument();
    }
    fireEvent.click(document.querySelector(".demo-bar-row") as HTMLElement);
    expect(screen.getByText(/已选/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保留全量值" }));
    expect(screen.getByText(/99,999/)).toBeInTheDocument();
    const area = document.querySelector(".demo-line-area");
    expect(area?.getAttribute("d")).toMatch(/L \d+\.\d,\d+\.\d L \d+\.\d,\d+\.\d/);
  });

  it("只有 PII 脱敏时「谁能看」步骤仍然可用，并写明字段脱敏", () => {
    const { container } = render(
      <DemoBrief
        insights={{
          ...insights,
          scope: {
            rows: 3000,
            visible: 3000,
            pii_columns: ["姓名", "学号"],
            principal_id: "teacher",
          },
        }}
      />,
    );
    const policy = screen.getByRole("button", { name: /谁能看/ });
    expect(policy).not.toHaveClass("is-off");
    fireEvent.click(policy);
    expect(container.querySelector(".demo-trust-detail")).toHaveTextContent(
      "字段脱敏（PII）",
    );
    expect(container.querySelector(".demo-trust-detail")).not.toHaveTextContent(
      "该演示未启用访问策略",
    );
  });

  it("scope.principal_id 命中 principals 时初始选中该主体", () => {
    render(
      <DemoBrief
        insights={{
          ...insights,
          scope: {
            rows: 3000,
            visible: 3000,
            rls_column: "region",
            principal_id: "u_east",
          },
        }}
        principals={[
          { id: "admin", label: "管理员 · 20 行", policy: {} },
          { id: "u_east", label: "华东主体 · 8 行", policy: {} },
        ]}
      />,
    );
    expect(screen.getByRole("button", { name: "华东主体 · 8 行" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "管理员 · 20 行" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("6 条序列使用 6 种颜色，第 7 条起循环颜色并叠加虚线", () => {
    const series = Array.from({ length: 8 }, (_, index) => ({
      name: `渠道${index + 1}`,
      points: [index + 1, index + 2],
    }));
    const { container } = render(
      <DemoBrief
        insights={{
          ...insights,
          charts: [
            {
              type: "multiline",
              title: "八渠道趋势",
              labels: ["1 月", "2 月"],
              series,
            },
          ],
        }}
      />,
    );
    const lines = Array.from(container.querySelectorAll(".demo-series-line"));
    expect(lines).toHaveLength(8);
    const styleOf = (index: number) => lines[index].getAttribute("style") ?? "";
    const strokeOf = (index: number) =>
      styleOf(index).match(/stroke:\s*([^;]+)/)?.[1]?.trim();
    const colors = lines.slice(0, 6).map((_, index) => strokeOf(index));
    expect(new Set(colors).size).toBe(6);
    expect(styleOf(0)).not.toContain("stroke-dasharray");
    expect(styleOf(6)).toContain("stroke-dasharray");
    expect(strokeOf(6)).toBe(strokeOf(0));
  });

  it("堆叠图点击锁定后移开鼠标仍保持 aria-pressed", () => {
    render(
      <DemoBrief
        insights={{
          ...insights,
          charts: [
            {
              type: "stacked",
              title: "月度堆叠",
              labels: ["1 月", "2 月"],
              series: [
                { name: "线上", values: [1, 2] },
                { name: "线下", values: [2, 1] },
              ],
            },
          ],
        }}
      />,
    );
    const online = screen.getByRole("button", { name: "线上" });
    expect(online).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(online);
    expect(online).toHaveAttribute("aria-pressed", "true");
    fireEvent.mouseLeave(online);
    expect(online).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(online);
    expect(online).toHaveAttribute("aria-pressed", "false");
  });
});
