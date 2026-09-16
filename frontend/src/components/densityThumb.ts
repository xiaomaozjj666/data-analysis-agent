// 密度视图（≥2 万行散点聚合成的分档热力图）在 209×131 缩略图里的公共解析层。
//
// 两个引擎的产物结构不同——Plotly 走 layout.meta.density_view，ECharts 走
// 顶层非标准键 densityBands——但"认出密度图 + 读出当前主题的档位色板"这一层
// 完全一致（色板必须整组切换，才能保证同一颜色在两种主题下代表同一档记录数）。
//
// 所有取值都对**旧产物**（没有这些键）与**结构损坏**（被截断/字段类型变了）
// 保持沉默：返回 null 让调用方退回原有精简行为，绝不抛错——缩略图渲染失败会
// 让整张卡片变成占位图，代价远大于少一次换色。
//
// 语义出处：src/data_agent/density.py（档位 1-2-5 系列、浅/暗两套色板）与
// src/data_agent/tools/density_plotly.py（meta.density_view 的字段）。

/** 单个档位（ECharts densityBands 的一项，与后端 band_legend 同构）。 */
export interface DensityBand {
  label: string;
  low: number | null;
  high: number | null;
  color: string;
}

/** Plotly 硬边界色标：[[档位位置, 颜色], ...]，每档一段纯色。 */
export type DensityColorscale = Array<[number, string]>;

export interface DensityColorscales {
  light: DensityColorscale | null;
  dark: DensityColorscale | null;
}

export interface DensityBands {
  light: DensityBand[] | null;
  dark: DensityBand[] | null;
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isColor(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

/** Plotly 档位色标 [[位置, 颜色], ...]；结构不合法时返回 null。 */
export function parseColorscale(value: unknown): DensityColorscale | null {
  if (!Array.isArray(value) || value.length < 2) return null;
  const scale: DensityColorscale = [];
  for (const entry of value) {
    if (!Array.isArray(entry) || entry.length < 2) return null;
    const position = entry[0];
    const color = entry[1];
    if (typeof position !== "number" || !Number.isFinite(position) || !isColor(color)) return null;
    scale.push([position, color]);
  }
  return scale;
}

/** ECharts 单主题档位列表 [{label, low, high, color}, ...]；不合法时返回 null。 */
export function parseBands(value: unknown): DensityBand[] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const bands: DensityBand[] = [];
  for (const entry of value) {
    const record = asRecord(entry);
    if (!record || !isColor(record.color)) return null;
    bands.push({
      label: typeof record.label === "string" ? record.label : "",
      low: numberOrNull(record.low),
      high: numberOrNull(record.high),
      color: record.color,
    });
  }
  return bands;
}

/**
 * 读 ECharts 密度产物顶层的 densityBands（两套主题色板）。
 *
 * 键不存在或两套都读不出来 → null（旧产物 / 损坏，调用方走普通热力图行为）。
 */
export function readDensityBands(option: Record<string, unknown>): DensityBands | null {
  const raw = asRecord(option.densityBands);
  if (!raw) return null;
  const light = parseBands(raw.light);
  const dark = parseBands(raw.dark);
  if (!light && !dark) return null;
  return { light, dark };
}

/** 当前主题的档位色；该主题缺失时退回另一套（有颜色总比默认彩虹色板好）。 */
export function pickBands(bands: DensityBands, isDark: boolean): DensityBand[] | null {
  return isDark ? bands.dark ?? bands.light : bands.light ?? bands.dark;
}

/**
 * 读 Plotly 密度图的档位色标（layout.meta.density_view.colorscales）。
 *
 * 返回非 null 即表示"这是密度图"（meta.density_view 存在）；此时即使色标
 * 两套都损坏（返回 {light: null, dark: null}），仍要走密度精简——去标注、
 * 去色标、隐刻度——只是不做换色。
 */
export function readPlotlyColorscales(layout: Record<string, unknown>): DensityColorscales | null {
  const meta = asRecord(layout.meta);
  const density = meta ? asRecord(meta.density_view) : null;
  if (!density) return null;
  const scales = asRecord(density.colorscales);
  return {
    light: scales ? parseColorscale(scales.light) : null,
    dark: scales ? parseColorscale(scales.dark) : null,
  };
}

/** 当前主题的 Plotly 档位色标；缺失时退回另一套，都缺失则 null（保持原色标）。 */
export function pickColorscale(
  scales: DensityColorscales,
  isDark: boolean,
): DensityColorscale | null {
  return isDark ? scales.dark ?? scales.light : scales.light ?? scales.dark;
}

// 密度图的所有坐标轴键：xaxis / yaxis / zaxis 以及分面子图的 xaxis2 … xaxis6。
const AXIS_KEY = /^(?:x|y|z)axis\d*$/;

export function isAxisKey(key: string): boolean {
  return AXIS_KEY.test(key);
}
