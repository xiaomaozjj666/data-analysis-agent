import { create } from "zustand";
import type { Updater } from "./types";

// === 错误 slice ===
// setError 包装逻辑：msg 非空时重置 errorExpanded=false，与原 useCallback 行为一致。
interface ErrorState {
  error: string;
  errorExpanded: boolean;
  setError: (msg: string) => void;
  setErrorExpanded: (updater: Updater<boolean>) => void;
}

export const useErrorStore = create<ErrorState>((set) => ({
  error: "",
  errorExpanded: false,
  setError: (msg) =>
    set((state) =>
      msg
        ? { error: msg, errorExpanded: false }
        : { error: msg || "" }
    ),
  setErrorExpanded: (updater) =>
    set((state) => ({
      errorExpanded:
        typeof updater === "function" ? updater(state.errorExpanded) : updater,
    })),
}));
