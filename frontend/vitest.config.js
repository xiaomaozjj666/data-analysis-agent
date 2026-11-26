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
    // === 覆盖率门禁 ===
    // 后端有 --cov-fail-under=97，前端此前只保证"测试不红"：212 个用例
    // 可以整块删掉而不报警。这里给前端补同等性质的门禁。
    // 口径：
    //   - provider v8（与 vitest 同源，无需额外转译，CI 与本地一致）
    //   - include 只圈前端源码；测试、类型声明、入口 main.tsx 不计入
    //   - 不排除任何业务组件/组件/hook（rb/ 装饰组件虽无独立测试，但被
    //     App.tsx / TaskBox / ReportView 实际引用，按业务代码计入）
    //   - 阈值 = 实测值向下留 ~1.5 个点：容忍 CI 与本地环境差异（node 版本、
    //     并发 worker 数导致的用例跳过），同时能拦住"整块测试被删"的回退。
    coverage: {
      provider: "v8",
      reporter: ["text-summary", "json-summary", "lcov"],
      reportsDirectory: "coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/__tests__/**",
        "src/types/**",
        "src/**/*.d.ts",
        "src/main.tsx",
      ],
      thresholds: {
        // 2026-11-26 第二轮实测（40+ 个测试文件）：lines 84.7%、statements 82.3%、
        // functions 79.4%、branches 72.6%（多次全量运行波动 < 0.2 个点）。
        // App.tsx 单文件：lines 98.4%、functions 98.8%、branches 83.1%。
        // 本轮把此前无测试的组件/工具补齐：DatasetOverview、ToolTraceItem、
        // ConversationThread、PlanPanel、HistoryPanel、EmptyWorkspace、CodeBlock、
        // ReportParts、utils/api（请求/上传/超时/取消的错误路径）、utils/notify。
        // 阈值统一向下留约 1.5~2 个点：既容忍 CI 环境（node 22 / 并发调度）与
        // 本地的小幅波动，也保证"删掉整块测试"或"新增组件完全不带测试"会被
        // 立刻拦下。提高阈值时按实测值向上调整，不要反过来靠排除文件凑数。
        lines: 83,
        statements: 80.5,
        functions: 77.5,
        branches: 70.5,
      },
    },
    // 2026-11-21 实测：本机同时跑两仓后端全量测试时，默认 fork 数（≈CPU 数）
    // 争抢资源，出现过 23 个 Unhandled Errors（worker 崩溃/测试超时连带）；
    // 独立复跑全绿、不可复现。钉住并发并放宽超时，让「本地并行开发」下
    // 结果可复现——代价只是本地跑得稍慢，CI 独占资源不受影响。
    maxWorkers: 4,
    testTimeout: 20_000,
    hookTimeout: 30_000,
  },
});
