import React, { useEffect, useState } from "react";
import {
  Activity,
  Check,
  ChevronDown,
  ChevronUp,
  Circle,
  Clock,
  LoaderCircle,
  Network,
  Rows3,
  Trash2,
} from "lucide-react";
import { ToolTraceItem } from "./ToolTraceItem";
import { formatDuration } from "../utils/format";
import type { CompletedStep, PlanStep, StepProgress, ToolTraceItem as ToolTraceItemType } from "../types";

interface PlanPanelProps {
  plan: PlanStep[];
  completed?: CompletedStep[];
  running?: boolean;
  currentNodeTitle?: string;
  elapsedSeconds?: number | null;
  toolTrace?: ToolTraceItemType[];
  // Batch 4：计划审批 / 步骤进度 / 重跑入口
  awaitingApproval?: boolean;
  stepProgress?: StepProgress | null;
  onApprovePlan?: (editedPlan: PlanStep[]) => void;
  onCancelApproval?: () => void;
  onRerunFromStep?: (index: number) => void;
}

const PlanPanel = React.memo(function PlanPanel({
  plan,
  completed,
  running,
  currentNodeTitle,
  elapsedSeconds,
  toolTrace,
  awaitingApproval,
  stepProgress,
  onApprovePlan,
  onCancelApproval,
  onRerunFromStep,
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

  // === 计划审批：本地编辑态 ===
  // awaitingApproval 变为 true 时初始化 editedPlan（深拷贝避免污染 store），
  // 用户可在 PlanPanel 内联编辑步骤标题/成功标准，删除或上下移动步骤。
  const [editMode, setEditMode] = useState(false);
  const [editedPlan, setEditedPlan] = useState<PlanStep[]>([]);

  useEffect(() => {
    if (awaitingApproval) {
      // 深拷贝 plan，避免直接 mutate store 中的引用
      setEditedPlan(plan.map((step) => ({ ...step })));
      setEditMode(true);
    } else {
      setEditMode(false);
      setEditedPlan([]);
    }
  }, [awaitingApproval, plan]);

  // 更新步骤字段（title / success_criteria）
  const updateStep = (index: number, field: "title" | "success_criteria", value: string) => {
    setEditedPlan((prev) => prev.map((step, i) => (i === index ? { ...step, [field]: value } : step)));
  };

  // 删除步骤
  const deleteStep = (index: number) => {
    setEditedPlan((prev) => prev.filter((_, i) => i !== index));
  };

  // 移动步骤（up: -1, down: +1）
  const moveStep = (index: number, direction: -1 | 1) => {
    setEditedPlan((prev) => {
      const target = index + direction;
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  // 批准当前编辑后的计划并触发执行
  const approvePlan = () => {
    if (!onApprovePlan) return;
    onApprovePlan(editedPlan);
  };

  // 取消审批：丢弃编辑并通知上层
  const cancelApproval = () => {
    if (!onCancelApproval) return;
    onCancelApproval();
  };

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

      {/* 步骤内进度：后端在执行单步时推送 step_progress，
          展示"步骤 2/4 · 第 3 次工具调用 · 预估 60%"的复合进度，
          让用户同时看到全局位置与步骤内位置；旧事件无步骤序号时隐藏前缀 */}
      {running && stepProgress && (
        <div className="step-progress" aria-live="polite">
          <div className="step-progress-bar">
            <span style={{ width: `${stepProgress.progress}%` }} />
          </div>
          <small>
            {(stepProgress.stepIndex ?? 0) > 0 && (stepProgress.totalSteps ?? 0) > 0 && (
              <strong className="step-progress-step">步骤 {stepProgress.stepIndex}/{stepProgress.totalSteps} · </strong>
            )}
            {stepProgress.message}
            {" · 预估 "}
            {stepProgress.progress}%
          </small>
        </div>
      )}

      {plan.length > 0 && !editMode && (
        <div className="progress-line" aria-label={`已完成 ${doneCount}/${plan.length}`}>
          <span style={{ width: `${(doneCount / plan.length) * 100}%` }} />
        </div>
      )}

      {!plan.length ? (
        <div className="plan-empty">
          <Rows3 size={18} />
          <p>运行任务后，这里会显示规划和执行状态。</p>
        </div>
      ) : editMode ? (
        // === 编辑模式：每步可改标题/成功标准，可删除、上下移动 ===
        <ol className={`plan-list ${editMode ? "editable" : ""}`}>
          {editedPlan.map((step, index) => (
            <li key={`${step.id}-${index}`}>
              <span className="step-mark">{index + 1}</span>
              <div className="step-edit-body">
                <label className="step-edit-field">
                  <span>步骤标题</span>
                  <input
                    type="text"
                    value={step.title}
                    onChange={(e) => updateStep(index, "title", e.target.value)}
                    placeholder="例如：检查缺失值"
                  />
                </label>
                <label className="step-edit-field">
                  <span>成功标准</span>
                  <input
                    type="text"
                    value={step.success_criteria}
                    onChange={(e) => updateStep(index, "success_criteria", e.target.value)}
                    placeholder="例如：输出缺失统计表"
                  />
                </label>
                <div className="step-edit-controls">
                  <button
                    type="button"
                    title="上移"
                    onClick={() => moveStep(index, -1)}
                    disabled={index === 0}
                  >
                    <ChevronUp size={12} />
                  </button>
                  <button
                    type="button"
                    title="下移"
                    onClick={() => moveStep(index, 1)}
                    disabled={index === editedPlan.length - 1}
                  >
                    <ChevronDown size={12} />
                  </button>
                  <button type="button" title="删除" onClick={() => deleteStep(index)}>
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <ol className="plan-list">
          {plan.map((step, index) => {
            const done = completedIds.has(step.id);
            const active = running && !done && plan.slice(0, index).every((item) => completedIds.has(item.id));
            // 已完成步骤允许从此处重跑：截断 completed 至 index 之前的步骤
            const canRerun = !!onRerunFromStep && done && !running && !awaitingApproval;
            return (
              <li key={`${step.id}-${index}`} className={done ? "done" : active ? "active" : ""}>
                <span className="step-mark">{done ? <Check size={13} /> : index + 1}</span>
                <div>
                  <strong>{step.title}</strong>
                  <p>{step.success_criteria}</p>
                </div>
                {canRerun && (
                  <button
                    type="button"
                    className="step-rerun"
                    onClick={() => onRerunFromStep?.(index)}
                    title={`从此步骤重新执行（保留前 ${index} 步结果）`}
                  >
                    重跑从此处
                  </button>
                )}
              </li>
            );
          })}
        </ol>
      )}

      {/* 审批操作区：批准执行 / 取消 */}
      {editMode && (
        <div className="plan-approval-actions">
          <button type="button" className="cancel-btn" onClick={cancelApproval}>
            取消
          </button>
          <button type="button" className="approve-btn" onClick={approvePlan} disabled={editedPlan.length === 0}>
            批准并执行
          </button>
        </div>
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
