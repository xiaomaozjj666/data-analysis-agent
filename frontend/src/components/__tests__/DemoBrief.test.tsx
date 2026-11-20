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
    for (const label of ["原始快照", "权限过滤", "独立复算", "数字签名", "证据包"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeInTheDocument();
    }
    fireEvent.click(document.querySelector(".demo-bar-row") as HTMLElement);
    expect(screen.getByText(/已选/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "包含极端值" }));
    expect(screen.getByText(/99,999/)).toBeInTheDocument();
    const area = document.querySelector(".demo-line-area");
    expect(area?.getAttribute("d")).toMatch(/L \d+\.\d,\d+\.\d L \d+\.\d,\d+\.\d/);
  });
});
