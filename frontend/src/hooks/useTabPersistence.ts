import { useEffect } from "react";

import type { WorkspaceTab } from "../types";

// Tab 持久化 hook：activeTab 变化时同步到 lastActiveTab，切换历史会话时恢复，
// 避免每次切换都回到"分析" Tab，减少用户重复点击。提取自 App.tsx，行为一致。
function useTabPersistence(
  activeTab: WorkspaceTab,
  setLastActiveTab: (v: WorkspaceTab) => void,
): void {
  useEffect(() => {
    setLastActiveTab(activeTab);
  }, [activeTab, setLastActiveTab]);
}

export default useTabPersistence;
