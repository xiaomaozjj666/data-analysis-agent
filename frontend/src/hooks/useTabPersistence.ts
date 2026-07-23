import { useEffect } from "react";

// 与 store 的 Tab 类型保持一致（useSessionStore 中 activeTab 的字面量联合）。
type TabId = "analysis" | "data" | "artifacts";

// Tab 持久化 hook：activeTab 变化时同步到 lastActiveTab，切换历史会话时恢复，
// 避免每次切换都回到"分析" Tab，减少用户重复点击。提取自 App.tsx，行为一致。
function useTabPersistence(
  activeTab: TabId,
  setLastActiveTab: (v: TabId) => void,
): void {
  useEffect(() => {
    setLastActiveTab(activeTab);
  }, [activeTab, setLastActiveTab]);
}

export default useTabPersistence;
