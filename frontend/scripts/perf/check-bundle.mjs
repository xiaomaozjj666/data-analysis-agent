#!/usr/bin/env node
/**
 * 首屏 JS 体积 / 图表库懒加载检查（零依赖，只读 dist 产物）。
 *
 * 用法：
 *   npm run build && npm run perf:check
 *
 * 检查内容：
 *   1. 入口 chunk gzip ≤ 400 KiB（409 600 B）；
 *   2. plotly / echarts / echarts-gl 不在入口 chunk 的静态依赖图里；
 *   3. 三者确实是某个首屏 chunk 的动态 import() 目标（懒加载链路存在）；
 *   4. src 生产代码里没有对它们的静态 import（防止回归）。
 *
 * 退出码：0 全部通过；1 任一检查失败；2 dist 缺失（忘记 build）。
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = resolve(SCRIPT_DIR, "../..");
const DIST_DIR = join(FRONTEND_DIR, "dist");
const ASSETS_DIR = join(DIST_DIR, "assets");
const SRC_DIR = join(FRONTEND_DIR, "src");
const INDEX_HTML = join(DIST_DIR, "index.html");

const ENTRY_GZIP_BUDGET = 400 * 1024; // 400 KiB
const CHART_LIB_PATTERN = /^(?:plotly(?:\.min)?|echarts(?:-gl)?)-/;
const CHART_LIB_SOURCE_PATTERN = /^(?:plotly\.js-dist-min|echarts|echarts-gl)(?:\/|$)/;

if (!existsSync(INDEX_HTML) || !existsSync(ASSETS_DIR)) {
  console.error("未找到 dist/index.html 或 dist/assets，请先运行：npm run build");
  process.exit(2);
}

const failures = [];
const notes = [];

const fmt = (bytes) => `${(bytes / 1024).toFixed(2)} KiB`;
const rel = (file) => relative(FRONTEND_DIR, file).split(sep).join("/");

/** 从 index.html 读取真正的入口 <script type=module> 与 modulepreload 列表。 */
function readEntryPoints(html) {
  const attr = (tag, name) => {
    const match = new RegExp(`\\b${name}\\s*=\\s*["']([^"']*)["']`, "i").exec(tag);
    return match?.[1] ?? null;
  };
  const scripts = [...html.matchAll(/<script\b[^>]*>/gi)].map((m) => m[0]);
  const links = [...html.matchAll(/<link\b[^>]*>/gi)].map((m) => m[0]);

  const entry = scripts
    .filter((tag) => /\btype\s*=\s*["']module["']/i.test(tag))
    .map((tag) => attr(tag, "src"))
    .find((src) => src && /\.m?js(?:[?#]|$)/.test(src));
  const preloads = links
    .filter((tag) => /\brel\s*=\s*["']modulepreload["']/i.test(tag))
    .map((tag) => attr(tag, "href"))
    .filter((href) => href && /\.m?js(?:[?#]|$)/.test(href));

  return { entry, preloads };
}

/** 把 index.html 里的 URL（/assets/x.js）或相对 chunk 引用解析成本地文件。 */
function resolveAsset(specifier, fromFile) {
  if (!specifier) return null;
  const clean = specifier.split(/[?#]/)[0];
  let candidate;
  if (clean.startsWith("/")) {
    candidate = join(DIST_DIR, clean.slice(1));
  } else if (clean.startsWith(".")) {
    candidate = resolve(dirname(fromFile), clean);
  } else {
    candidate = join(ASSETS_DIR, clean);
  }
  return existsSync(candidate) && statSync(candidate).isFile() ? candidate : null;
}

/** ES 静态 import / export-from 的模块说明符（不含 import() 动态形式）。 */
function staticSpecifiers(code) {
  const found = new Set();
  const fromRe = /\b(?:import|export)\s*[^;"'`]*?\bfrom\s*["']([^"']+)["']/g;
  for (const match of code.matchAll(fromRe)) found.add(match[1]);
  const sideEffectRe = /(?:^|[;{}])\s*import\s*["']([^"']+)["']/g;
  for (const match of code.matchAll(sideEffectRe)) found.add(match[1]);
  return [...found];
}

/** 动态 import("...") / import(`...`) 的模块说明符。 */
function dynamicSpecifiers(code) {
  const found = new Set();
  const re = /\bimport\s*\(\s*(?:["']([^"']+)["']|`([^`]+)`)\s*\)/g;
  for (const match of code.matchAll(re)) {
    const specifier = match[1] ?? match[2];
    if (specifier) found.add(specifier);
  }
  return [...found];
}

const moduleCache = new Map();
function loadModule(file) {
  if (!moduleCache.has(file)) {
    const source = readFileSync(file);
    moduleCache.set(file, {
      file,
      source,
      gzip: gzipSync(source, { level: 9 }).length,
      size: source.length,
    });
  }
  return moduleCache.get(file);
}

const html = readFileSync(INDEX_HTML, "utf8");
const { entry: entryUrl, preloads } = readEntryPoints(html);
if (!entryUrl) {
  console.error("dist/index.html 中没有找到入口 <script type=\"module\" src=...>。");
  process.exit(1);
}

const entryFile = resolveAsset(entryUrl, INDEX_HTML);
if (!entryFile) {
  console.error(`入口脚本不存在：${entryUrl}`);
  process.exit(1);
}

// ---- 构建入口静态依赖图（modulepreload 也属于首屏初始图）----
const initialGraph = new Set();
const queue = [entryFile];
const preloadFiles = new Set();
for (const href of preloads) {
  const file = resolveAsset(href, INDEX_HTML);
  if (file) {
    preloadFiles.add(file);
    queue.push(file);
  }
}
while (queue.length > 0) {
  const file = queue.pop();
  if (initialGraph.has(file)) continue;
  initialGraph.add(file);
  const mod = loadModule(file);
  for (const specifier of staticSpecifiers(mod.source.toString("utf8"))) {
    const dep = resolveAsset(specifier, file);
    if (dep && !initialGraph.has(dep)) queue.push(dep);
  }
}

// 首屏 chunks 各自能触发的动态 import 目标（懒加载链路）。
const dynamicTargets = new Set();
for (const file of initialGraph) {
  const mod = loadModule(file);
  for (const specifier of dynamicSpecifiers(mod.source.toString("utf8"))) {
    const dep = resolveAsset(specifier, file);
    if (dep) dynamicTargets.add(dep);
  }
}

const allJsAssets = readdirSync(ASSETS_DIR)
  .filter((name) => name.endsWith(".js"))
  .map((name) => join(ASSETS_DIR, name));

const chartAssets = allJsAssets.filter((file) => CHART_LIB_PATTERN.test(rel(file).split("/").pop()));

// ---- 断言 1：入口 chunk gzip 预算 ----
const entryMod = loadModule(entryFile);
if (entryMod.gzip > ENTRY_GZIP_BUDGET) {
  failures.push(
    `入口 chunk gzip ${fmt(entryMod.gzip)} 超过预算 ${fmt(ENTRY_GZIP_BUDGET)}（${rel(entryFile)}）`,
  );
}

// ---- 断言 2/3：图表库在静态图之外、且确实走动态 import ----
const staticViolations = chartAssets.filter((file) => initialGraph.has(file));
for (const file of staticViolations) {
  failures.push(`图表库 chunk 进入入口静态依赖图：${rel(file)}`);
}
if (chartAssets.length === 0) {
  notes.push("dist 中没有独立的 plotly/echarts/echarts-gl chunk（可能已被内联或改名，请人工确认）。");
}
for (const file of chartAssets) {
  if (!dynamicTargets.has(file)) {
    failures.push(`图表库 chunk 不是首屏 chunk 的动态 import 目标（懒加载链路缺失）：${rel(file)}`);
  }
}

// ---- 断言 4：src 生产代码没有静态 import 图表库 ----
function* walkSource(dir) {
  for (const name of readdirSync(dir)) {
    const file = join(dir, name);
    const stat = statSync(file);
    if (stat.isDirectory()) {
      if (name === "__tests__" || name === "node_modules") continue;
      yield* walkSource(file);
    } else if (/\.(?:ts|tsx)$/.test(name) && !/\.(?:test|spec)\./.test(name)) {
      yield file;
    }
  }
}

for (const file of walkSource(SRC_DIR)) {
  if (file.split(sep).includes("types")) continue;
  const code = readFileSync(file, "utf8");
  const fromRe = /\b(import|export)\s+(type\s+)?[^;"'`]*?\bfrom\s*["']([^"']+)["']/g;
  for (const match of code.matchAll(fromRe)) {
    const [, , typeOnly, specifier] = match;
    if (typeOnly) continue;
    if (CHART_LIB_SOURCE_PATTERN.test(specifier)) {
      failures.push(`${rel(file)} 静态 import 图表库 "${specifier}"（应改为动态 import()）`);
    }
  }
  const sideEffectRe = /(?:^|[;{}])\s*import\s*["']([^"']+)["']/g;
  for (const match of code.matchAll(sideEffectRe)) {
    if (CHART_LIB_SOURCE_PATTERN.test(match[1])) {
      failures.push(`${rel(file)} 静态 import 图表库 "${match[1]}"（应改为动态 import()）`);
    }
  }
}

// ---- 报告 ----
const initialChunks = [...initialGraph]
  .map((file) => loadModule(file))
  .sort((a, b) => b.size - a.size);
const initialRaw = initialChunks.reduce((sum, mod) => sum + mod.size, 0);
const initialGzip = initialChunks.reduce((sum, mod) => sum + mod.gzip, 0);

const asyncChunks = allJsAssets
  .filter((file) => !initialGraph.has(file))
  .map((file) => loadModule(file))
  .sort((a, b) => b.size - a.size);

console.log("== 首屏 JS 体积 / 图表库懒加载检查 ==");
console.log(`入口 chunk : ${rel(entryFile)}`);
console.log(`            raw ${fmt(entryMod.size)} / gzip ${fmt(entryMod.gzip)}`);
console.log(`预算       : gzip ≤ ${fmt(ENTRY_GZIP_BUDGET)}  (400 KB)`);
console.log("");
console.log(`初始静态图 (${initialChunks.length} 个 chunk，含 modulepreload)：`);
if (preloadFiles.size > 0) {
  console.log(`  modulepreload: ${[...preloadFiles].map((f) => rel(f).split("/").pop()).join(", ")}`);
}
for (const mod of initialChunks) {
  console.log(`  ${rel(mod.file).padEnd(46)} raw ${fmt(mod.size).padStart(11)} / gzip ${fmt(mod.gzip).padStart(10)}`);
}
console.log(`  初始 JS 合计                              raw ${fmt(initialRaw).padStart(11)} / gzip ${fmt(initialGzip).padStart(10)}`);
console.log("");
console.log(`异步 chunk (${asyncChunks.length} 个)：`);
for (const mod of asyncChunks) {
  console.log(`  ${rel(mod.file).padEnd(46)} raw ${fmt(mod.size).padStart(11)} / gzip ${fmt(mod.gzip).padStart(10)}`);
}
console.log("");
console.log("图表库懒加载：");
if (chartAssets.length === 0) {
  console.log("  (未发现独立图表库 chunk)");
}
for (const file of chartAssets.sort()) {
  const mod = loadModule(file);
  const loadedStatic = initialGraph.has(file);
  const lazyTarget = dynamicTargets.has(file);
  console.log(
    `  ${rel(file).padEnd(46)} gzip ${fmt(mod.gzip).padStart(10)}  ` +
      `static=${loadedStatic ? "YES" : "no"}  dynamic=${lazyTarget ? "YES" : "no"}`,
  );
}

for (const note of notes) console.log(`\n注意：${note}`);

if (failures.length > 0) {
  console.error("\n结果：FAIL");
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}

console.log(
  `\n结果：PASS（入口 gzip ${fmt(entryMod.gzip)} ≤ ${fmt(ENTRY_GZIP_BUDGET)}；` +
    `plotly/echarts/echarts-gl 均不在入口静态图内）`,
);
