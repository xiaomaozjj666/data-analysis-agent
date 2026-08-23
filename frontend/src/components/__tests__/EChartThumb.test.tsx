/// <reference types="vitest/globals" />
import "@testing-library/jest-dom";

import { boostForDark, formatCompact, simplifyForThumb, stripFunctions } from "../EChartThumb";

describe("EChartThumb.simplifyForThumb", () => {
  it("keeps minimal tooltip for line/bar so hover reveals data", () => {
    const option = simplifyForThumb(
      {
        tooltip: { trigger: "axis", formatter: "function(p){return p;}" },
        xAxis: [{ type: "category", data: ["a", "b"], name: "月份" }],
        yAxis: [{ type: "value", min: 0, max: 10, name: "销售额" }],
        series: [{ type: "line", data: [1, 2] }],
      },
      false,
    );
    expect(option.tooltip).toEqual(
      expect.objectContaining({ trigger: "axis", backgroundColor: "#ffffff" }),
    );
    expect((option.xAxis as Array<Record<string, unknown>>)[0].name).toBeUndefined();
  });

  it("keeps compact visualMap for heatmap instead of falling back to a flat color", () => {
    const colors = ["#EDF3F9", "#8FB3D1", "#2C5F8D"];
    const light = simplifyForThumb(
      {
        tooltip: { trigger: "item", formatter: "function(p){return p;}" },
        visualMap: { min: 10, max: 20, calculable: true, inRange: { color: colors } },
        series: [{ type: "heatmap", data: [[0, 0, 1]] }],
      },
      false,
    );
    expect(light.visualMap).toEqual(
      expect.objectContaining({ min: 10, max: 20, show: false }),
    );
    expect((light.visualMap as { inRange: { color: string[] } }).inRange.color).toEqual(colors);
    // 热力图：tooltip formatter 剥离后默认显示原始数组，直接去掉
    expect(light.tooltip).toBeUndefined();

    // 暗色画布：深色端提亮，保证格子与 #1c2433 背景有对比
    const dark = simplifyForThumb(
      {
        visualMap: { min: 10, max: 20, inRange: { color: colors } },
        series: [{ type: "heatmap", data: [[0, 0, 1]] }],
      },
      true,
    );
    const darkColors = (dark.visualMap as { inRange: { color: string[] } }).inRange.color;
    expect(darkColors).not.toEqual(colors);
    expect(darkColors[2]).not.toBe("#2C5F8D");
    expect(/^#[0-9a-f]{6}$/i.test(darkColors[2])).toBe(true);
  });

  it("removes 3D axis names and shrinks tick labels in the mini view", () => {
    const option = simplifyForThumb(
      {
        xAxis3D: { name: "销售额", axisLabel: { fontSize: 10 } },
        yAxis3D: { name: "利润", axisLabel: { fontSize: 10 } },
        zAxis3D: { name: "数量", axisLabel: { fontSize: 10 } },
        series: [{ type: "scatter3D", data: [[1, 2, 3]] }],
      },
      false,
    );
    expect(option.xAxis3D).toEqual(
      expect.objectContaining({ axisLabel: expect.objectContaining({ fontSize: 8 }) }),
    );
    expect((option.xAxis3D as Record<string, unknown>).name).toBeUndefined();
    expect((option.zAxis3D as Record<string, unknown>).name).toBeUndefined();
  });

  it("scales down markPoints and bakes formatted static labels", () => {
    const option = simplifyForThumb(
      {
        series: [
          {
            type: "line",
            data: [77401.2, 148062.5],
            markPoint: {
              symbolSize: 42,
              data: [{ type: "max", name: "峰值" }, { type: "min", name: "谷值" }],
            },
          },
        ],
      },
      false,
    );
    const series = option.series as Array<Record<string, unknown>>;
    const markPoint = series[0].markPoint as { symbolSize: number; data: Array<Record<string, unknown>> };
    // 函数字符串剥离后默认显示裸值（148062），这里换成预设好的格式化文本
    const labels = markPoint.data.map((d) => (d.label as { formatter: string }).formatter);
    expect(labels).toEqual(["14.8万", "7.7万"]);
    expect(markPoint.symbolSize).toBe(20);
    // 大头针区域顶部留白加大，防裁切
    expect((option.grid as { top: number }).top).toBe(24);
  });
});

describe("EChartThumb.stripFunctions", () => {
  it("removes function strings without executing anything", () => {
    const out = stripFunctions({
      tooltip: { formatter: "function(p){return p.value;}" },
      series: [{ type: "bar", data: [1], label: { formatter: "function(v){return v;}" } }],
    }) as Record<string, unknown>;
    // 函数字段剥离后对象保留空壳（无任何可执行内容）
    expect(out.tooltip).toEqual({});
    expect((out.series as Array<Record<string, unknown>>)[0].label).toEqual({});
  });
});

describe("EChartThumb helpers", () => {
  it("formats compact values with 万 unit", () => {
    expect(formatCompact(148062.5)).toBe("14.8万");
    expect(formatCompact(77401.2)).toBe("7.7万");
    expect(formatCompact(999)).toBe("999");
    expect(formatCompact(2913.33)).toBe("2913.33");
  });

  it("boosts dark colors for dark canvases", () => {
    expect(boostForDark("#2C5F8D")).not.toBe("#2C5F8D");
    expect(boostForDark("#EDF3F9")).toBe("#EDF3F9"); // 浅色端不动
    expect(boostForDark("not-a-color")).toBe("not-a-color");
  });
});
