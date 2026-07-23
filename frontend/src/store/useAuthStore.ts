import { create } from "zustand";

// === 认证 slice ===
interface AuthState {
  authRequired: boolean;
  authenticated: boolean;
  authReady: boolean;
  setAuthRequired: (v: boolean) => void;
  setAuthenticated: (v: boolean) => void;
  setAuthReady: (v: boolean) => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  authRequired: false,
  authenticated: false,
  authReady: false,
  setAuthRequired: (v) => set({ authRequired: v }),
  setAuthenticated: (v) => set({ authenticated: v }),
  setAuthReady: (v) => set({ authReady: v }),
}));
