import { create } from "zustand";
import type { RetryOffer } from "../types";

// === 重试 slice ===
interface RetryState {
  retryOffer: RetryOffer | null;
  retryChecking: boolean;
  setRetryOffer: (v: RetryOffer | null) => void;
  setRetryChecking: (v: boolean) => void;
}

export const useRetryStore = create<RetryState>((set) => ({
  retryOffer: null,
  retryChecking: false,
  setRetryOffer: (v) => set({ retryOffer: v }),
  setRetryChecking: (v) => set({ retryChecking: v }),
}));
