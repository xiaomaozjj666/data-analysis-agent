import { create } from "zustand";
import type { Artifact } from "../types";
import type { Updater } from "./types";

export type Locale = "zh-CN" | "en-US";

const LOCALE_KEY = "data-desk-locale";

function initialLocale(): Locale {
  try {
    const saved = window.localStorage.getItem(LOCALE_KEY);
    if (saved === "zh-CN" || saved === "en-US") return saved;
  } catch {
    // 隐私模式/禁用存储时回退默认中文。
  }
  return "zh-CN";
}

// === UI + 命令面板 / 帮助 / 语言 slice ===
// Cmd+K 弹层 + ? 快捷键帮助弹层状态
interface UIState {
  uploading: boolean;
  // 上传进度百分比（0-100）；null 表示当前没有可展示的进度
  //（未在上传，或请求体长度不可计）。
  uploadProgress: number | null;
  previewItem: Artifact | null;
  previewHtml: string;
  previewLoading: boolean;
  previewError: string;
  commandOpen: boolean;
  commandQuery: string;
  helpOpen: boolean;
  // 定时任务面板开关（全局能力，不依赖当前是否有会话）
  schedulesOpen: boolean;
  // 界面语言：默认中文，localStorage 记忆
  locale: Locale;
  setUploading: (v: boolean) => void;
  setUploadProgress: (v: number | null) => void;
  setPreviewItem: (v: Artifact | null) => void;
  setPreviewHtml: (v: string) => void;
  setPreviewLoading: (v: boolean) => void;
  setPreviewError: (v: string) => void;
  setCommandOpen: (updater: Updater<boolean>) => void;
  setCommandQuery: (v: string) => void;
  setHelpOpen: (updater: Updater<boolean>) => void;
  setSchedulesOpen: (updater: Updater<boolean>) => void;
  setLocale: (locale: Locale) => void;
}

export const useUIStore = create<UIState>((set) => ({
  uploading: false,
  uploadProgress: null,
  previewItem: null,
  previewHtml: "",
  previewLoading: false,
  previewError: "",
  commandOpen: false,
  commandQuery: "",
  helpOpen: false,
  schedulesOpen: false,
  locale: typeof window === "undefined" ? "zh-CN" : initialLocale(),
  setUploading: (v) => set({ uploading: v }),
  setUploadProgress: (v) => set({ uploadProgress: v }),
  setPreviewItem: (v) => set({ previewItem: v }),
  setPreviewHtml: (v) => set({ previewHtml: v }),
  setPreviewLoading: (v) => set({ previewLoading: v }),
  setPreviewError: (v) => set({ previewError: v }),
  setCommandOpen: (updater) =>
    set((state) => ({
      commandOpen:
        typeof updater === "function" ? updater(state.commandOpen) : updater,
    })),
  setCommandQuery: (v) => set({ commandQuery: v }),
  setHelpOpen: (updater) =>
    set((state) => ({
      helpOpen:
        typeof updater === "function" ? updater(state.helpOpen) : updater,
    })),
  setSchedulesOpen: (updater) =>
    set((state) => ({
      schedulesOpen:
        typeof updater === "function" ? updater(state.schedulesOpen) : updater,
    })),
  setLocale: (locale) => {
    try {
      window.localStorage.setItem(LOCALE_KEY, locale);
    } catch {
      // 存储不可用时仅当前会话生效。
    }
    document.documentElement.lang = locale;
    set({ locale });
  },
}));
