import { create } from "zustand";
import type { TokenUsage } from "../types";
import type { Updater } from "./types";

// === Reasoning / Usage slice ===
// DeepSeek reasoning_content 流式累积；usage 在 complete / chat_done 终态携带。
interface ReasoningState {
  reasoning: string;
  reasoningStreaming: boolean;
  usage: TokenUsage | null;
  setReasoning: (updater: Updater<string>) => void;
  setReasoningStreaming: (v: boolean) => void;
  setUsage: (v: TokenUsage | null) => void;
}

export const useReasoningStore = create<ReasoningState>((set) => ({
  reasoning: "",
  reasoningStreaming: false,
  usage: null,
  setReasoning: (updater) =>
    set((state) => ({
      reasoning: typeof updater === "function" ? updater(state.reasoning) : updater,
    })),
  setReasoningStreaming: (v) => set({ reasoningStreaming: v }),
  setUsage: (v) => set({ usage: v }),
}));
