import React, { useEffect, useRef, useState } from "react";
import { pickChartIcon } from "../constants";
import { fetchJsonWithTimeout } from "../utils/api";
import useInView from "../hooks/useInView";
import AuthImage from "./AuthImage";

interface PlotlyThumbProps {
  /** 图表 preview_url，用于推导 plotly-json 地址与点击预览回调。 */
  previewUrl: string;
  /** plotly-json 缺失/损坏时回退的静态 PNG 缩略图地址（可为空）。 */
  fallbackSrc?: string | null;
  alt: string;
}

interface FigureShape {
  data?: unknown[];
  layout?: Record<string, unknown>;
}

interface PlotlyThemeColors {
  paper: string;
  font: string;
  grid: string;
  zeroLine: string;
  hoverBg: string;
  hoverFont: string;
}

export function plotlyThemeColors(isDark: boolean): PlotlyThemeColors {
  return isDark
    ? {
        paper: "#1c2433",
        font: "#c9cfd9",
        grid: "#39435c",
        zeroLine: "#4a4b50",
        hoverBg: "#10151f",
        hoverFont: "#f3f5f9",
      }
    : {
        paper: "#fbfaf5",
        font: "#102a2a",
        grid: "#e5ece9",
        zeroLine: "#c8d2cf",
        hoverBg: "#102a2a",
        hoverFont: "#ffffff",
      };
}

const PLOTLY_CONFIG = {
  displayModeBar: false,
  responsive: true,
  scrollZoom: false,
  displaylogo: false,
  locale: "zh-CN",
};

// 全尺寸 figure 直接塞进 209×131 迷你卡：标题/图例/轴名挤占绘图区、
// 文字与缩略图卡片尺寸不匹配。精简：去标题/图例/轴名，画布与文字
// 随主题着色（深色主题下与 ECharts 迷你图保持一致），保留全部 trace
// 数据——plotly.js 原地渲染后鼠标悬停即可查看数据点（与 ECharts
// 卡片体验对齐，替代原来无法交互的静态 PNG）。
export function simplifyPlotlyForThumb(
  figure: FigureShape,
  isDark: boolean,
): { data: unknown[]; layout: Record<string, unknown> } {
  const theme = plotlyThemeColors(isDark);
  const srcLayout = (figure.layout ?? {}) as Record<string, unknown>;
  const layout: Record<string, unknown> = { ...srcLayout };
  delete layout.title;
  layout.showlegend = false;
  layout.autosize = true;
  // 迷你卡不缩放：拖拽框选会触发 plotly 缩放（且 scattergl 缩放后
  // gl 画布背景色需重渲染，卡片内没有该修复链）；卡片点击本来就是
  // 打开完整交互图。dragmode=false 禁用拖拽缩放层。
  layout.dragmode = false;
  layout.margin = { l: 38, r: 6, t: 6, b: 22 };
  layout.paper_bgcolor = theme.paper;
  layout.plot_bgcolor = theme.paper;
  layout.font = {
    ...(srcLayout.font && typeof srcLayout.font === "object" ? (srcLayout.font as Record<string, unknown>) : {}),
    color: theme.font,
    size: 9,
  };
  layout.hoverlabel = {
    bgcolor: theme.hoverBg,
    bordercolor: theme.grid,
    font: { color: theme.hoverFont },
  };
  for (const key of ["xaxis", "yaxis", "zaxis"]) {
    const axis = layout[key];
    if (axis && typeof axis === "object") {
      const copy: Record<string, unknown> = { ...(axis as Record<string, unknown>) };
      delete copy.title;
      copy.gridcolor = theme.grid;
      copy.zerolinecolor = theme.zeroLine;
      copy.tickfont = {
        ...(copy.tickfont && typeof copy.tickfont === "object" ? (copy.tickfont as Record<string, unknown>) : {}),
        size: 9,
        color: theme.font,
      };
      layout[key] = copy;
    }
  }
  // 3D 场景：把 scene.<axis> 的轴名/刻度字号一并缩到迷你尺寸
  const scene = layout.scene;
  if (scene && typeof scene === "object") {
    const sceneCopy: Record<string, unknown> = { ...(scene as Record<string, unknown>) };
    for (const key of ["xaxis", "yaxis", "zaxis"]) {
      const axis = sceneCopy[key];
      if (axis && typeof axis === "object") {
        const copy: Record<string, unknown> = { ...(axis as Record<string, unknown>) };
        delete copy.title;
        copy.tickfont = {
          ...(copy.tickfont && typeof copy.tickfont === "object" ? (copy.tickfont as Record<string, unknown>) : {}),
          size: 8,
          color: theme.font,
        };
        sceneCopy[key] = copy;
      }
    }
    layout.scene = sceneCopy;
  }
  return { data: figure.data ?? [], layout };
}

// Plotly 卡片的内联交互迷你图：卡片的静态 PNG 缩略图（kaleido 渲染）
// 悬停没有任何反应——与 ECharts 卡片体验割裂。此处 fetch 后端
// plotly-json，懒加载 plotly.js（与预览 iframe 同版本 v3.7.0）原地
// 渲染迷你图，悬停即可查看数据点；失败（无数据文件/损坏）时回退到
// 原有 PNG 缩略图，保证任何情况下卡片都有内容。
const PlotlyThumb = React.memo(function PlotlyThumb({ previewUrl, fallbackSrc, alt }: PlotlyThumbProps) {
  // 容器同时承担可见性观测：进入视口才拉取数据并初始化图表，离开视口
  // purge 释放实例（重新进入时用已缓存的 figure 重绘，不再发请求）。
  // 产物网格几十张卡时，常驻实例数从"卡片总数"降到"可见数"。
  const { ref: containerRef, inView } = useInView<HTMLDivElement>("240px");
  const [failed, setFailed] = useState(false);
  const [figure, setFigure] = useState<FigureShape | null>(null);
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme === "dark");
  // 已用 newPlot 初始化过；主题切换时改用 react 原地换肤，不重建 DOM
  const renderedRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!inView || figure) return;
    let disposed = false;
    (async () => {
      try {
        const jsonUrl = previewUrl.replace(/\/preview$/, "/plotly-json");
        const fig = await fetchJsonWithTimeout<FigureShape>(jsonUrl);
        if (!disposed) setFigure(fig);
      } catch {
        if (!disposed) setFailed(true);
      }
    })();
    return () => {
      disposed = true;
    };
  }, [previewUrl, inView, figure]);

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setTheme(document.documentElement.dataset.theme === "dark");
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  // 渲染 / 换肤 / 释放：figure 就绪且在视口内 → newPlot（首次）或
  // react（主题切换，保留悬停状态）；离开视口 → purge 释放实例。
  useEffect(() => {
    if (!figure || failed || !inView) {
      if (!inView && renderedRef.current) {
        renderedRef.current = null;
        void import("plotly.js-dist-min")
          .then((m) => {
            const el = containerRef.current;
            if (el) m.default.purge(el);
          })
          .catch(() => {});
      }
      return;
    }
    let disposed = false;
    (async () => {
      try {
        const Plotly = (await import("plotly.js-dist-min")).default;
        const el = containerRef.current;
        if (disposed || !el) return;
        const { data, layout } = simplifyPlotlyForThumb(figure, theme);
        if (renderedRef.current) {
          // 主题切换：保留数据与交互状态，仅更新主题色
          await Plotly.react(renderedRef.current, data, layout, PLOTLY_CONFIG);
        } else {
          await Plotly.newPlot(el, data, layout, PLOTLY_CONFIG);
          renderedRef.current = el;
        }
      } catch {
        if (!disposed) setFailed(true);
      }
    })();
    return () => {
      disposed = true;
    };
  }, [figure, theme, failed, inView, containerRef]);

  // 容器尺寸变化（卡片网格重排/窗口缩放）时让 plotly 跟随
  useEffect(() => {
    if (!figure || failed) return;
    const el = containerRef.current;
    if (!el) return;
    let lastWidth = 0;
    const observer = new ResizeObserver(() => {
      const width = el.clientWidth;
      if (Math.abs(width - lastWidth) < 1) return;
      lastWidth = width;
      if (!renderedRef.current) return;
      void import("plotly.js-dist-min")
        .then((m) => m.default.Plots.resize(renderedRef.current as HTMLElement))
        .catch(() => {});
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [figure, failed, containerRef]);

  // 卸载时 purge，避免跨组件复用同一 DOM 产生残留
  useEffect(() => {
    return () => {
      const el = renderedRef.current;
      if (!el) return;
      void import("plotly.js-dist-min")
        .then((m) => m.default.purge(el))
        .catch(() => {});
    };
  }, []);

  if (failed) {
    if (fallbackSrc) {
      return <AuthImage src={fallbackSrc} alt={alt} loading="lazy" />;
    }
    const { Icon } = pickChartIcon(alt);
    return (
      <div className="echart-thumb-fallback">
        <Icon size={26} />
        <small>点击查看交互图</small>
      </div>
    );
  }
  return <div ref={containerRef} className="plotly-thumb" role="img" aria-label={alt} />;
});

export default PlotlyThumb;
