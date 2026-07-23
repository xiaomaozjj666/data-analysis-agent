import { create } from "zustand";
import type { Updater } from "./types";

// === Key UI slice ===
interface KeyState {
  showKey: boolean;
  keyOpen: boolean;
  setShowKey: (updater: Updater<boolean>) => void;
  setKeyOpen: (updater: Updater<boolean>) => void;
}

export const useKeyStore = create<KeyState>((set) => ({
  showKey: false,
  keyOpen: false,
  setShowKey: (updater) =>
    set((state) => ({
      showKey: typeof updater === "function" ? updater(state.showKey) : updater,
    })),
  setKeyOpen: (updater) =>
    set((state) => ({
      keyOpen: typeof updater === "function" ? updater(state.keyOpen) : updater,
    })),
}));
