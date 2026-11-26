import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DatasetOverview from "../DatasetOverview";

// CountUp 依赖 IntersectionObserver（jsdom 未实现），桩成立即可见。
beforeEach(() => {
  class IntersectionObserverStub {
    constructor(private callback: IntersectionObserverCallback) {}
    observe(target: Element) {
      this.callback(
        [{ isIntersecting: true, target } as unknown as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", IntersectionObserverStub);
});

describe("DatasetOverview", () => {
  it("无字段信息时展示空态并把完整度置 0", () => {
    render(<DatasetOverview />);
    expect(screen.getByText("暂无字段信息")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  });

  it("数值列展示 min/max/均值，整数与浮点分别格式化", () => {
    const { container } = render(
      <DatasetOverview
        profile={{
          row_count: 10,
          column_count: 1,
          column_info: [
            { name: "sales", dtype: "int64", non_null: 9, unique: 5, missing: 1, min: 1, max: 2000, mean: 42.333 },
          ],
        }}
      />,
    );
    expect(screen.getByText("sales")).toBeInTheDocument();
    expect(container.textContent).toContain("min 1");
    expect(container.textContent).toContain("max 2,000");
    expect(container.textContent).toContain("均值 42.33");
    // 9/10 非空 → 完整度 90%
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "90");
    // 缺失率 10% 属于预警区间
    expect(container.querySelector(".quality-dot.is-warn")).toBeTruthy();
  });

  it("分类列展示前三个高频值，缺失超过 30% 标红", () => {
    const { container } = render(
      <DatasetOverview
        profile={{
          row_count: 10,
          column_count: 2,
          column_info: [
            {
              name: "region",
              dtype: "object",
              non_null: 4,
              unique: 3,
              top_values: [
                { value: "East", count: 3 },
                { value: "West", count: 2 },
                { value: "North", count: 1 },
                { value: "South", count: 1 },
              ],
            },
            { name: "note", dtype: "object", non_null: 10, unique: 0 },
          ],
        }}
      />,
    );
    expect(container.textContent).toContain("East");
    expect(container.textContent).toContain("West");
    expect(container.textContent).toContain("North");
    // 只展示前三个高频值
    expect(container.textContent).not.toContain("South");
    expect(container.querySelector(".quality-dot.is-bad")).toBeTruthy();
    // 无统计信息且非数值 → 占位破折号
    expect(container.textContent).toContain("—");
  });

  it("忽略非数字统计字段与非法 top_values", () => {
    const { container } = render(
      <DatasetOverview
        profile={{
          row_count: 0,
          column_count: 1,
          column_info: [
            {
              name: "weird",
              dtype: "object",
              non_null: 0,
              unique: 0,
              min: "not-a-number",
              top_values: ["bad", null],
            },
          ],
        }}
      />,
    );
    expect(container.textContent).toContain("weird");
    // row_count 为 0 时缺失率与完整度都按 0/1 处理，不抛除零错误
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  });

  it("缺失率落在 0~5% 之间时标绿", () => {
    const { container } = render(
      <DatasetOverview
        profile={{
          row_count: 100,
          column_count: 1,
          column_info: [{ name: "ok", dtype: "float64", non_null: 98, unique: 90, missing: 2 }],
        }}
      />,
    );
    expect(container.querySelector(".quality-dot.is-good")).toBeTruthy();
  });
});
