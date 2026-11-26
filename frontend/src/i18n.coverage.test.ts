// i18n 静态检查：防止新增文案漏翻 / 占位符对不上 / 代码里用了不存在的 key。
//
// TypeScript 已经能保证 `EN` 的键与 `ZH` 完全一致（Record<MessageKey, string>），
// 但拦不住：英文值直接抄中文、占位符少写、以及代码里通过字符串扫描发现不了的
// 缺失键（例如动态拼接 key 时）。这里用源码扫描 + 字典比对补上这三类。
import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { EN, ZH, type MessageKey } from "./i18n";

// vitest 下 import.meta.url 不是 file:// 协议，直接以进程工作目录（frontend/）
// 为根定位源码目录；npm 脚本与 CI 都在 frontend 下执行。
const SRC_DIR = join(process.cwd(), "src");

// 语言名称允许在英文界面里保留中文（用户需要认出"简体中文"）。
const ALLOW_CHINESE_IN_EN = new Set(["cmd.toggleLanguageSub", "settings.zh"]);

function sourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "__tests__" || entry.name === "node_modules") continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...sourceFiles(full));
      continue;
    }
    if (!/\.(ts|tsx)$/.test(entry.name)) continue;
    if (/\.test\.(ts|tsx)$/.test(entry.name)) continue;
    if (entry.name === "i18n.ts") continue;
    files.push(full);
  }
  return files;
}

function collectUsedKeys(): { key: string; file: string }[] {
  const usage: { key: string; file: string }[] = [];
  // t("key") / t('key') 以及 translate(locale, "key")
  const patterns = [/\bt\(\s*["']([A-Za-z][\w.]*)["']/g, /\btranslate\(\s*[^,]+,\s*["']([A-Za-z][\w.]*)["']/g];
  for (const file of sourceFiles(SRC_DIR)) {
    const text = readFileSync(file, "utf8");
    for (const pattern of patterns) {
      for (const match of text.matchAll(pattern)) {
        usage.push({ key: match[1], file: relative(SRC_DIR, file) });
      }
    }
  }
  return usage;
}

function placeholders(template: string): string[] {
  return [...template.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
}

describe("i18n 字典完整性", () => {
  const zhKeys = Object.keys(ZH) as MessageKey[];
  const enKeys = Object.keys(EN) as MessageKey[];

  it("中英文字典键集合完全一致", () => {
    expect(enKeys.sort()).toEqual(zhKeys.sort());
  });

  it("所有文案非空且占位符一一对应", () => {
    for (const key of zhKeys) {
      expect(ZH[key].trim(), `zh:${key}`).not.toBe("");
      expect(EN[key].trim(), `en:${key}`).not.toBe("");
      expect(placeholders(EN[key]), `en:${key} 占位符与中文不一致`).toEqual(
        placeholders(ZH[key]),
      );
    }
  });

  it("英文值没有漏翻的中文（语言名称除外）", () => {
    const untranslated = zhKeys.filter(
      (key) => !ALLOW_CHINESE_IN_EN.has(key) && /[\u4e00-\u9fff]/.test(EN[key]),
    );
    expect(untranslated).toEqual([]);
  });

  it("源码里使用的字面量 key 都能在字典中找到", () => {
    const missing = collectUsedKeys().filter(({ key }) => !(key in ZH));
    expect(missing).toEqual([]);
  });
});
