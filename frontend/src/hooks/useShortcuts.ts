import { useEffect } from "react";
import type { Artifact } from "../types";

// 与 store 的 Tab 类型保持一致（useSessionStore 中 activeTab 的字面量联合）。
type TabId = "analysis" | "data" | "artifacts";

// 与 store 的 Updater<T> 一致：setter 既接受直接值也接受 functional updater。
type SetState<T> = (v: T | ((prev: T) => T)) => void;

interface UseShortcutsDeps {
  // Esc 关闭预览模态框 / 设置面板
  previewItem: Artifact | null;
  keyOpen: boolean;
  closeArtifactPreview: () => void;
  setKeyOpen: SetState<boolean>;
  setApiKey: (v: string) => void;
  // 全局快捷键：Cmd+K / Cmd+B / Cmd+. / ? / T / 1-3
  running: boolean;
  chatRunning: boolean;
  stopAnalysis: () => void;
  stopFollowUp: () => void;
  toggleTheme: () => void;
  setCommandOpen: SetState<boolean>;
  setHistoryExpanded: SetState<boolean>;
  setHelpOpen: SetState<boolean>;
  setActiveTab: (v: TabId) => void;
}

// 键盘快捷键 hook：合并原 App.tsx 中两处 keydown useEffect——
//   1. Esc 关闭预览模态框或设置面板（原 deps [previewItem, keyOpen]）
//   2. 全局快捷键 Cmd+K/Cmd+B/Cmd+./?/T/1-3（原 deps [running, chatRunning, toggleTheme]）
// 行为与原实现完全一致。依赖数组刻意保持原样，不添加 stopAnalysis/stopFollowUp
// 等每次渲染都重建的函数，避免 keydown 监听每帧重绑定（那会改变行为）。
function useShortcuts(deps: UseShortcutsDeps): void {
  const {
    previewItem, keyOpen, closeArtifactPreview, setKeyOpen, setApiKey,
    running, chatRunning, stopAnalysis, stopFollowUp, toggleTheme,
    setCommandOpen, setHistoryExpanded, setHelpOpen, setActiveTab,
  } = deps;

  // Esc 键关闭预览模态框或设置面板（P0-4）。原 effect 仅依赖 [previewItem, keyOpen]；
  // closeArtifactPreview/setKeyOpen/setApiKey 均为稳定引用，省略与原行为一致。
  useEffect(() => {
    if (!previewItem && !keyOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (previewItem) closeArtifactPreview();
      else if (keyOpen) { setKeyOpen(false); setApiKey(""); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewItem, keyOpen]);

  // 全局键盘快捷键：Cmd+K 命令面板、Cmd+B 折叠侧栏、Cmd+. 停止分析、
  // ? 帮助、T 主题、1/2/3 切换 Tab。在 input/textarea/contenteditable 中
  // 按键时跳过单字符快捷键（? T 1 2 3），避免误触发；Cmd 组合键不受此限制。
  // 依赖数组保持原样 [running, chatRunning, toggleTheme]：stopAnalysis/stopFollowUp
  // 每次渲染重建，加入依赖会让监听每帧重绑定并改变闭包刷新时机；它们内部已
  // 读取最新 running/chatRunning，故受控的闭包刷新不会产生行为偏差。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const isTyping = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      const meta = event.metaKey || event.ctrlKey;

      // Cmd+K：打开命令面板（任何焦点下都生效）
      if (meta && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((v) => !v);
        return;
      }
      // Cmd+B：折叠/展开历史侧栏
      if (meta && event.key.toLowerCase() === "b") {
        event.preventDefault();
        setHistoryExpanded((v) => !v);
        return;
      }
      // Cmd+.：停止正在运行的分析
      if (meta && event.key === ".") {
        if (running) { event.preventDefault(); stopAnalysis(); }
        else if (chatRunning) { event.preventDefault(); stopFollowUp(); }
        return;
      }
      // 单字符快捷键：只在非输入态下生效
      if (isTyping) return;
      // ?：打开快捷键帮助面板（Shift+/）
      if (event.key === "?" && !meta) {
        event.preventDefault();
        setHelpOpen((v) => !v);
        return;
      }
      // T：切换主题
      if (event.key.toLowerCase() === "t" && !meta && !event.altKey) {
        event.preventDefault();
        toggleTheme();
        return;
      }
      // 1/2/3：切换分析/数据/产物 Tab
      if (event.key === "1" && !meta) { event.preventDefault(); setActiveTab("analysis"); return; }
      if (event.key === "2" && !meta) { event.preventDefault(); setActiveTab("data"); return; }
      if (event.key === "3" && !meta) { event.preventDefault(); setActiveTab("artifacts"); return; }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // 依赖与原 App.tsx 保持一致；stopAnalysis/stopFollowUp 刻意省略。
  }, [running, chatRunning, toggleTheme]);
}

export default useShortcuts;
