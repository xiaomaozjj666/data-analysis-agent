import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 文字对比度门禁（WCAG AA）。
 *
 * 背景：本项目此前的"三级文字色"里 --fg-subtle 在浅色底只有 2.37:1、暗色底
 * 2.97:1，而它被时间戳/行号/提示/面包屑等正文用了 59 处——靠肉眼是发现不了的，
 * 是一次浏览器实测审计才暴露出来。这个测试把当时实测建立的**令牌契约**固化下来：
 * 每个文字令牌 × 每个可能承载文字的表面，两套主题都必须 ≥4.5:1。
 *
 * 为什么在 CI 里用"令牌矩阵"而不是浏览器审计：浏览器审计需要起服务 + 装
 * Playwright（约 2 分钟且多一个失败面），而绝大多数回归都是"改了令牌取值"，
 * 矩阵测试能在毫秒级拦住。浏览器审计保留为手工复核手段（见 ENGINEERING-NOTES）。
 *
 * 维护方式：新增承载文字的表面底色时把它加进 SURFACES；新增文字令牌时加进
 * TEXT_TOKENS。若某个令牌只是装饰（图标/分隔线），不要加进来。
 */

const stylesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const css = readFileSync(path.join(stylesDir, "tokens.css"), "utf8");

function block(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`tokens.css 里找不到 ${selector}`);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  const out: Record<string, string> = {};
  for (const match of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) out[match[1]] = match[2].trim();
  return out;
}

const light = block(":root {");
const dark = block('[data-theme="dark"] {');

function rgb(value: string): [number, number, number] {
  const hex = value.replace("#", "");
  const full = hex.length === 3 ? hex.split("").map((c) => c + c).join("") : hex;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as [number, number, number];
}

function luminance([r, g, b]: [number, number, number]): number {
  const channel = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(fg: string, bg: string): number {
  const a = luminance(rgb(fg));
  const b = luminance(rgb(bg));
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** 可能承载文字的表面底色（画布 / 分层底 / 交互态底 / 强调底）。 */
const SURFACES = [
  "--canvas-page",
  "--canvas-default",
  "--canvas-subtle",
  "--canvas-inset",
  "--sidebar-bg",
  "--topbar-bg",
  "--accent-subtle",
  "--control-hover",
  "--neutral-muted",
] as const;

/** 承载文字的三个令牌（装饰性颜色不要放进来）。 */
const TEXT_TOKENS = ["--fg-default", "--fg-muted", "--fg-subtle"] as const;

describe("设计令牌对比度（WCAG AA）", () => {
  for (const [theme, tokens] of [["浅色", light], ["暗色", dark]] as const) {
    describe(theme, () => {
      for (const token of TEXT_TOKENS) {
        it(`${token} 在所有承载文字的表面上都 ≥ 4.5:1`, () => {
          const failures: string[] = [];
          for (const surface of SURFACES) {
            const ratio = contrast(tokens[token], tokens[surface]);
            if (ratio < 4.5) {
              failures.push(
                `${token}(${tokens[token]}) 压在 ${surface}(${tokens[surface]}) 上只有 ${ratio.toFixed(2)}:1`,
              );
            }
          }
          expect(failures, failures.join("\n")).toEqual([]);
        });
      }

      it("白字压在实心强调色上 ≥ 4.5:1", () => {
        // 暗色主题会把 --accent-fg 提亮，所以"白字压底"必须用 --accent-solid，
        // 这条断言就是防止有人把按钮背景改回 --accent-fg。
        expect(contrast("#ffffff", tokens["--accent-solid"])).toBeGreaterThanOrEqual(4.5);
        expect(contrast("#ffffff", tokens["--accent-solid-hover"])).toBeGreaterThanOrEqual(4.5);
      });

      it("三级文字在页面底上保持可区分的层级", () => {
        const page = tokens["--canvas-page"];
        const levels = TEXT_TOKENS.map((token) => contrast(tokens[token], page));
        // 逐级变淡：default > muted > subtle，且相邻两级至少差 1.2 倍
        expect(levels[0]).toBeGreaterThan(levels[1] * 1.2);
        expect(levels[1]).toBeGreaterThan(levels[2] * 1.2);
      });
    });
  }
});
