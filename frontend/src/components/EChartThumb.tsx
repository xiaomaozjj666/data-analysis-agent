import React, { useEffect, useRef, useState } from "react";
import { API_URL } from "../constants";
import { pickChartIcon } from "../constants";
import { requestHeaders } from "../utils/api";

interface EChartThumbProps {
  /** 图表 preview_url（/api/sessions/{sid}/artifacts/{name}/preview），
      用于推导 echarts-json 地址与点击预览回调。 */
  previewUrl: string;
  alt: string;
}

// 递归移除 option 中以字符串存档的 JS 函数（"function(...){...}"）。
// 迷你图只渲染基础图形，不执行任何代码——避免 new Function 执行
// 产物 JSON 里任意字符串的安全风险（与 dashboard 服务端还原不同，
// 前端没有可信的执行上下文）。
function stripFunctions(node: unknown): unknown {
  if (typeof node === "string") {
    const s = node.trim();
    if (s.startsWith("function(") && s.endsWith("}")) return undefined;
    return node;
  }
  if (Array.isArray(node)) {
    const cleaned = node.map(stripFunctions).filter((v) => v !== undefined);
    return cleaned;
  }
  if (node && typeof node === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      const cleaned = stripFunctions(value);
      if (cleaned !== undefined) out[key] = cleaned;
    }
    return out;
  }
  return node;
}

// 大图 option 直接塞进 140px 迷你会让图例/标题/坐标轴挤压重叠、文字
// 错乱。渲染迷你图前精简：删标题/图例/交互组件/轴标题，放大绘图区，
// 坐标文字用默认深色（画布是浅色底，无论原图深浅主题都可读）。
// 数据系列（柱条/箱线/散点）与关键标注（markPoint 等）全部保留。
function simplifyForThumb(option: Record<string, unknown>): Record<string, unknown> {
  const output: Record<string, unknown> = { ...option };
  // 迷你图不需要的顶层组件
  delete output.title;
  delete output.legend;
  delete output.toolbox;
  delete output.tooltip;
  delete output.dataZoom;
  delete output.visualMap;
  delete output.animation;
  // 绘图区占满容器：留少量边距 + 容纳轴标签（顶部多留一点，
  // 避免柱状图/折线图顶部网格线贴边被卡片裁切）
  output.grid = { left: 4, right: 10, top: 14, bottom: 6, containLabel: true };

  // 文字/轴线随主题：浅色画布（#fbfaf5）用深灰文字，深色画布
  // （#1c2433）用浅灰文字——迷你图首次渲染时读取当前主题。
  const isDark = document.documentElement.dataset.theme === "dark";
  const textColor = isDark ? "#9aa0a6" : "#5f6368";
  const axisColor = isDark ? "#4a4b50" : "#c7ccd4";

  const ax = (axis: unknown): unknown => {
    if (!axis || typeof axis !== "object") return axis;
    const a: Record<string, unknown> = { ...(axis as Record<string, unknown>) };
    delete a.name;
    const label = { fontSize: 9, color: textColor, ...((a.axisLabel as Record<string, unknown> | undefined) ?? {}) };
    a.axisLabel = label;
    if (typeof a.axisLine !== "object") {
      a.axisLine = { lineStyle: { color: axisColor } };
    }
    return a;
  };
  if (Array.isArray(output.xAxis)) output.xAxis = output.xAxis.map(ax);
  else if (output.xAxis) output.xAxis = ax(output.xAxis);
  if (Array.isArray(output.yAxis)) output.yAxis = output.yAxis.map(ax);
  else if (output.yAxis) output.yAxis = ax(output.yAxis);
  // 全局文字统一主题色，画布上才可读
  if (!output.textStyle) output.textStyle = { color: textColor };
  return output;
}

// 检测 option 是否包含 echarts-gl 3D 系列（scatter3D/bar3D/surface/
// lines3D/scatterGL 等）。这类系列需要 echarts-gl 扩展才能渲染。
function hasGlSeries(option: Record<string, unknown>): boolean {
  const series = option.series;
  if (!Array.isArray(series)) return false;
  return series.some((item) => {
    const type = typeof item === "object" && item !== null ? (item as { type?: unknown }).type : undefined;
    return typeof type === "string" && (/3D$/.test(type) || /GL$/.test(type));
  });
}

// 迷你图渲染前精简布局。热力图/大表图全量格子数值 label 在 209×131
// 小卡里会全部挤成文字块，剥离 series.label 只保留颜色编码。
function stripDenseLabels(option: Record<string, unknown>): void {
  const series = option.series;
  if (!Array.isArray(series)) return;
  for (const item of series) {
    if (typeof item !== "object" || item === null) continue;
    const type = (item as { type?: unknown }).type;
    if (type === "heatmap") {
      delete (item as { label?: unknown }).label;
    }
  }
}

// ECharts 产物卡片的内联迷你图：产物页无需点击即可预览图表内容。
// 渲染策略：fetch 后端 echarts-json → 剥离函数字段 + 精简布局 →
// 懒加载 echarts → SVG 渲染到浅色画布（与 Plotly PNG 缩略图的米白底
// 一致）。失败（无数据文件/损坏）时回退占位。
const EChartThumb = React.memo(function EChartThumb({ previewUrl, alt }: EChartThumbProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let disposed = false;
    let chart: { resize: () => void; dispose: () => void } | null = null;
    let observer: ResizeObserver | null = null;

    (async () => {
      try {
        const jsonUrl = previewUrl.replace(/\/preview$/, "/echarts-json");
        const response = await fetch(`${API_URL}${jsonUrl}`, { headers: requestHeaders() });
        if (!response.ok) throw new Error(`echarts-json ${response.status}`);
        const raw = (await response.json()) as unknown;
        const cleaned = stripFunctions(raw) as Record<string, unknown> | undefined;
        if (disposed || !cleaned) return;
        // 3D 系列（scatter3D 等）需要 echarts-gl 扩展，懒加载后同样
        // 能在卡片内渲染迷你 3D 视图。
        const needsGl = hasGlSeries(cleaned);
        const simplify = simplifyForThumb(cleaned);
        stripDenseLabels(simplify);
        // 等两帧让容器的 aspect-ratio 布局稳定后再初始化，避免 ECharts
        // 按错误尺寸初绘导致坐标轴错位/顶部被裁。
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const echarts = await import("echarts");
        if (needsGl) await import("echarts-gl");
        if (disposed || !containerRef.current) return;
        const instance = echarts.init(containerRef.current, undefined, {
          renderer: needsGl ? "canvas" : "svg",
        });
        instance.setOption(simplify);
        // 布局稳定后再强重绘一次，修复 init 与最终 CSS 尺寸不一致
        requestAnimationFrame(() => instance.resize());
        chart = instance;
        observer = new ResizeObserver(() => instance.resize());
        observer.observe(containerRef.current);
      } catch {
        if (!disposed) setFailed(true);
      }
    })();

    return () => {
      disposed = true;
      observer?.disconnect();
      chart?.dispose();
    };
  }, [previewUrl]);

  if (failed) {
    const { Icon } = pickChartIcon(alt);
    return (
      <div className="echart-thumb-fallback">
        <Icon size={26} />
        <small>点击查看交互图</small>
      </div>
    );
  }
  return <div ref={containerRef} className="echart-thumb" role="img" aria-label={alt} />;
});

export default EChartThumb;
