import React, { useEffect, useRef, useState } from "react";
import { pickChartIcon } from "../constants";
import { fetchJsonWithTimeout } from "../utils/api";
import useInView from "../hooks/useInView";
import {
  asRecord,
  pickBands,
  readDensityBands,
  type DensityBand,
  type DensityBands,
} from "./densityThumb";

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

// #RRGGBB → HSL，暗色画布上把深色系提亮：热力图单元格颜色是
// visualMap 阶色板（浅→深蓝/红），深色端在 #1c2433 画布上几乎与
// 背景融为一体；只提亮明度 < 0.6 的颜色，浅色端保持不变，色相不受影响。
function boostForDark(hex: string): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  const [h, s, l] = rgbToHsl(r, g, b);
  // 浅色端保持原样（含大小写），只提亮深色端
  if (l >= 0.6) return hex;
  const boosted = Math.min(0.82, l * 1.9);
  return hslToHex(h, s, boosted);
}

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  const rr = r / 255;
  const gg = g / 255;
  const bb = b / 255;
  const max = Math.max(rr, gg, bb);
  const min = Math.min(rr, gg, bb);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = 0;
  if (max === rr) h = ((gg - bb) / d + (gg < bb ? 6 : 0)) / 6;
  else if (max === gg) h = ((bb - rr) / d + 2) / 6;
  else h = ((rr - gg) / d + 4) / 6;
  return [h, s, l];
}

function hslToHex(h: number, s: number, l: number): string {
  if (s === 0) {
    const v = Math.round(l * 255);
    return `#${[v, v, v].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
  }
  const hue2rgb = (p: number, q: number, t: number): number => {
    let tt = t;
    if (tt < 0) tt += 1;
    if (tt > 1) tt -= 1;
    if (tt < 1 / 6) return p + (q - p) * 6 * tt;
    if (tt < 1 / 2) return q;
    if (tt < 2 / 3) return p + (q - p) * (2 / 3 - tt) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const toHex = (x: number) => Math.round(x * 255).toString(16).padStart(2, "0");
  return `#${toHex(hue2rgb(p, q, h + 1 / 3))}${toHex(hue2rgb(p, q, h))}${toHex(hue2rgb(p, q, h - 1 / 3))}`;
}

const DEFAULT_HEATMAP_PALETTE = ["#EDF3F9", "#8FB3D1", "#2C5F8D"];

function hasSeriesType(option: Record<string, unknown>, type: string): boolean {
  const series = option.series;
  if (!Array.isArray(series)) return false;
  return series.some((item) => {
    const t = typeof item === "object" && item !== null ? (item as { type?: unknown }).type : undefined;
    return t === type;
  });
}

// 大数值在迷你卡里的紧凑格式（与全图 "x万" 口径一致）
function formatCompact(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return String(Number(value.toFixed(2)));
}

// === 密度视图（≥2 万行散点聚合成的分档热力图）===
// 语义见 src/data_agent/density.py：颜色按 1-2-5 系列分档表达每格记录数，
// 浅/暗两套档位色板随产物一起带过来（顶层非标准键 densityBands）。
// 缩略图只做两件事：① 整组换成当前主题的档位色（同一颜色在两种主题下必须
// 代表同一档记录数）；② 去掉轴文字/分隔线/档位图例——209×131 里它们全压在
// 格子上，"一块小热力图"才是缩略图该有的样子。densityBands 缺失或损坏时
// readDensityBands 返回 null，整个函数退回普通图表行为。

// 档位 → visualMap piece：只用于 pieces 缺失/损坏时的兜底重建（后端 pieces
// 自带 min/max，这里按档位边界补，语义一致——档位边界都是整数记录数）。
function bandPiece(band: DensityBand): Record<string, unknown> {
  const piece: Record<string, unknown> = { color: band.color };
  if (band.label) piece.label = band.label;
  if (band.low !== null) piece.min = band.low;
  if (band.high !== null) piece.max = band.high;
  return piece;
}

// pieces 是密度格子的唯一色源（图例隐藏后仍负责着色）：整组换成当前主题的
// 档位色，min/max/label 等结构原样保留。优先按 label 匹配（顺序被后端调过也
// 能对上），退回下标；两处都拿不到就保留原色。
function swapPieceColors(pieces: unknown[], palette: DensityBand[]): unknown[] {
  const byLabel = new Map(palette.map((band) => [band.label, band.color]));
  return pieces.map((piece, index) => {
    const record = asRecord(piece);
    if (!record) return piece;
    const matched = typeof record.label === "string" ? byLabel.get(record.label) : undefined;
    return { ...record, color: matched ?? palette[index]?.color ?? record.color };
  });
}

function densityVisualMap(source: unknown, bands: DensityBands, isDark: boolean): unknown {
  const palette = pickBands(bands, isDark);
  if (source === undefined || source === null) {
    // 没有 visualMap：档位色板是密度的唯一色源，补一套隐藏的分档映射
    // （否则 ECharts 退回默认色板，深浅不再代表记录数）
    return palette ? { show: false, type: "piecewise", pieces: palette.map(bandPiece) } : source;
  }
  const isList = Array.isArray(source);
  const updated = (isList ? source : [source]).map((entry, index) => {
    const record = asRecord(entry);
    if (!record) return entry;
    // 档位图例（分档色块 + 滑条）在迷你卡里会盖住半个绘图区
    const copy: Record<string, unknown> = { ...record, show: false };
    if (index > 0 || !palette) return copy;
    const inRange = asRecord(copy.inRange);
    if (Array.isArray(copy.pieces)) {
      copy.pieces = swapPieceColors(copy.pieces, palette);
    } else if (inRange) {
      copy.inRange = { ...inRange, color: palette.map((band) => band.color) };
    } else {
      copy.type = "piecewise";
      copy.pieces = palette.map(bandPiece);
    }
    return copy;
  });
  return isList ? updated : updated[0];
}

// 密度图的轴只负责定位格子：轴名、刻度文字、轴线与分隔线在缩略图里都压在
// 格子上。分面 / 边缘分布会有多组轴（xAxis、yAxis 数组按 gridIndex 对应各自
// 的 grid），逐个按同一规则处理。
// splitArea 是类目轴交替底纹（普通热力图用它读行列），它在**零记录格**后面
// 画出来会冒充数据（密度图的空格必须是透明的，见 density.py），一并关掉。
function densityAxis(axis: unknown): unknown {
  if (!axis || typeof axis !== "object") return axis;
  const copy: Record<string, unknown> = { ...(axis as Record<string, unknown>) };
  copy.name = "";
  delete copy.nameTextStyle;
  for (const key of ["axisLabel", "axisLine", "axisTick", "splitLine", "splitArea"]) {
    copy[key] = { ...(asRecord(copy[key]) ?? {}), show: false };
  }
  return copy;
}

// 多 grids（边缘分布 / 分面）保留原始几何：series 用 gridIndex 引用各自的
// 网格，换成单个 grid 会让所有面板叠在第一个网格上。只把**数值型（px）**内边距
// 收小——图例与轴名已隐藏，大图里的那些留白在 209×131 卡里只会挤掉数据；
// 百分比写法是多面板的比例布局，原样保留。
const DENSITY_GRID_INSET = 8;
const DENSITY_GRID_TIGHT = { left: 4, right: 4, top: 4, bottom: 4, containLabel: false };

function densityGrids(grid: unknown): unknown {
  const adjust = (entry: unknown): unknown => {
    const record = asRecord(entry);
    if (!record) return { ...DENSITY_GRID_TIGHT };
    const copy: Record<string, unknown> = { ...record, containLabel: false };
    for (const key of ["left", "right", "top", "bottom"]) {
      const value = record[key];
      if (typeof value === "number" && Number.isFinite(value)) {
        copy[key] = Math.min(value, DENSITY_GRID_INSET);
      }
    }
    return copy;
  };
  if (Array.isArray(grid)) return grid.map(adjust);
  const record = asRecord(grid);
  return record ? adjust(record) : { ...DENSITY_GRID_TIGHT };
}

// 密度图的锚点注记（与 Plotly 侧删掉的 layout.annotations / layout.shapes 对位）。
//
// 实测产物（api_bigtest0001 的散点图_3.echarts.json）里它们**不是** markPoint/
// markLine，而是两种更隐蔽的形态：
//   ① 每个热力图系列恰有一个"对象型"数据项，带 itemStyle（#E15759 红框 + 阴影）
//      与 label（"最密 10,104 条" 红字白底框）。per-item 的 label 会盖过 series
//      级的 label.show=false，所以 209×131 里四个分面各顶一个红框文字，整张缩略图
//      被读成"红框"而不是密度图；
//   ② 每面板一条趋势线，是 silent（legendHoverLink:false、tooltip.show:false）的
//      line 系列——非交互，属锚点注记而非数据系列。
// markPoint/markLine/graphic 一并兜底清理（其他后端版本可能这么存）。
// 只对密度选项生效：普通图表（含 correlation_heatmap）的标注必须逐字不变。
function stripDensityOverlays(option: Record<string, unknown>): void {
  delete option.graphic;
  if (!Array.isArray(option.series)) return;
  const kept: unknown[] = [];
  // 抽掉趋势线会改变后续系列的下标，而 visualMap 是按**下标**绑定系列的
  // （实测产物：seriesIndex [0,3,6,9] 指四个热力图）。不重映射的话，后两个
  // 热力图会掉出档位色板、退回全局调色板（橙/红大色块）——比注记更糟。
  const indexMap = new Map<number, number>();
  option.series.forEach((item, index) => {
    const series = asRecord(item);
    let keptItem: unknown = item;
    if (series) {
      // 非交互的趋势线是锚点注记：整条系列不再进入迷你图
      if (series.type === "line" && (series.silent === true || series.legendHoverLink === false)) return;
      const copy: Record<string, unknown> = { ...series };
      delete copy.markPoint;
      delete copy.markLine;
      if (copy.type === "heatmap" && Array.isArray(copy.data)) {
        // 峰值格只留 value（数据本体）；红框与"最密 N 条"文字去掉
        copy.data = copy.data.map((cell) => {
          const record = asRecord(cell);
          if (!record) return cell;
          const trimmed: Record<string, unknown> = { ...record };
          delete trimmed.label;
          delete trimmed.itemStyle;
          return trimmed;
        });
      }
      keptItem = copy;
    }
    indexMap.set(index, kept.length);
    kept.push(keptItem);
  });
  option.series = kept;
  option.visualMap = remapVisualMapSeries(option.visualMap, indexMap);
}

// 按系列重映射后的下标改写 visualMap.seriesIndex（数组或单值；被删掉的系列
// 不再保留引用）。其余字段（piecewise 的 pieces / dimension / selectedMode…）
// 原样保留——pieces 是格子颜色的唯一来源。返回改写后的结构（不就地改写入参）。
function remapVisualMapSeries(visualMap: unknown, indexMap: Map<number, number>): unknown {
  const remap = (value: unknown): number | undefined =>
    typeof value === "number" && Number.isFinite(value) ? indexMap.get(value) : undefined;
  const rewrite = (entry: unknown): unknown => {
    const record = asRecord(entry);
    if (!record || record.seriesIndex === undefined) return entry;
    const copy: Record<string, unknown> = { ...record };
    if (Array.isArray(copy.seriesIndex)) {
      copy.seriesIndex = copy.seriesIndex.map(remap).filter((value) => value !== undefined);
    } else {
      const mapped = remap(copy.seriesIndex);
      if (mapped === undefined) delete copy.seriesIndex;
      else copy.seriesIndex = mapped;
    }
    return copy;
  };
  if (Array.isArray(visualMap)) return visualMap.map(rewrite);
  return visualMap === undefined ? visualMap : rewrite(visualMap);
}

// 大图 option 直接塞进 140px 迷你会让图例/标题/坐标轴挤压重叠、文字
// 错乱。渲染迷你图前精简：删标题/图例/交互组件/轴标题，放大绘图区，
// 坐标文字用默认深色（画布是浅色底，无论原图深浅主题都可读）。
// 数据系列（柱条/箱线/散点）与关键标注（markPoint 等）全部保留；
// 但保留最小 tooltip（剥离 formatter 后走默认样式），卡片悬停即可
// 读数——与 Plotly 交互迷你图体验一致。
function simplifyForThumb(option: Record<string, unknown>, isDark: boolean): Record<string, unknown> {
  const output: Record<string, unknown> = { ...option };
  // 迷你图不需要的顶层组件
  delete output.title;
  delete output.legend;
  delete output.toolbox;
  delete output.dataZoom;
  delete output.animation;

  // 文字/轴线随主题：浅色画布（#fbfaf5）用深灰文字，深色画布
  // （#1c2433）用浅灰文字——迷你图首次渲染时读取当前主题。
  const textColor = isDark ? "#9aa0a6" : "#5f6368";
  const axisColor = isDark ? "#4a4b50" : "#c7ccd4";

  // 密度视图（大数据散点）走独立分支：档位色板、轴与 grids 的处理都与普通
  // 图表不同，且必须是"整组换色"而不是按亮度提亮（见上方 density 区块）。
  const densityBands = readDensityBands(output);
  const density = densityBands !== null;

  // 热力图：color 映射完全来自 visualMap，直接删除会让所有格子
  // 退化成同一个色块（global 色板第一色）。保留精简 visualMap
  // （隐藏滑条、只留 inRange），暗色画布下把深色端提亮保证对比度。
  // 热力图 tooltip 剥离 formatter 后默认展示原始数组 [x,y,v]，
  // 比没有更糟糕，故不保留；点进完整交互图可读。
  if (density) {
    // 与普通热力图同理：formatter 剥离后 hover 只剩原始数组，不如不给
    delete output.tooltip;
    // 峰值红框/"最密 N 条"、分面趋势线与 graphic 注记在迷你卡里盖过密度本身
    stripDensityOverlays(output);
    output.visualMap = densityVisualMap(output.visualMap, densityBands, isDark);
  } else if (hasSeriesType(output, "heatmap")) {
    delete output.tooltip;
    const sourceVm = (output.visualMap ?? {}) as Record<string, unknown>;
    const sourceColors = (
      ((sourceVm.inRange as Record<string, unknown> | undefined)?.color as string[] | undefined) ?? DEFAULT_HEATMAP_PALETTE
    );
    output.visualMap = {
      min: sourceVm.min,
      max: sourceVm.max,
      show: false,
      inRange: { color: isDark ? sourceColors.map(boostForDark) : sourceColors },
    };
  } else if (output.tooltip) {
    // formatter 是 JS 函数字符串，已在上游剥离；保留 trigger 走默认样式
    const trigger =
      typeof output.tooltip === "object" && (output.tooltip as { trigger?: unknown }).trigger === "axis"
        ? "axis"
        : "item";
    output.tooltip = {
      trigger,
      backgroundColor: isDark ? "#232b3a" : "#ffffff",
      borderColor: isDark ? "#3a4258" : "#d8dce3",
      textStyle: { color: isDark ? "#e8ebf0" : "#333a45" },
    };
  } else {
    output.tooltip = { trigger: "item" };
  }

  const ax = (axis: unknown): unknown => {
    if (!axis || typeof axis !== "object") return axis;
    const a: Record<string, unknown> = { ...(axis as Record<string, unknown>) };
    delete a.name;
    // 默认值放在展开之后：迷你图固定 9px 与主题色，同时保留
    // 原配置里的 rotate/interval 等排版设定
    const label = { ...((a.axisLabel as Record<string, unknown> | undefined) ?? {}), fontSize: 9, color: textColor };
    a.axisLabel = label;
    if (typeof a.axisLine !== "object") {
      a.axisLine = { lineStyle: { color: axisColor } };
    }
    return a;
  };
  // 密度图没有可读的轴（分箱坐标在 209×131 里读不出数值），轴名/刻度/分隔线
  // 一律去掉，只留格子的颜色分布
  const axisMapper = density ? densityAxis : ax;
  if (Array.isArray(output.xAxis)) output.xAxis = output.xAxis.map(axisMapper);
  else if (output.xAxis) output.xAxis = axisMapper(output.xAxis);
  if (Array.isArray(output.yAxis)) output.yAxis = output.yAxis.map(axisMapper);
  else if (output.yAxis) output.yAxis = axisMapper(output.yAxis);

  // 3D 轴（xAxis3D 等）：轴名（销售额/利润/数量）与轴刻度在 209×131
  // 小卡里重叠成文字团，一并删除；刻度字号降到 8
  for (const key of ["xAxis3D", "yAxis3D", "zAxis3D"]) {
    const axis = output[key];
    if (axis && typeof axis === "object") {
      const a: Record<string, unknown> = { ...(axis as Record<string, unknown>) };
      delete a.name;
      a.axisLabel = {
        ...((a.axisLabel as Record<string, unknown> | undefined) ?? {}),
        fontSize: 8,
        color: textColor,
      };
      a.nameTextStyle = { fontSize: 8 };
      output[key] = a;
    }
  }

  // 全局文字统一主题色，画布上才可读
  if (!output.textStyle) output.textStyle = { color: textColor };

  // 峰谷大头针（42px）在 131px 高卡片里过大且顶部被裁：缩小并预先把
  // formatter 换成格式化好的静态文本（函数字符串剥离后默认会显示
  // 裸值 148062，"效果粗糙"）。markLine 均值同理。
  let gridTop = 14;
  if (Array.isArray(output.series)) {
    for (const item of output.series) {
      if (!item || typeof item !== "object") continue;
      const s = item as Record<string, unknown>;
      const data = Array.isArray(s.data) ? s.data : [];
      const nums: number[] = [];
      for (const v of data) {
        const n = typeof v === "number" ? v : Array.isArray(v) ? v[v.length - 1] : NaN;
        if (Number.isFinite(n)) nums.push(n);
      }
      if (nums.length === 0) continue;
      const markPoint = s.markPoint as Record<string, unknown> | undefined;
      if (markPoint && typeof markPoint === "object") {
        markPoint.symbolSize = 20;
        if (Array.isArray(markPoint.data)) {
          for (const item of markPoint.data) {
            if (!item || typeof item !== "object") continue;
            const it = item as Record<string, unknown>;
            const val = it.type === "max" ? Math.max(...nums) : it.type === "min" ? Math.min(...nums) : undefined;
            if (val !== undefined) {
              it.label = { ...((it.label as Record<string, unknown> | undefined) ?? {}), formatter: formatCompact(val as number) };
            }
          }
        }
        gridTop = Math.max(gridTop, 24);
      }
      const markLine = s.markLine as Record<string, unknown> | undefined;
      if (markLine && typeof markLine === "object" && Array.isArray(markLine.data)) {
        const avg = nums.reduce((a, b) => a + b, 0) / nums.length;
        for (const item of markLine.data) {
          if (item && typeof item === "object" && (item as { type?: unknown }).type === "average") {
            const it = item as Record<string, unknown>;
            it.label = { ...((it.label as Record<string, unknown> | undefined) ?? {}), formatter: `均值 ${formatCompact(avg)}` };
          }
        }
      }
    }
  }

  // 绘图区占满容器：留少量边距 + 容纳轴标签（顶部多留一点，
  // 避免柱状图/折线图顶部网格线贴边被卡片裁切）。
  // 密度图例外：多 grids 的几何由后端按面板比例给出（series 用 gridIndex
  // 引用），不能被单个 grid 覆盖，只收紧数值型内边距。
  output.grid = density
    ? densityGrids(output.grid)
    : { left: 4, right: 10, top: gridTop, bottom: 6, containLabel: true };
  // 柱状图数值标签：预格式化静态文本（在 markPoint 数值提取之后，
  // 不能把数据项提前转成对象）
  bakeBarValueLabels(output);
  return output;
}

// 柱状图顶部数值标签：formatter 是 JS 函数字符串，迷你图里被剥离后
// ECharts 会退回默认显示裸浮点（70012.68000000001）。把每个数值预
// 格式化成静态文本（5.5万 / 7.0万）挂到数据项上，迷你图与全图读数
// 口径一致。
function bakeBarValueLabels(option: Record<string, unknown>): void {
  const series = option.series;
  if (!Array.isArray(series)) return;
  for (const item of series) {
    if (!item || typeof item !== "object") continue;
    const s = item as Record<string, unknown>;
    const label = s.label as Record<string, unknown> | undefined;
    if (!label || label.show === false) continue;
    const data = s.data as unknown[] | undefined;
    if (!Array.isArray(data) || data.length > 60) continue;
    s.data = data.map((v) => {
      if (typeof v !== "number") return v;
      return { value: v, label: { show: true, formatter: formatCompact(v) } };
    });
  }
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

// 迷你图渲染前精简布局。热力图全量格子数值 label 在 209×131
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

// 等两帧让容器的 aspect-ratio 布局稳定后再初始化，避免 ECharts
// 按错误尺寸初绘导致坐标轴错位/顶部被裁。rAF 在隐藏/被遮挡标签页里
// 会被 Chromium 冻结（实测 visibility=hidden 时永不触发，缩略图永久
// 空白），必须加超时兜底——宁可按当前布局初始化也不留白。
const nextFrame = () =>
  new Promise<void>((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    requestAnimationFrame(() => requestAnimationFrame(finish));
    setTimeout(finish, 200);
  });

// ECharts 产物卡片的内联迷你图：产物页无需点击即可预览图表内容。
// 渲染策略：fetch 后端 echarts-json → 剥离函数字段 + 精简布局 →
// 懒加载 echarts → SVG 渲染到浅色画布（与 Plotly PNG 缩略图的米白底
// 一致）。失败（无数据文件/损坏）时回退占位。
const EChartThumb = React.memo(function EChartThumb({ previewUrl, alt }: EChartThumbProps) {
  // 容器同时承担可见性观测：进入视口才拉取数据并初始化图表，离开视口
  // dispose 释放实例（重新进入时用缓存的 option 重渲染，不再发请求）。
  const { ref: containerRef, inView } = useInView<HTMLDivElement>("240px");
  const [failed, setFailed] = useState(false);
  // 主题随 data-theme 变化响应：迷你图颜色在渲染时按主题取色，
  // 切换主题后重渲染（容器 CSS 背景即时变化，画布文字需同步）。
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme === "dark");
  // 已拉取并清洗过的 option 缓存：主题切换/滚出视口后的重渲染不再发起
  // 网络请求——产物网格几十张卡同时换肤时，避免几十个重复 echarts-json
  // 请求（首卡之后的渲染全部走本地缓存）。
  const optionRef = useRef<Record<string, unknown> | null>(null);

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setTheme(document.documentElement.dataset.theme === "dark");
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let disposed = false;
    let chart: { resize: () => void; dispose: () => void } | null = null;
    let observer: ResizeObserver | null = null;

    (async () => {
      try {
        // 出视口：释放实例，把常驻图表数压到"可见数"。缓存的 option
        // 保留，重新进入视口时本 effect 会重建。
        if (!inView) {
          void import("echarts").then((echarts) => {
            const el = containerRef.current;
            if (el) echarts.getInstanceByDom(el)?.dispose();
          }).catch(() => {});
          return;
        }
        if (!optionRef.current) {
          const jsonUrl = previewUrl.replace(/\/preview$/, "/echarts-json");
          const cleaned = await fetchJsonWithTimeout<Record<string, unknown>>(jsonUrl);
          if (disposed) return;
          if (cleaned) optionRef.current = stripFunctions(cleaned) as Record<string, unknown>;
        }
        const cleaned = optionRef.current;
        if (disposed || !cleaned) return;
        // simplifyForThumb 会原地改写 series 嵌套对象（markPoint 缩放、
        // 柱顶数值标签烘焙成静态文本），从缓存二次渲染前必须深拷贝，
        // 避免对已转换的数据形态重复改写。
        const source = structuredClone(cleaned);
        // 3D 系列（scatter3D 等）需要 echarts-gl 扩展，懒加载后同样
        // 能在卡片内渲染迷你 3D 视图。
        const needsGl = hasGlSeries(source);
        const simplify = simplifyForThumb(source, theme);
        stripDenseLabels(simplify);
        await nextFrame();
        const echarts = await import("echarts");
        if (needsGl) await import("echarts-gl");
        if (disposed || !inView || !containerRef.current) return;
        // 防御性清理：快进快出时上一次的异步 dispose 可能尚未完成，
        // 直接 init 到带实例的 DOM 会抛错
        echarts.getInstanceByDom(containerRef.current)?.dispose();
        const instance = echarts.init(containerRef.current, undefined, {
          renderer: needsGl ? "canvas" : "svg",
        });
        instance.setOption(simplify);
        // 布局稳定后再强重绘一次，修复 init 与最终 CSS 尺寸不一致
        // （rAF + 超时双保险：隐藏标签页里 rAF 冻结，超时保证执行）
        requestAnimationFrame(() => instance.resize());
        setTimeout(() => instance.resize(), 300);
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
  }, [previewUrl, theme, inView, containerRef]);

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
export { simplifyForThumb, stripFunctions, boostForDark, formatCompact };
