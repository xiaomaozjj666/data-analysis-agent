/// <reference types="vitest/globals" />
import "@testing-library/jest-dom";
import { render, waitFor } from "@testing-library/react";

import PlotlyThumb, { simplifyPlotlyForThumb } from "../PlotlyThumb";

// jsdom 没有 ResizeObserver（与 DataTable 测试同样的处理）
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;

const plotlyModule = {
  newPlot: vi.fn(async () => undefined),
  react: vi.fn(async () => undefined),
  purge: vi.fn(),
  Plots: { resize: vi.fn() },
};
vi.mock("plotly.js-dist-min", () => ({ default: plotlyModule }));

const figureResponse = {
  data: [
    {
      type: "scatter",
      x: [1, 2, 3],
      y: [2, 4, 6],
      name: "华东",
      marker: { color: "#4E79A7" },
    },
  ],
  layout: {
    title: { text: "销量与利润关系" },
    xaxis: { title: { text: "销量" } },
    margin: { l: 64, r: 36, t: 108, b: 120 },
  },
};

// 档位色标（后端 colorscale_from_bands 的硬边界形式：每档一段纯色）
const DENSITY_BAND_COLORS_LIGHT = ["#DCE8F2", "#9BBEDC", "#4E79A7", "#1E4468"];
const DENSITY_BAND_COLORS_DARK = ["#2C3B4D", "#3D6E8E", "#5AABC1", "#B9E0AE"];

function bandScale(colors: string[]): Array<[number, string]> {
  const scale: Array<[number, string]> = [];
  colors.forEach((color, index) => {
    scale.push([index / colors.length, color] as [number, string]);
    scale.push([(index + 1) / colors.length, color] as [number, string]);
  });
  return scale;
}

// 密度视图 figure（data_agent/tools/density_plotly.py）：分面热力图 + 边缘
// 直方图 + 最外围原始点，标注与 shapes 是面板标题/峰值框/均值参考线。
function densityFigureResponse() {
  return {
    data: [
      {
        type: "heatmap",
        x: [0.5, 1.5],
        y: [0.5, 1.5],
        z: [[1, -1], [2, 0]],
        zmin: -0.5,
        zmax: 3.5,
        showscale: true,
        customdata: [[[0, 1, 0, 1, 12, 4], [0, 1, 0, 1, 0, 0]]],
        hovertemplate: "销量 %{customdata[0]} ~ %{customdata[1]}<br>记录数 %{customdata[4]:,.0f}",
        colorscale: bandScale(DENSITY_BAND_COLORS_LIGHT),
        colorbar: { title: { text: "记录数/格" }, thickness: 13, tickvals: [0, 1, 2, 3] },
        hoverongaps: false,
        name: "华东",
      },
      {
        type: "heatmap",
        x: [0.5, 1.5],
        y: [0.5, 1.5],
        z: [[0, 1], [1, 1]],
        zmin: -0.5,
        zmax: 3.5,
        showscale: false,
        colorbar: { title: { text: "记录数/格" }, thickness: 13 },
        hoverongaps: false,
        xaxis: "x2",
        yaxis: "y2",
        name: "华南",
      },
      { type: "scattergl", x: [1], y: [2], mode: "markers", name: "最外围记录" },
      { type: "bar", x: [0.5, 1.5], y: [3, 4], name: "x 分布" },
    ],
    layout: {
      title: { text: "销量与利润（密度）" },
      meta: {
        density_view: {
          bins: "154×80",
          panels: 4,
          bands: ["1", "2–4", "5–9", "≥10"],
          total: 300000,
          colorscales: {
            light: bandScale(DENSITY_BAND_COLORS_LIGHT),
            dark: bandScale(DENSITY_BAND_COLORS_DARK),
          },
        },
      },
      annotations: [
        { text: "华东 · 12,000 条（40%）· r=0.62", x: 0.1, y: 0.9 },
        { text: "最密 812 条", x: 1, y: 2 },
        { text: "x 均值 1,200", x: 0.5, y: 0 },
      ],
      shapes: [
        { type: "rect", x0: 0, x1: 1, y0: 0, y1: 1 },
        { type: "line", x0: 0, y0: 0, x1: 1, y1: 1 },
      ],
      xaxis: { title: { text: "销量" } },
      yaxis: { title: { text: "利润" }, showticklabels: true },
      xaxis2: { title: { text: "销量" }, matches: "x" },
      yaxis2: { title: { text: "利润" }, matches: "y" },
      margin: { l: 64, r: 128, t: 108, b: 96 },
    },
  };
}

function heatmapTraces(data: unknown[]): Array<Record<string, unknown>> {
  return (data as Array<Record<string, unknown>>).filter((trace) => trace.type === "heatmap");
}

describe("PlotlyThumb.simplifyPlotlyForThumb", () => {
  it("strips title/legend/axis-titles keeps traces and themes the canvas", () => {
    const { data, layout } = simplifyPlotlyForThumb(figureResponse, false);
    expect(data).toHaveLength(1);
    expect((data[0] as { type: string }).type).toBe("scatter");
    expect(layout.title).toBeUndefined();
    expect(layout.showlegend).toBe(false);
    expect(layout.paper_bgcolor).toBe("#fbfaf5");
    // 迷你卡不做拖拽缩放（点击卡片是打开完整交互图）
    expect(layout.dragmode).toBe(false);
    expect(layout.hoverlabel).toEqual(
      expect.objectContaining({ bgcolor: "#102a2a" }),
    );
    expect((layout.xaxis as Record<string, unknown>).title).toBeUndefined();
    expect((layout.margin as { t: number }).t).toBeLessThan(20); // 大图边距被压缩
  });

  it("uses dark theme colors when isDark", () => {
    const { layout } = simplifyPlotlyForThumb(figureResponse, true);
    expect(layout.paper_bgcolor).toBe("#1c2433");
    expect((layout.font as { color: string }).color).toBe("#c9cfd9");
    expect((layout.hoverlabel as { bgcolor: string }).bgcolor).toBe("#10151f");
  });

  it("简化密度图：去标注与色标，保留热量网格与档位色标（浅色主题）", () => {
    const figure = densityFigureResponse();
    const { data, layout } = simplifyPlotlyForThumb(figure, false);
    const traces = data as Array<Record<string, unknown>>;

    // 面板标题 / "最密 N 条" / 均值参考线（annotations）、峰值框与趋势线（shapes）
    expect(layout.annotations).toBeUndefined();
    expect(layout.shapes).toBeUndefined();
    // 色标（colorbar）在 209×131 里挤掉近三分之一宽度：每条 trace 都关掉
    for (const trace of traces) {
      expect(trace.showscale).toBe(false);
      expect(trace.colorbar).toBeUndefined();
    }
    // 热量网格本体保留：z / zmin / zmax / 分箱 customdata / hover 文案 / hoverongaps
    const heatmaps = heatmapTraces(data);
    expect(heatmaps).toHaveLength(2);
    expect(heatmaps[0].z).toEqual(figure.data[0].z);
    expect(heatmaps[0].zmin).toBe(-0.5);
    expect(heatmaps[0].zmax).toBe(3.5);
    expect(heatmaps[0].customdata).toEqual(figure.data[0].customdata);
    expect(String(heatmaps[0].hovertemplate)).toContain("记录数");
    expect(heatmaps[1].hoverongaps).toBe(false);
    // 档位色标换成当前主题的一套（浅色主题 → colorscales.light）
    const scales = figure.layout.meta.density_view.colorscales;
    expect(heatmaps[0].colorscale).toEqual(scales.light);
    expect(heatmaps[1].colorscale).toEqual(scales.light);
    // 轴：刻度文字/刻度线/网格线/轴名全隐（分面与边缘分布的轴同样处理）
    for (const key of ["xaxis", "yaxis", "xaxis2", "yaxis2"]) {
      const axis = layout[key] as Record<string, unknown>;
      expect(axis.showticklabels).toBe(false);
      expect(axis.ticks).toBe("");
      expect(axis.showgrid).toBe(false);
      expect(axis.title).toBeUndefined();
    }
    expect(layout.showlegend).toBe(false);
    expect((layout.margin as { t: number }).t).toBeLessThan(10);
    // 原 figure 不被就地改写：卡片按主题重渲染时复用同一份缓存的 figure
    expect((figure.data[0] as Record<string, unknown>).colorbar).toBeTruthy();
    expect(figure.layout.annotations).toHaveLength(3);
  });

  it("深色主题下换成暗色档位色标", () => {
    const figure = densityFigureResponse();
    const { data } = simplifyPlotlyForThumb(figure, true);
    const scales = figure.layout.meta.density_view.colorscales;
    const heatmaps = heatmapTraces(data);
    expect(heatmaps).toHaveLength(2);
    for (const trace of heatmaps) {
      expect(trace.colorscale).toEqual(scales.dark);
    }
    expect(heatmaps[0].colorscale).not.toEqual(scales.light);
  });

  it("普通热力图（没有 meta.density_view）保持现有行为", () => {
    const correlation = {
      data: [
        {
          type: "heatmap",
          z: [[1, 0.2], [0.2, 1]],
          colorscale: [[0, "#2166AC"], [1, "#B2182B"]],
          showscale: true,
          colorbar: { thickness: 13, ticktext: ["-1", "1"] },
        },
      ],
      layout: {
        annotations: [{ text: "强正相关：销量 × 利润" }],
        xaxis: { title: { text: "销量" } },
      },
    };
    const { data, layout } = simplifyPlotlyForThumb(correlation, false);
    const trace = data[0] as Record<string, unknown>;
    // 密度精简不生效：色标、颜色映射与标注全部原样保留
    expect(trace.showscale).toBe(true);
    expect(trace.colorbar).toEqual({ thickness: 13, ticktext: ["-1", "1"] });
    expect(trace.colorscale).toEqual([[0, "#2166AC"], [1, "#B2182B"]]);
    expect(layout.annotations).toHaveLength(1);
    // 通用精简不变：只去轴名与标题，刻度文字照旧
    expect((layout.xaxis as Record<string, unknown>).title).toBeUndefined();
    expect((layout.xaxis as Record<string, unknown>).showticklabels).toBeUndefined();
  });

  it("密度元数据缺失或损坏时不抛错：能认出的仍精简，认不出的退回通用行为", () => {
    // meta.density_view 不是对象 → 当作普通图（标注保留、色标不动）
    const stringMeta = {
      data: [{ type: "heatmap", z: [[1]], showscale: true, colorbar: { thickness: 13 } }],
      layout: { meta: { density_view: "154×80" }, annotations: [{ text: "最密 812 条" }] },
    };
    const fallback = simplifyPlotlyForThumb(stringMeta, false);
    expect(fallback.layout.annotations).toHaveLength(1);
    expect((fallback.data[0] as Record<string, unknown>).showscale).toBe(true);

    // density_view 在但色标损坏：仍按密度图精简，只是不换色标（不写坏数据）
    const brokenScales = {
      data: [{ type: "heatmap", z: [[1]], showscale: true, colorscale: "not-a-scale" }],
      layout: {
        meta: { density_view: { colorscales: { light: [[0, "#fff"]], dark: "nope" } } },
        annotations: [{ text: "最密 812 条" }],
        xaxis: { title: { text: "销量" } },
      },
    };
    const density = simplifyPlotlyForThumb(brokenScales, true);
    expect(density.layout.annotations).toBeUndefined();
    const trace = density.data[0] as Record<string, unknown>;
    expect(trace.showscale).toBe(false);
    expect(trace.colorbar).toBeUndefined();
    expect(trace.colorscale).toBe("not-a-scale");

    // 空 meta、非数组 data、空 density_view 都不能抛
    expect(() => simplifyPlotlyForThumb({ layout: { meta: null } }, true)).not.toThrow();
    expect(() => simplifyPlotlyForThumb({ layout: {} }, false)).not.toThrow();
    expect(() =>
      simplifyPlotlyForThumb(
        { data: "oops" as unknown as unknown[], layout: { meta: { density_view: {} } } },
        false,
      ),
    ).not.toThrow();
  });
});

describe("PlotlyThumb component", () => {
  beforeEach(() => {
    plotlyModule.newPlot.mockClear();
    plotlyModule.react.mockClear();
    plotlyModule.purge.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Promise.resolve({
          ok: true,
          json: async () => figureResponse,
        }),
      ),
    );
    document.documentElement.dataset.theme = "light";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders an interactive mini chart with themed layout", async () => {
    render(
      <PlotlyThumb previewUrl="/api/sessions/x/artifacts/散点图_1.html/preview" alt="销量与利润" />,
    );
    await waitFor(() => expect(plotlyModule.newPlot).toHaveBeenCalledTimes(1));
    const [, data, layout, config] = plotlyModule.newPlot.mock.calls[0] as unknown as [
      HTMLElement,
      unknown[],
      Record<string, unknown>,
      Record<string, unknown>,
    ];
    expect(data).toHaveLength(1);
    expect(layout.showlegend).toBe(false);
    expect(config.displayModeBar).toBe(false);
  });

  it("re-themes via react when data-theme toggles", async () => {
    render(
      <PlotlyThumb previewUrl="/api/sessions/x/artifacts/散点图_1.html/preview" alt="销量与利润" />,
    );
    await waitFor(() => expect(plotlyModule.newPlot).toHaveBeenCalledTimes(1));
    document.documentElement.dataset.theme = "dark";
    await waitFor(() => expect(plotlyModule.react).toHaveBeenCalledTimes(1));
    const [, , layout] = plotlyModule.react.mock.calls[0] as unknown as [
      HTMLElement,
      unknown[],
      Record<string, unknown>,
    ];
    expect(layout.paper_bgcolor).toBe("#1c2433");
  });

  it("密度图切主题后把暗色档位色标真正交给 plotly.react", async () => {
    const figure = densityFigureResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Promise.resolve({ ok: true, json: async () => figure })),
    );
    render(
      <PlotlyThumb previewUrl="/api/sessions/x/artifacts/散点图_1.html/preview" alt="销量与利润（密度）" />,
    );
    await waitFor(() => expect(plotlyModule.newPlot).toHaveBeenCalledTimes(1));
    const [, firstData, firstLayout] = plotlyModule.newPlot.mock.calls[0] as unknown as [
      HTMLElement,
      unknown[],
      Record<string, unknown>,
    ];
    expect(firstLayout.annotations).toBeUndefined();
    for (const trace of heatmapTraces(firstData)) {
      expect(trace.showscale).toBe(false);
    }

    document.documentElement.dataset.theme = "dark";
    await waitFor(() => expect(plotlyModule.react).toHaveBeenCalledTimes(1));
    const [, darkData] = plotlyModule.react.mock.calls[0] as unknown as [HTMLElement, unknown[]];
    for (const trace of heatmapTraces(darkData)) {
      expect(trace.colorscale).toEqual(figure.layout.meta.density_view.colorscales.dark);
    }
  });

  it("falls back to static thumbnail when plotly-json is missing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Promise.resolve({ ok: false, status: 404 })),
    );
    render(
      <PlotlyThumb
        previewUrl="/api/sessions/x/artifacts/散点图_1.html/preview"
        fallbackSrc="/api/sessions/x/artifacts/散点图_1.html/thumbnail"
        alt="销量与利润"
      />,
    );
    // 失败后不渲染 plotly（newPlot 不应被调用），回退组件接管
    await waitFor(() => expect(plotlyModule.newPlot).not.toHaveBeenCalled());
  });
});
