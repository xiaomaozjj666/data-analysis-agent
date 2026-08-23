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

describe("PlotlyThumb.simplifyPlotlyForThumb", () => {
  it("strips title/legend/axis-titles keeps traces and themes the canvas", () => {
    const { data, layout } = simplifyPlotlyForThumb(figureResponse, false);
    expect(data).toHaveLength(1);
    expect((data[0] as { type: string }).type).toBe("scatter");
    expect(layout.title).toBeUndefined();
    expect(layout.showlegend).toBe(false);
    expect(layout.paper_bgcolor).toBe("#fbfaf5");
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
