/// <reference types="vitest/globals" />
import "@testing-library/jest-dom";
import { render, waitFor } from "@testing-library/react";

import EChartThumb, { boostForDark, formatCompact, simplifyForThumb, stripFunctions } from "../EChartThumb";

// jsdom 没有 ResizeObserver（与 PlotlyThumb 测试同样处理）
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;

// echarts 实例化被替换掉：断言"到达渲染层的 option"，而不只是纯函数的返回值
const echartsMock = vi.hoisted(() => {
  const options: Array<Record<string, unknown>> = [];
  const module = {
    init: vi.fn(() => ({
      setOption: (option: Record<string, unknown>) => {
        options.push(option);
      },
      resize: () => {},
      dispose: () => {},
    })),
    getInstanceByDom: vi.fn(() => null),
  };
  return { module, options };
});
vi.mock("echarts", () => echartsMock.module);

const BAND_COLORS_LIGHT = ["#DCE8F2", "#9BBEDC", "#4E79A7", "#1E4468"];
const BAND_COLORS_DARK = ["#2C3B4D", "#3D6E8E", "#5AABC1", "#B9E0AE"];

// 档位说明（与后端 density.band_legend 同构：1-2-5 系列，最高一档开区间）
function bands(colors: string[]) {
  return [
    { label: "1", low: 1, high: 1, color: colors[0] },
    { label: "2–4", low: 2, high: 4, color: colors[1] },
    { label: "5–9", low: 5, high: 9, color: colors[2] },
    { label: "≥10", low: 10, high: null, color: colors[3] },
  ];
}

// 密度视图 option（≥2 万行散点聚合）：顶层 densityBands 带浅/暗两套档位色，
// visualMap 的分档 pieces 是格子颜色的唯一来源，主面板与边缘分布用两套
// grids / 轴（系列靠 gridIndex 引用各自的网格）。
function densityOption(): Record<string, unknown> {
  return {
    densityBands: { light: bands(BAND_COLORS_LIGHT), dark: bands(BAND_COLORS_DARK) },
    title: { text: "销量与利润（密度）" },
    tooltip: { trigger: "item", formatter: "function(p){return p.value;}" },
    visualMap: {
      type: "piecewise",
      show: true,
      left: "center",
      bottom: 12,
      pieces: [
        { min: 1, max: 1, label: "1", color: BAND_COLORS_LIGHT[0] },
        { min: 2, max: 4, label: "2–4", color: BAND_COLORS_LIGHT[1] },
        { min: 5, max: 9, label: "5–9", color: BAND_COLORS_LIGHT[2] },
        { min: 10, label: "≥10", color: BAND_COLORS_LIGHT[3] },
      ],
    },
    grid: [
      { left: "9%", right: "12%", top: "10%", height: "62%" },
      { left: "9%", right: "12%", top: "78%", height: "16%" },
    ],
    xAxis: [
      { gridIndex: 0, type: "value", name: "销量", splitLine: { show: true } },
      { gridIndex: 1, type: "value", name: "销量", axisLabel: { show: true } },
    ],
    yAxis: [
      { gridIndex: 0, type: "value", name: "利润", axisLine: { show: true } },
      { gridIndex: 1, type: "value", axisTick: { show: true } },
    ],
    series: [
      { type: "heatmap", name: "销量 × 利润", data: [[0, 0, 2], [1, 1, 3]] },
      { type: "bar", name: "x 分布", data: [12, 30] },
    ],
  };
}

function pieceColors(option: Record<string, unknown>): string[] {
  const pieces = (option.visualMap as { pieces: Array<{ color: string }> }).pieces;
  return pieces.map((piece) => piece.color);
}

// 真实 ECharts 密度产物的锚点注记形态（取自 api_bigtest0001 的散点图_3 产物）：
// 峰值不是 markPoint，而是热力图里的**对象型数据项**（红框 itemStyle +
// "最密 N 条" label，per-item label 会盖过 series 级 label.show=false）；
// 趋势线是 silent 的 line 系列（每面板一条，插在热力图之后）；顶层 graphic
// 作为兜底形态；visualMap 用 seriesIndex 按**下标**绑定热力图系列。
function densityOverlayOption(): Record<string, unknown> {
  const option = densityOption();
  const heatmap = (name: string, peakCount: number, peakText: string) => ({
    type: "heatmap",
    name,
    itemStyle: { borderWidth: 0 },
    label: { show: false },
    emphasis: { itemStyle: { borderColor: "#E15759", borderWidth: 1.5 } },
    markPoint: { symbolSize: 42, data: [{ type: "max", name: "峰值" }] },
    markLine: { data: [{ type: "average" }] },
    data: [
      [1, 20, 2, 63.739, 126.888, 19.403, 25.373],
      {
        value: [4, 31, peakCount, 253.186, 316.335, 85.075, 91.045],
        itemStyle: {
          borderColor: "#E15759",
          borderWidth: 1.5,
          shadowBlur: 6,
          shadowColor: "rgba(225,87,89,0.55)",
        },
        label: {
          show: true,
          position: "top",
          color: "#B23A3C",
          backgroundColor: "rgba(255,255,255,0.86)",
          borderColor: "#E15759",
          borderWidth: 1,
          formatter: peakText,
        },
      },
    ],
  });
  const trend = (color: string) => ({
    name: "趋势线",
    type: "line",
    silent: true,
    legendHoverLink: false,
    showSymbol: false,
    z: 6,
    lineStyle: { color, width: 2.5 },
    tooltip: { show: false },
    data: [[1, 2], [3, 4]],
  });
  const extremes = () => ({
    name: "最外围记录",
    type: "scatter",
    symbolSize: 5,
    itemStyle: { color: "rgba(225,87,89,0.85)" },
    data: [[6, 39, 434.6, 138.51]],
  });
  option.series = [
    heatmap("食品", 10104, "最密 10,104 条"),
    trend("#E15759"),
    extremes(),
    heatmap("电子产品", 2761, "最密 2,761 条"),
    trend("#E15759"),
    extremes(),
  ];
  option.visualMap = {
    type: "piecewise",
    dimension: 2,
    // 真实产物就是按下标绑定：抽出趋势线后必须重映射，否则后一个热力图
    // 掉出档位色板、退回全局调色板（橙/红大色块）
    seriesIndex: [0, 3],
    selectedMode: false,
    pieces: [
      { min: 1, max: 4, label: "1", color: BAND_COLORS_LIGHT[0] },
      { min: 5, max: 9, label: "5–9", color: BAND_COLORS_LIGHT[2] },
      { min: 10, label: "≥10", color: BAND_COLORS_LIGHT[3] },
    ],
  };
  option.graphic = [{ type: "text", left: 10, top: 10, style: { text: "最密 10,104 条" } }];
  return option;
}

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

  it("bakes formatted static value labels for bar charts", () => {
    const option = simplifyForThumb(
      {
        series: [
          {
            type: "bar",
            data: [54849.46, 70012.68000000001, 109179.03],
            label: { show: true, position: "top", formatter: "function(p){return p.value;}" },
          },
        ],
      },
      false,
    ) as { series: Array<{ data: Array<{ value: number; label: { formatter: string } }> }> };
    // 迷你图剥离 JS formatter 后，静态格式化文本避免裸浮点
    expect(option.series[0].data.map((d) => d.label.formatter)).toEqual(["5.5万", "7.0万", "10.9万"]);
  });

  it("密度图：整组换档位色、隐图例与轴文字，保留多 grids 几何", () => {
    const option = simplifyForThumb(densityOption(), false);
    const vm = option.visualMap as Record<string, unknown>;
    // 档位图例（分档色块 + 滑条）不再盖在数据上，但 pieces 仍是格子颜色来源
    expect(vm.show).toBe(false);
    expect(pieceColors(option)).toEqual(BAND_COLORS_LIGHT);
    // 分档结构（min/max/label）原样保留：最高一档仍是开区间
    const pieces = vm.pieces as Array<Record<string, unknown>>;
    expect(pieces.map((piece) => piece.min)).toEqual([1, 2, 5, 10]);
    expect(pieces[3].max).toBeUndefined();
    // 轴：轴名/刻度文字/轴线/分隔线全隐（主面板与边缘分布两组轴都处理）
    const axes = [
      ...(option.xAxis as Array<Record<string, unknown>>),
      ...(option.yAxis as Array<Record<string, unknown>>),
    ];
    expect(axes).toHaveLength(4);
    for (const axis of axes) {
      expect(axis.name).toBe("");
      expect((axis.axisLabel as { show: boolean }).show).toBe(false);
      expect((axis.axisLine as { show: boolean }).show).toBe(false);
      expect((axis.splitLine as { show: boolean }).show).toBe(false);
      // 类目轴交替底纹会在零记录格后面冒充数据（密度图的空格必须透明）
      expect((axis.splitArea as { show: boolean }).show).toBe(false);
    }
    // 多 grids 保留原始几何（series 用 gridIndex 引用各自的网格，不能被单个 grid 覆盖）
    const grids = option.grid as Array<Record<string, unknown>>;
    expect(grids).toHaveLength(2);
    expect(grids[0].left).toBe("9%");
    expect(grids[1].top).toBe("78%");
    // 与普通热力图一致：formatter 剥离后 hover 只剩原始数组，不给；标题/图例同样删掉
    expect(option.tooltip).toBeUndefined();
    expect(option.title).toBeUndefined();
    expect(option.legend).toBeUndefined();
  });

  it("密度图暗色主题：pieces 换成 densityBands.dark（分档边界不变）", () => {
    const option = simplifyForThumb(densityOption(), true);
    expect(pieceColors(option)).toEqual(BAND_COLORS_DARK);
    const pieces = (option.visualMap as { pieces: Array<Record<string, unknown>> }).pieces;
    expect(pieces.map((piece) => piece.min)).toEqual([1, 2, 5, 10]);

    // pieces 顺序被调过时按 label 对上档位（而不是错位换色）
    const shuffled = densityOption();
    (shuffled.visualMap as { pieces: Array<Record<string, unknown>> }).pieces.reverse();
    const out = simplifyForThumb(shuffled, true);
    expect(
      (out.visualMap as { pieces: Array<Record<string, unknown>> }).pieces.map((p) => [p.label, p.color]),
    ).toEqual([
      ["≥10", BAND_COLORS_DARK[3]],
      ["5–9", BAND_COLORS_DARK[2]],
      ["2–4", BAND_COLORS_DARK[1]],
      ["1", BAND_COLORS_DARK[0]],
    ]);
  });

  it("密度图去掉峰值红框/最密文字与趋势线，保留热力图数据与最外围记录", () => {
    const option = simplifyForThumb(densityOverlayOption(), false);
    const series = option.series as Array<Record<string, unknown>>;

    // 热力图系列与数据本体保留：普通格仍是原始数组，峰值格只剩 value
    const heatmaps = series.filter((item) => item.type === "heatmap");
    expect(heatmaps).toHaveLength(2);
    const cells = heatmaps[0].data as unknown[];
    expect(cells).toHaveLength(2);
    expect(cells[0]).toEqual([1, 20, 2, 63.739, 126.888, 19.403, 25.373]);
    const peak = cells[1] as Record<string, unknown>;
    expect(peak.value).toEqual([4, 31, 10104, 253.186, 316.335, 85.075, 91.045]);
    // 红框与"最密 N 条"文字（per-item label 会盖过 series 级 label.show=false）
    expect(peak.label).toBeUndefined();
    expect(peak.itemStyle).toBeUndefined();

    // 非交互趋势线（silent line 系列）不再进入迷你图
    expect(series.some((item) => item.type === "line")).toBe(false);
    expect(series.map((item) => item.type)).toEqual(["heatmap", "scatter", "heatmap", "scatter"]);
    // 小而有信息量的"最外围记录"散点保留
    expect(series.filter((item) => item.name === "最外围记录")).toHaveLength(2);
    // 顶层 graphic 注记同样去掉；series 级 markPoint/markLine 兜底清理
    expect(option.graphic).toBeUndefined();
    for (const heatmap of heatmaps) {
      expect(heatmap.markPoint).toBeUndefined();
      expect(heatmap.markLine).toBeUndefined();
    }
    // visualMap 按下标绑定系列：抽出趋势线后必须重映射，否则后一个热力图
    // 掉出档位色板、退回全局调色板（橙/红大色块）
    const vm = option.visualMap as Record<string, unknown>;
    expect(vm.seriesIndex).toEqual([0, 2]);
    expect(vm.selectedMode).toBe(false);
    expect(vm.dimension).toBe(2);
    expect((vm.pieces as unknown[]).length).toBe(3);
  });

  it("普通图表的 markPoint/markLine 保持现状（只缩尺寸、不删除）", () => {
    const option = simplifyForThumb(
      {
        series: [
          {
            type: "line",
            data: [77401.2, 148062.5],
            markPoint: { symbolSize: 42, data: [{ type: "max", name: "峰值" }] },
            markLine: { data: [{ type: "average" }] },
            label: { show: true, formatter: "function(p){return p.value;}" },
          },
        ],
      },
      false,
    );
    const series = (option.series as Array<Record<string, unknown>>)[0];
    const markPoint = series.markPoint as { symbolSize: number; data: Array<Record<string, unknown>> };
    expect(markPoint.symbolSize).toBe(20); // 仍走原有的缩小逻辑
    expect(markPoint.data).toHaveLength(1);
    expect((series.markLine as { data: unknown[] }).data).toHaveLength(1);
  });

  it("密度图的 px 内边距收小，缺 grid 时给紧凑默认值", () => {
    const pixelGrid = simplifyForThumb(
      { ...densityOption(), grid: { left: 100, right: 120, top: 60, bottom: 80, containLabel: true } },
      false,
    );
    expect(pixelGrid.grid).toEqual({ left: 8, right: 8, top: 8, bottom: 8, containLabel: false });

    const noGrid = simplifyForThumb({ ...densityOption(), grid: undefined }, false);
    expect(noGrid.grid).toEqual({ left: 4, right: 4, top: 4, bottom: 4, containLabel: false });
  });

  it("densityBands 缺失或损坏时退回普通图表行为且不抛错", () => {
    const base = {
      tooltip: { trigger: "item", formatter: "function(p){return p.value;}" },
      visualMap: { min: 0, max: 3, inRange: { color: ["#EDF3F9", "#2C5F8D"] } },
      xAxis: [{ type: "value", name: "销量" }],
      yAxis: [{ type: "value", name: "利润" }],
      series: [{ type: "heatmap", data: [[0, 0, 1]] }],
    };
    const malformed: Array<Record<string, unknown>> = [
      { ...base, densityBands: "154×80" },
      { ...base, densityBands: { light: [{ label: "1", color: 3 }] } },
      { ...base, densityBands: { light: [], dark: [] } },
      { ...base, densityBands: null },
    ];
    for (const option of malformed) {
      const out = simplifyForThumb(option, false);
      // 普通热力图行为：visualMap 收敛成 inRange 色板，档位 pieces 不参与
      expect((out.visualMap as { inRange: { color: string[] } }).inRange.color).toEqual(["#EDF3F9", "#2C5F8D"]);
      // 轴只去轴名，刻度文字保留（只有密度分支才隐刻度）
      const axis = (out.xAxis as Array<Record<string, unknown>>)[0];
      expect(axis.name).toBeUndefined();
      expect((axis.axisLabel as { show?: boolean }).show).toBeUndefined();
      expect(out.grid).toEqual({ left: 4, right: 10, top: 14, bottom: 6, containLabel: true });
    }

    // 分档 visualMap 但没有 densityBands（旧产物）：同样走当前行为
    const piecewise = simplifyForThumb(
      { ...base, visualMap: { type: "piecewise", show: true, pieces: [{ min: 1, max: 4, color: "#4E79A7" }] } },
      false,
    );
    const vm = piecewise.visualMap as Record<string, unknown>;
    expect(vm.show).toBe(false);
    expect(vm.pieces).toBeUndefined();
    expect(Array.isArray((vm.inRange as { color: string[] }).color)).toBe(true);
  });
});

describe("EChartThumb.stripFunctions", () => {  it("removes function strings without executing anything", () => {
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

describe("EChartThumb component", () => {
  beforeEach(() => {
    echartsMock.options.length = 0;
    echartsMock.module.init.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Promise.resolve({ ok: true, json: async () => densityOption() })),
    );
    document.documentElement.dataset.theme = "light";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("主题切换后把暗色档位色板真正交给 setOption", async () => {
    render(<EChartThumb previewUrl="/api/sessions/x/artifacts/散点图_1.html/preview" alt="销量与利润" />);
    await waitFor(() => expect(echartsMock.options.length).toBeGreaterThan(0), { timeout: 3000 });
    expect(pieceColors(echartsMock.options[0])).toEqual(BAND_COLORS_LIGHT);

    document.documentElement.dataset.theme = "dark";
    await waitFor(() => expect(echartsMock.options.length).toBeGreaterThan(1), { timeout: 3000 });
    const rendered = echartsMock.options[echartsMock.options.length - 1];
    expect(pieceColors(rendered)).toEqual(BAND_COLORS_DARK);
    // 缩略图里没有标题/图例/档位图例，只剩一块小热力图
    expect(rendered.title).toBeUndefined();
    expect(rendered.legend).toBeUndefined();
    expect((rendered.visualMap as { show: boolean }).show).toBe(false);
  });
});
