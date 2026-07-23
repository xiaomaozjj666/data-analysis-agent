import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest 配置：独立于 vite.config.js（后者含 build 专用 rolldownOptions，
// 与测试无关；分开配置避免污染构建）。react 插件用于支持未来的 .tsx 组件测试。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
