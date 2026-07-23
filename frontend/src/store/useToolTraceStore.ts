import { create } from "zustand";
import type { ToolTraceItem } from "../types";
import type { Updater } from "./types";

// === Tool trace slice ===
// 工具调用时间线：tool_call 追加，tool_result 按 call_id 更新。
interface ToolTraceState {
  toolTrace: ToolTraceItem[];
  setToolTrace: (updater: Updater<ToolTraceItem[]>) => void;
}

export const useToolTraceStore = create<ToolTraceState>((set) => ({
  toolTrace: [],
  setToolTrace: (updater) =>
    set((state) => ({
      toolTrace: typeof updater === "function" ? updater(state.toolTrace) : updater,
    })),
}));
