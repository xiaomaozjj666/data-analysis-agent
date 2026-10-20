import { useRef, type RefObject } from "react";
import { ListChecks, LoaderCircle, Play, Square } from "lucide-react";
import ClickSpark from "./rb/ClickSpark";
import StarBorder from "./rb/StarBorder";
import { formatDuration } from "../utils/format";
import { presets } from "../constants";

interface TaskBoxProps {
  task: string;
  running: boolean;
  stopping: boolean;
  awaitingApproval?: boolean;
  // DeepSeek API Key 是否已配置：未配置时按钮禁用并提示
  configured: boolean;
  // 是否已有会话（审阅计划需要已有数据集）
  hasSession: boolean;
  currentNodeTitle: string;
  elapsedSeconds: number | null;
  onTaskChange: (value: string) => void;
  // 运行分析（无参调用：使用 store 中当前 task）
  onRun: () => void;
  // 先审阅计划再执行（plan_only 流程）
  onRunPlanReview: () => void;
  onStop: () => void;
  onFocusPlan?: () => void;
  taskInputRef: RefObject<HTMLTextAreaElement | null>;
}

// 任务输入框：常驻顶部的分析发起区。
// Enter 换行、⌘/Ctrl+Enter 先生成计划。
export default function TaskBox({
  task, running, stopping, awaitingApproval, configured, hasSession,
  currentNodeTitle, elapsedSeconds,
  onTaskChange, onRun, onRunPlanReview, onStop, onFocusPlan, taskInputRef,
}: TaskBoxProps) {
  // 点击预设时先填入任务文本，下一帧把焦点送回输入框（preset 按钮被
  // 点击后焦点还在按钮上，直接 focus 会被 React 重渲染吞掉）
  const presetFocusTimer = useRef<number | null>(null);
  return (
    <div className={`task-box ${running ? "is-running" : ""} ${awaitingApproval ? "is-awaiting" : ""}`}>
      <div className="task-heading">
        <div>
          <span className="section-kicker">分析任务</span>
          <h2>你想从数据中了解什么？</h2>
        </div>
        {running && (
          <span className="task-running-hint">
            <LoaderCircle className="spin" size={14} />
            {currentNodeTitle ? `正在：${currentNodeTitle}` : "正在分析"}
            {elapsedSeconds != null && ` · ${formatDuration(elapsedSeconds)}`}
          </span>
        )}
      </div>
      {awaitingApproval && (
        <p className="task-review-banner" role="status">
          计划已生成。请在右侧核对步骤，确认后再批准执行。
          {onFocusPlan && (
            <button type="button" onClick={onFocusPlan}>
              查看计划
            </button>
          )}
        </p>
      )}
      <textarea
        ref={taskInputRef}
        value={task}
        onChange={(event) => onTaskChange(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            if (awaitingApproval) return;
            if (!running && task.trim() && configured && hasSession) onRunPlanReview();
          }
        }}
        placeholder="例如：比较各区域销售表现，解释异常波动并生成趋势图（⌘/Ctrl+Enter 生成计划）"
        rows={3}
        disabled={running}
      />
      <div className="task-actions">
        <div className="preset-row">
          {presets.map(({ title, detail, icon: Icon, task: presetTask }) => (
            <button type="button"
              key={title}
              title={configured ? detail : "请先在左下角配置 API Key"}
              onClick={() => {
                onTaskChange(presetTask);
                if (presetFocusTimer.current !== null) window.clearTimeout(presetFocusTimer.current);
                presetFocusTimer.current = window.setTimeout(() => taskInputRef.current?.focus(), 0);
              }}
              disabled={running || !configured}
            >
              <Icon size={14} />{title}
            </button>
          ))}
        </div>
        <div className="task-box-footer">
          <small className="input-hint">Enter 换行 · ⌘/Ctrl+Enter 生成计划</small>
          {running ? (
            <button type="button" className="cancel-button" onClick={onStop} disabled={stopping}>
              <Square size={13} fill="currentColor" />{stopping ? "停止中…" : "停止分析"}
            </button>
          ) : (
            <>
              <button type="button" className="plan-review-button" onClick={onRun} disabled={!task.trim() || !configured || awaitingApproval} title={awaitingApproval ? "请先批准或取消当前计划" : "跳过计划审阅，直接执行（适合已确认过的重复任务）"}>
                <Play size={15} fill="currentColor" />
                跳过审阅
              </button>
              <ClickSpark sparkColor="#5b5bd6" sparkCount={10} sparkLength={16}>
                <StarBorder disabled={!task.trim() || !configured || !hasSession}>
                  <button type="button" className="run-button" onClick={onRunPlanReview} disabled={!task.trim() || !configured || !hasSession} title={awaitingApproval ? "用当前问题重新生成计划" : "先生成计划，审阅后再执行"}>
                    <ListChecks size={15} />{awaitingApproval ? "重新生成" : "生成计划"}
                  </button>
                </StarBorder>
              </ClickSpark>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
