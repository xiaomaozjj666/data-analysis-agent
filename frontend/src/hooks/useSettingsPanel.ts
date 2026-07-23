import { useEffect } from "react";

// 设置面板点击外部关闭 hook：mousedown 落在 settings-form / provider-block
// 之外时收起面板。mousedown 而非 click，在文本选区拖拽到面板外释放时不会
// 误触关闭。提取自 App.tsx，行为与原 useEffect 完全一致。
function useSettingsPanel(
  keyOpen: boolean,
  setKeyOpen: (v: boolean) => void,
): void {
  useEffect(() => {
    if (!keyOpen) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest(".settings-form") && !target.closest(".provider-block")) {
        setKeyOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [keyOpen, setKeyOpen]);
}

export default useSettingsPanel;
