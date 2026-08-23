/// <reference types="vitest/globals" />
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";

import ArtifactCenter from "../ArtifactCenter";
import type { Artifact } from "../../types";

function makeArtifacts(): Artifact[] {
  return [
    {
      name: "柱状图_1.html",
      kind: "visualization",
      path: "/runs/x/artifacts/柱状图_1.html",
      description: "区域销售对比",
      size_bytes: 20_000,
      engine: "echarts",
    },
    {
      name: "散点图_1.html",
      kind: "visualization",
      path: "/runs/x/artifacts/散点图_1.html",
      description: "销量与利润",
      size_bytes: 15_000,
      engine: "plotly",
      thumbnail_url: "/api/sessions/x/artifacts/散点图_1.html/thumbnail",
    },
    {
      name: "cleaned_data.csv",
      kind: "dataset",
      path: "/runs/x/artifacts/cleaned_data.csv",
      description: "清洗后的数据集",
      size_bytes: 1_024,
    },
  ];
}

describe("ArtifactCenter", () => {
  it("renders charts and data files with engine badges", () => {
    render(<ArtifactCenter artifacts={makeArtifacts()} onDownload={vi.fn()} onPreview={vi.fn()} />);

    expect(screen.getByText("区域销售对比")).toBeTruthy();
    expect(screen.getByText("销量与利润")).toBeTruthy();
    expect(screen.getByText(/清洗后的数据集/)).toBeTruthy();
    // 引擎徽章（筛选按钮也含 ECharts/Plotly 文本，徽章用 class 精确断言）
    expect(document.querySelectorAll(".engine-badge").length).toBe(2);
  });

  it("shows empty state when there are no artifacts", () => {
    render(<ArtifactCenter artifacts={[]} onDownload={vi.fn()} onPreview={vi.fn()} />);
    expect(screen.getByText(/分析完成后/)).toBeTruthy();
  });

  it("renders interactive Plotly mini preview when plotly-json is available", () => {
    const artifacts = makeArtifacts();
    artifacts[1] = {
      ...artifacts[1],
      preview_url: "/api/sessions/x/artifacts/散点图_1.html/preview",
    };
    const { container } = render(
      <ArtifactCenter artifacts={artifacts} onDownload={vi.fn()} onPreview={vi.fn()} />,
    );
    // Plotly 卡片从静态 PNG 升级为 plotly.js 交互迷你图（悬停可读数）
    expect(container.querySelector(".plotly-thumb")).toBeTruthy();
  });

  it("filters charts by engine with aria-pressed state", () => {
    render(<ArtifactCenter artifacts={makeArtifacts()} onDownload={vi.fn()} onPreview={vi.fn()} />);

    const echartsFilter = screen.getByRole("button", { name: "ECharts" });
    const plotlyFilter = screen.getByRole("button", { name: "Plotly" });

    // 初始：全部选中
    expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true");
    expect(echartsFilter).toHaveAttribute("aria-pressed", "false");

    // 点击 ECharts：只保留 ECharts 图，状态切换
    fireEvent.click(echartsFilter);
    expect(echartsFilter).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("销量与利润")).toBeNull();
    expect(screen.getByText("区域销售对比")).toBeTruthy();

    // 切回全部
    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(screen.getByText("销量与利润")).toBeTruthy();
  });

  it("hides engine filter when no chart of that engine exists", () => {
    const onlyEcharts = makeArtifacts().filter((a) => a.engine !== "plotly");
    render(<ArtifactCenter artifacts={onlyEcharts} onDownload={vi.fn()} onPreview={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Plotly" })).toBeNull();
    expect(screen.getByRole("button", { name: "ECharts" })).toBeTruthy();
  });

  it("sorts charts by name and by size", () => {
    render(<ArtifactCenter artifacts={makeArtifacts()} onDownload={vi.fn()} onPreview={vi.fn()} />);

    const select = screen.getByRole("combobox", { name: "图表排序方式" });
    const cards = () => within(screen.getByText("交互图表").closest("section")!).getAllByRole("button")
      .map((b) => b.textContent);

    // 默认顺序：区域销售对比在前
    fireEvent.change(select, { target: { value: "name" } });
    const chartSection = screen.getByText("交互图表").closest("section")!;
    const titles = Array.from(chartSection.querySelectorAll(".chart-card-text strong"))
      .map((el) => el.textContent);
    expect(titles).toEqual(["销量与利润", "区域销售对比"]); // 按名称升序

    fireEvent.change(select, { target: { value: "size" } });
    const titlesBySize = Array.from(chartSection.querySelectorAll(".chart-card-text strong"))
      .map((el) => el.textContent);
    expect(titlesBySize).toEqual(["区域销售对比", "销量与利润"]); // 按大小降序
  });

  it("opens preview when thumbnail is activated with Enter key", () => {
    const onPreview = vi.fn();
    render(<ArtifactCenter artifacts={makeArtifacts()} onDownload={vi.fn()} onPreview={onPreview} />);

    // Plotly 图有缩略图：模拟键盘 Enter 激活
    const thumbnail = screen.getByRole("button", { name: /预览 销量与利润/ });
    fireEvent.keyDown(thumbnail, { key: "Enter" });
    expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ name: "散点图_1.html" }));

    // Space 键同样触发
    const iconThumb = screen.getByRole("button", { name: /预览 区域销售对比/ });
    fireEvent.keyDown(iconThumb, { key: " " });
    expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ name: "柱状图_1.html" }));
  });

  it("supports batch selection and download", () => {
    const onBatchDownload = vi.fn();
    render(
      <ArtifactCenter
        artifacts={makeArtifacts()}
        onDownload={vi.fn()}
        onPreview={vi.fn()}
        onBatchDownload={onBatchDownload}
      />,
    );

    // 勾选两张图
    fireEvent.click(screen.getByRole("checkbox", { name: /选中 区域销售对比/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /选中 销量与利润/ }));

    const downloadButton = screen.getByRole("button", { name: /下载选中/ });
    expect(downloadButton.textContent).toContain("2");
    fireEvent.click(downloadButton);
    expect(onBatchDownload).toHaveBeenCalledTimes(1);
    const items = onBatchDownload.mock.calls[0][0] as Artifact[];
    expect(items.map((i) => i.name)).toEqual(["柱状图_1.html", "散点图_1.html"]);
  });
});
