import { create } from "zustand";
import type { Settings } from "../types";

// === 配置 slice ===
interface ConfigState {
  settings: Settings | null;
  apiKey: string;
  effort: string;
  thinking: boolean;
  setSettings: (v: Settings | null) => void;
  setApiKey: (v: string) => void;
  setEffort: (v: string) => void;
  setThinking: (v: boolean) => void;
}

export const useConfigStore = create<ConfigState>((set) => ({
  settings: null,
  apiKey: "",
  effort: "high",
  thinking: true,
  setSettings: (v) => set({ settings: v }),
  setApiKey: (v) => set({ apiKey: v }),
  setEffort: (v) => set({ effort: v }),
  setThinking: (v) => set({ thinking: v }),
}));
