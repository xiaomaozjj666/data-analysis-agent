import { create } from "zustand";
import type { FollowUpMessage } from "../types";
import type { Updater } from "./types";

// === 多轮对话 slice ===
// followUps: [{role, content, streaming?, tools?, error?}]
interface ChatState {
  followUps: FollowUpMessage[];
  followUpInput: string;
  chatRunning: boolean;
  setFollowUps: (updater: Updater<FollowUpMessage[]>) => void;
  setFollowUpInput: (v: string) => void;
  setChatRunning: (v: boolean) => void;
}

export const useChatStore = create<ChatState>((set) => ({
  followUps: [],
  followUpInput: "",
  chatRunning: false,
  setFollowUps: (updater) =>
    set((state) => ({
      followUps: typeof updater === "function" ? updater(state.followUps) : updater,
    })),
  setFollowUpInput: (v) => set({ followUpInput: v }),
  setChatRunning: (v) => set({ chatRunning: v }),
}));
