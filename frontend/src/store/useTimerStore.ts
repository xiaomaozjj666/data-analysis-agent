import { create } from "zustand";

// === 计时 slice ===
// running 时由 setInterval 每秒刷新；非 running 时由 session.elapsed_seconds 计算一次性赋值。
interface TimerState {
  elapsedSeconds: number | null;
  setElapsedSeconds: (v: number | null) => void;
}

export const useTimerStore = create<TimerState>((set) => ({
  elapsedSeconds: null,
  setElapsedSeconds: (v) => set({ elapsedSeconds: v }),
}));
