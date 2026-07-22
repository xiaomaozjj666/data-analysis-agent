import { useCallback, useEffect, useState } from "react";
import { THEME_KEY } from "../constants";

type Theme = "light" | "dark";

interface UseThemeResult {
  theme: Theme;
  toggle: () => void;
  setTheme: (theme: Theme) => void;
}

// 主题 hook：管理 light/dark 切换，首次进入时读取 localStorage，若无则跟随系统。
// 通过 document.documentElement.dataset.theme 设置 CSS 变量覆盖范围。
// 跟随系统模式下（localStorage 无值），监听 prefers-color-scheme 变化实时切换，
// 让 macOS 自动深色模式等场景能即时响应。用户手动切换后写入 localStorage，
// 不再跟随系统。
function useTheme(): UseThemeResult {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  // 跟随系统模式：localStorage 被清除后，监听系统主题变化
  useEffect(() => {
    const mediaQuery = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mediaQuery) return;
    const handler = (e: MediaQueryListEvent) => {
      // 仅在用户未手动选择（localStorage 无值）时跟随系统
      if (!window.localStorage.getItem(THEME_KEY)) {
        setTheme(e.matches ? "dark" : "light");
      }
    };
    mediaQuery.addEventListener?.("change", handler);
    return () => mediaQuery.removeEventListener?.("change", handler);
  }, []);
  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  return { theme, toggle, setTheme };
}

export default useTheme;
