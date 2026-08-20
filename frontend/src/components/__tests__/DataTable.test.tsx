/// <reference types="vitest/globals" />
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";

import DataTable from "../DataTable";

// jsdom 没有 ResizeObserver，DataTable 的虚拟滚动用它测量容器高度
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;

const rows = [
  { order_id: 1001, region: "华东", sales: 1280.5, quantity: 2, order_date: "2024-01-15T00:00:00" },
  { order_id: 1002, region: "华南", sales: 320, quantity: 10, order_date: "2024-01-18T00:00:00" },
  { order_id: 1003, region: "华北", sales: 890, quantity: null, order_date: "2024-02-03T14:30:00" },
];

describe("DataTable", () => {
  it("right-aligns numeric columns and left-aligns text columns", () => {
    const { container } = render(<DataTable rows={rows} />);
    const salesCells = container.querySelectorAll("td.is-number");
    expect(salesCells.length).toBeGreaterThan(0);
    const headers = container.querySelectorAll("th.is-number");
    expect(headers.length).toBe(3); // order_id + sales + quantity
    // 数字单元格渲染原始数值
    expect(screen.getByText("1280.5")).toBeTruthy();
  });

  it("formats pure dates by stripping the midnight time suffix", () => {
    const { container } = render(<DataTable rows={rows} />);
    // 纯日期（T00:00:00）折叠为日期；带真实时刻的值保持原样
    expect(container.textContent).toContain("2024-01-15");
    expect(container.textContent).not.toContain("2024-01-15T00:00:00");
    expect(container.textContent).toContain("2024-02-03T14:30:00");
  });

  it("renders a persistent sort arrow that activates after sorting", () => {
    const { container } = render(<DataTable rows={rows} />);
    const arrows = container.querySelectorAll(".data-table-arrow");
    expect(arrows.length).toBeGreaterThan(0); // 常驻 DOM
    expect(container.querySelectorAll(".data-table-arrow.is-active").length).toBe(0);

    const regionHeader = screen.getByText("region").closest("th")!;
    fireEvent.click(regionHeader);
    const active = container.querySelector(".data-table-arrow.is-active");
    expect(active).toBeTruthy();
    expect(active!.textContent).toBe("▲");
    // 再点一次切降序
    fireEvent.click(regionHeader);
    expect(container.querySelector(".data-table-arrow.is-active")!.textContent).toBe("▼");
  });

  it("filters rows via the search box", () => {
    render(<DataTable rows={rows} />);
    expect(screen.getByText("共 3 行")).toBeTruthy();
    const search = screen.getByRole("textbox", { name: "过滤数据预览行" });
    fireEvent.change(search, { target: { value: "华南" } });
    expect(screen.getByText("共 1 行")).toBeTruthy();
    expect(screen.getByText("华南")).toBeTruthy();
    expect(screen.queryByText("华北")).toBeNull();
  });

  it("renders nulls as an em dash placeholder", () => {
    render(<DataTable rows={rows} />);
    const emptyCells = document.querySelectorAll(".cell-empty");
    expect(emptyCells.length).toBeGreaterThan(0);
    expect(emptyCells[0].textContent).toBe("—");
  });
});
