import { create } from "zustand";
import type { HistorySessionItem } from "../types";
import type { Updater } from "./types";

// === 历史 slice ===
// historyError 区分"没数据"和"加载失败"，避免用户误以为数据丢失
interface HistoryState {
  history: HistorySessionItem[];
  historyLoading: boolean;
  historyError: boolean;
  historyExpanded: boolean;
  switchingSessionId: string | null;
  setHistory: (v: HistorySessionItem[]) => void;
  setHistoryLoading: (v: boolean) => void;
  setHistoryError: (v: boolean) => void;
  setHistoryExpanded: (updater: Updater<boolean>) => void;
  setSwitchingSessionId: (v: string | null) => void;
}

export const useHistoryStore = create<HistoryState>((set) => ({
  history: [],
  historyLoading: false,
  historyError: false,
  historyExpanded: false,
  switchingSessionId: null,
  setHistory: (v) => set({ history: v }),
  setHistoryLoading: (v) => set({ historyLoading: v }),
  setHistoryError: (v) => set({ historyError: v }),
  setHistoryExpanded: (updater) =>
    set((state) => ({
      historyExpanded:
        typeof updater === "function" ? updater(state.historyExpanded) : updater,
    })),
  setSwitchingSessionId: (v) => set({ switchingSessionId: v }),
}));
