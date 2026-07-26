import { create } from "zustand";
import type { Artifact } from "../types";
import type { Updater } from "./types";

// === UI + 命令面板 / 帮助 slice ===
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
  setUploading: (v: boolean) => void;
  setUploadProgress: (v: number | null) => void;
  setPreviewItem: (v: Artifact | null) => void;
  setPreviewHtml: (v: string) => void;
  setPreviewLoading: (v: boolean) => void;
  setPreviewError: (v: string) => void;
  setCommandOpen: (updater: Updater<boolean>) => void;
  setCommandQuery: (v: string) => void;
  setHelpOpen: (updater: Updater<boolean>) => void;
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
}));
