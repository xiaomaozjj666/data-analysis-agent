import React, { useState } from "react";
import {
  Activity,
  Check,
  Circle,
  Clock,
  LoaderCircle,
  Network,
  Rows3,
} from "lucide-react";
import { ToolTraceItem } from "./ToolTraceItem";
import { formatDuration } from "../utils/format";
import type { CompletedStep, PlanStep, ToolTraceItem as ToolTraceItemType } from "../types";

interface PlanPanelProps {
  plan: PlanStep[];
  completed?: CompletedStep[];
  running?: boolean;
  currentNodeTitle?: string;
  elapsedSeconds?: number | null;
  toolTrace?: ToolTraceItemType[];
}

const PlanPanel = React.memo(function PlanPanel({
  plan,
  completed,
  running,
  currentNodeTitle,
  elapsedSeconds,
  toolTrace,
}: PlanPanelProps) {
  const completedIds = new Set((completed || []).map((item) => item.id));
  const doneCount = plan.filter((item) => completedIds.has(item.id)).length;
  const hasTiming = elapsedSeconds != null && elapsedSeconds >= 0;
  const elapsedLabel = running ? "已耗时" : plan.length ? "总耗时" : "";
  const isCompleted = !running && plan.length > 0;
  const [toolsExpanded, setToolsExpanded] = useState(false);
  const allTools = toolTrace || [];
  const TOOL_PREVIEW_COUNT = 8;
  const recentTools = toolsExpanded ? allTools : allTools.slice(-TOOL_PREVIEW_COUNT);
  const hasMoreTools = allTools.length > TOOL_PREVIEW_COUNT;

  return (
    <aside className="plan-panel" aria-label="执行记录">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">执行记录</span>
          <h2>分析进度</h2>
        </div>
        <div className="panel-meta">
          {hasTiming && elapsedLabel && (
            <span
              className={`elapsed-chip ${running ? "is-running" : isCompleted ? "is-done" : ""}`}
              title={running ? "本次分析已运行时长" : "本次分析总耗时"}
            >
              <Clock size={11} />
              <span className="elapsed-label">{elapsedLabel}</span>
              <span className="elapsed-value">{formatDuration(elapsedSeconds ?? null)}</span>
            </span>
          )}
          <span className={`run-state ${running ? "is-running" : isCompleted ? "is-done" : ""}`}>
            {running ? (
              <span className="status-dot" aria-hidden="true" />
            ) : isCompleted ? (
              <Check size={11} />
            ) : (
              <Circle size={7} />
            )}
            {running ? "运行中" : isCompleted ? "已完成" : "待开始"}
          </span>
        </div>
      </div>

      {running && currentNodeTitle && (
        <div className="current-node" role="status" aria-live="polite">
          <LoaderCircle size={12} className="spin" />
          <span>{currentNodeTitle}</span>
        </div>
      )}

      {plan.length > 0 && (
        <div className="progress-line" aria-label={`已完成 ${doneCount}/${plan.length}`}>
          <span style={{ width: `${(doneCount / plan.length) * 100}%` }} />
        </div>
      )}

      {!plan.length ? (
        <div className="plan-empty">
          <Rows3 size={18} />
          <p>运行任务后，这里会显示规划和执行状态。</p>
        </div>
      ) : (
        <ol className="plan-list">
          {plan.map((step, index) => {
            const done = completedIds.has(step.id);
            const active = running && !done && plan.slice(0, index).every((item) => completedIds.has(item.id));
            return (
              <li key={`${step.id}-${index}`} className={done ? "done" : active ? "active" : ""}>
                <span className="step-mark">{done ? <Check size={13} /> : index + 1}</span>
                <div>
                  <strong>{step.title}</strong>
                  <p>{step.success_criteria}</p>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {/* 工具调用时间线：实时展示 ReAct 内部工具调用，让用户看到"正在读取数据
          → 正在清洗 → 正在生成图表"的过程，而不是只看到"正在执行 (2/4)"等 30 秒 */}
      {recentTools.length > 0 && (
        <div className="tool-trace" aria-label="工具调用时间线">
          <div className="tool-trace-label">
            <Activity size={12} />
            <span>工具调用</span>
            <small>{allTools.length}</small>
          </div>
          <ul className="tool-trace-list">
            {recentTools.map((tool) => (
              <ToolTraceItem key={tool.call_id} tool={tool} />
            ))}
          </ul>
          {hasMoreTools && (
            <button
              type="button"
              className="tool-trace-toggle"
              onClick={() => setToolsExpanded((v) => !v)}
              aria-expanded={toolsExpanded}
            >
              {toolsExpanded ? "收起" : `展开全部 (${allTools.length})`}
            </button>
          )}
        </div>
      )}

      <div className="architecture-note">
        <Network size={14} />
        <span>Plan &amp; Execute</span>
        <i />
        <span>ReAct</span>
      </div>
    </aside>
  );
});

export default PlanPanel;
