import { create } from "zustand";
import type { Session } from "../types";
import type { Updater } from "./types";

// === 会话 slice ===
// lastActiveTab：跨会话切换时持久化用户最后查看的 Tab，
// 切换历史会话后恢复而非每次回到"分析"，减少重复点击。
interface SessionState {
  session: Session | null;
  activeTab: "analysis" | "data" | "artifacts";
  lastActiveTab: "analysis" | "data" | "artifacts";
  setSession: (updater: Updater<Session | null>) => void;
  setActiveTab: (tab: "analysis" | "data" | "artifacts") => void;
  setLastActiveTab: (tab: "analysis" | "data" | "artifacts") => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  session: null,
  activeTab: "analysis",
  lastActiveTab: "analysis",
  setSession: (updater) =>
    set((state) => ({
      session: typeof updater === "function" ? updater(state.session) : updater,
    })),
  setActiveTab: (tab) => set({ activeTab: tab }),
  setLastActiveTab: (tab) => set({ lastActiveTab: tab }),
}));
