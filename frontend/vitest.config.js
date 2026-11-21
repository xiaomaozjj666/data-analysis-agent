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
    // 2026-11-21 实测：本机同时跑两仓后端全量测试时，默认 fork 数（≈CPU 数）
    // 争抢资源，出现过 23 个 Unhandled Errors（worker 崩溃/测试超时连带）；
    // 独立复跑全绿、不可复现。钉住并发并放宽超时，让「本地并行开发」下
    // 结果可复现——代价只是本地跑得稍慢，CI 独占资源不受影响。
    maxWorkers: 4,
    testTimeout: 20_000,
    hookTimeout: 30_000,
  },
});
