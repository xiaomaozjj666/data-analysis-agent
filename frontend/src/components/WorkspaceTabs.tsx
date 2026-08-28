import { BarChart3, FileSpreadsheet, Table2 } from "lucide-react";

export type WorkspaceTab = "analysis" | "data" | "artifacts";

interface WorkspaceTabsProps {
  activeTab: WorkspaceTab;
  artifactCount: number;
  onSelectTab: (tab: WorkspaceTab) => void;
}

// 工作区三视图切换（分析 / 数据 / 产物）。提取自 App.tsx 的 tabs nav，
// 行为不变：ARIA tablist 语义 + roving tabindex + 左右箭头循环切换，
// 屏幕阅读器可正确识别。
export default function WorkspaceTabs({ activeTab, artifactCount, onSelectTab }: WorkspaceTabsProps) {
  const tabs: { id: WorkspaceTab; label: string; icon: typeof BarChart3; focusNext: WorkspaceTab; focusPrev: WorkspaceTab }[] = [
    { id: "analysis", label: "分析", icon: BarChart3, focusNext: "data", focusPrev: "artifacts" },
    { id: "data", label: "数据", icon: Table2, focusNext: "artifacts", focusPrev: "analysis" },
    { id: "artifacts", label: "产物", icon: FileSpreadsheet, focusNext: "analysis", focusPrev: "data" },
  ];
  return (
    <nav className="tabs" role="tablist" aria-label="工作区视图">
      {tabs.map(({ id, label, icon: Icon, focusNext, focusPrev }) => (
        <button type="button"
          key={id}
          id={`tab-${id}`}
          role="tab"
          aria-selected={activeTab === id}
          aria-controls={`tabpanel-${id}`}
          tabIndex={activeTab === id ? 0 : -1}
          className={activeTab === id ? "active" : ""}
          onClick={() => onSelectTab(id)}
          onKeyDown={(e) => {
            if (e.key === "ArrowRight") { e.preventDefault(); document.getElementById(`tab-${focusNext}`)?.focus(); }
            else if (e.key === "ArrowLeft") { e.preventDefault(); document.getElementById(`tab-${focusPrev}`)?.focus(); }
          }}
        >
          <Icon size={15} />{label}
          {id === "artifacts" && <span>{artifactCount}</span>}
        </button>
      ))}
    </nav>
  );
}
