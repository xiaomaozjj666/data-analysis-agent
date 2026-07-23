import { create } from "zustand";

// === Busy slice ===
// savingSettings / stopping：防止连点发多请求
interface BusyState {
  savingSettings: boolean;
  stopping: boolean;
  setSavingSettings: (v: boolean) => void;
  setStopping: (v: boolean) => void;
}

export const useBusyStore = create<BusyState>((set) => ({
  savingSettings: false,
  stopping: false,
  setSavingSettings: (v) => set({ savingSettings: v }),
  setStopping: (v) => set({ stopping: v }),
}));
