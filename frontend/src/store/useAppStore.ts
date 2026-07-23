// 旧的单一 store 已按功能拆分为独立的 slice store（每个 create<T>()），
// 避免 15+ slice 共用一个 store 导致每次状态变更触发所有订阅者重渲染。
//
// 拆分后的 store：
//   useAuthStore / useConfigStore / useSessionStore / useAnalysisStore /
//   useUIStore / useErrorStore / useKeyStore / useToolTraceStore /
//   useChatStore / useRetryStore / useBusyStore / useHistoryStore /
//   useTimerStore / useReasoningStore
//
// 这里保留 useAppStore 作为向后兼容的聚合 hook：调用所有 slice store 并
// 合并返回，App.tsx 的 `const { ... } = useAppStore()` 无需改动即可继续工作。
// 组件如需更细粒度的订阅，可直接 import 各 slice store。
//
// 所有 setter 行为与原 useState 一致：直接赋值或 functional updater 均支持。
// setError 保留原有包装逻辑——设置非空 error 时把 errorExpanded 重置为 false。
import { useAnalysisStore } from "./useAnalysisStore";
import { useAuthStore } from "./useAuthStore";
import { useBusyStore } from "./useBusyStore";
import { useChatStore } from "./useChatStore";
import { useConfigStore } from "./useConfigStore";
import { useErrorStore } from "./useErrorStore";
import { useHistoryStore } from "./useHistoryStore";
import { useKeyStore } from "./useKeyStore";
import { useReasoningStore } from "./useReasoningStore";
import { useRetryStore } from "./useRetryStore";
import { useSessionStore } from "./useSessionStore";
import { useTimerStore } from "./useTimerStore";
import { useToolTraceStore } from "./useToolTraceStore";
import { useUIStore } from "./useUIStore";

// 重新导出各 slice store，作为统一的导入入口，方便组件按需迁移。
export {
  useAnalysisStore,
  useAuthStore,
  useBusyStore,
  useChatStore,
  useConfigStore,
  useErrorStore,
  useHistoryStore,
  useKeyStore,
  useReasoningStore,
  useRetryStore,
  useSessionStore,
  useTimerStore,
  useToolTraceStore,
  useUIStore,
};

// 聚合 hook：合并所有 slice store 的状态与 setter，保持与原 useAppStore
// 完全相同的 API。各子 hook 按 React 规则在固定顺序下无条件调用。
export function useAppStore() {
  const auth = useAuthStore();
  const config = useConfigStore();
  const session = useSessionStore();
  const analysis = useAnalysisStore();
  const ui = useUIStore();
  const error = useErrorStore();
  const key = useKeyStore();
  const toolTrace = useToolTraceStore();
  const chat = useChatStore();
  const retry = useRetryStore();
  const busy = useBusyStore();
  const history = useHistoryStore();
  const timer = useTimerStore();
  const reasoning = useReasoningStore();

  return {
    ...auth,
    ...config,
    ...session,
    ...analysis,
    ...ui,
    ...error,
    ...key,
    ...toolTrace,
    ...chat,
    ...retry,
    ...busy,
    ...history,
    ...timer,
    ...reasoning,
  };
}
