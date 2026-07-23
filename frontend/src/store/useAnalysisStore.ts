import { create } from "zustand";
import type { AnalysisResult, CompletedStep, PlanStep } from "../types";
import type { Updater } from "./types";

// === 分析任务 + 计划审批 slice ===
// plan_only=true 时后端在 plan_analysis 后结束流并推送 plan_ready 事件，
// 前端进入 awaitingApproval 模式：PlanPanel 显示编辑/删除/重排控件，
// 用户审批后调用 startAnalysis(editedPlan, completed_steps: []) 继续执行。
// stepProgress：执行中后端推送的当前步骤进度（百分比 / 工具调用数 / 提示）。
interface AnalysisState {
  task: string;
  plan: PlanStep[];
  completed: CompletedStep[];
  result: AnalysisResult | null;
  running: boolean;
  awaitingApproval: boolean;
  pendingObjective: string;
  stepProgress: { progress: number; toolCalls: number; message: string } | null;
  currentNodeTitle: string;
  setTask: (v: string) => void;
  setPlan: (v: PlanStep[]) => void;
  setCompleted: (v: CompletedStep[]) => void;
  setResult: (updater: Updater<AnalysisResult | null>) => void;
  setRunning: (v: boolean) => void;
  setAwaitingApproval: (v: boolean) => void;
  setPendingObjective: (v: string) => void;
  setStepProgress: (v: { progress: number; toolCalls: number; message: string } | null) => void;
  setCurrentNodeTitle: (updater: Updater<string>) => void;
}

export const useAnalysisStore = create<AnalysisState>((set) => ({
  task: "",
  plan: [],
  completed: [],
  result: null,
  running: false,
  awaitingApproval: false,
  pendingObjective: "",
  stepProgress: null,
  currentNodeTitle: "",
  setTask: (v) => set({ task: v }),
  setPlan: (v) => set({ plan: v }),
  setCompleted: (v) => set({ completed: v }),
  setResult: (updater) =>
    set((state) => ({
      result: typeof updater === "function" ? updater(state.result) : updater,
    })),
  setRunning: (v) => set({ running: v }),
  setAwaitingApproval: (v) => set({ awaitingApproval: v }),
  setPendingObjective: (v) => set({ pendingObjective: v }),
  setStepProgress: (v) => set({ stepProgress: v }),
  setCurrentNodeTitle: (updater) =>
    set((state) => ({
      currentNodeTitle:
        typeof updater === "function" ? updater(state.currentNodeTitle) : updater,
    })),
}));
