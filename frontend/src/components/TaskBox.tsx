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
  taskInputRef: RefObject<HTMLTextAreaElement | null>;
}

// 任务输入框：常驻顶部的分析发起区。提取自 App.tsx，行为不变——
// 无论切到哪个 tab 都能直接发起新分析；Enter 换行、⌘/Ctrl+Enter 运行。
export default function TaskBox({
  task, running, stopping, configured, hasSession,
  currentNodeTitle, elapsedSeconds,
  onTaskChange, onRun, onRunPlanReview, onStop, taskInputRef,
}: TaskBoxProps) {
  // 点击预设时先填入任务文本，下一帧把焦点送回输入框（preset 按钮被
  // 点击后焦点还在按钮上，直接 focus 会被 React 重渲染吞掉）
  const presetFocusTimer = useRef<number | null>(null);
  return (
    <div className={`task-box ${running ? "is-running" : ""}`}>
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
      <textarea
        ref={taskInputRef}
        value={task}
        onChange={(event) => onTaskChange(event.target.value)}
        onKeyDown={(event) => {
          // Ctrl/Cmd+Enter 快捷提交：与几乎所有聊天/搜索框一致，
          // 避免用户输入完只能移动鼠标点按钮，破坏键盘操作流。
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            if (!running && task.trim() && configured) onRun();
          }
        }}
        placeholder="例如：比较各区域销售表现，解释异常波动并生成趋势图（⌘/Ctrl+Enter 运行）"
        rows={3}
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
        {/* 任务输入提示 + 操作按钮分组：提示紧贴按钮左侧，
            明确告知 Enter 换行、⌘/Ctrl+Enter 运行的键位约定 */}
        <div className="task-box-footer">
          <small className="input-hint">Enter 换行 · ⌘/Ctrl+Enter 运行分析</small>
          {running ? (
            <button type="button" className="cancel-button" onClick={onStop} disabled={stopping}>
              <Square size={13} fill="currentColor" />{stopping ? "停止中…" : "停止分析"}
            </button>
          ) : (
            <>
              <button type="button" className="plan-review-button" onClick={onRunPlanReview} disabled={!task.trim() || !configured || !hasSession} title="先生成计划，审阅后再执行">
                <ListChecks size={15} />
                审阅计划
              </button>
              {/* ClickSpark + StarBorder：点击"运行分析"时品牌色火花迸发，
                  给最重要的操作一个明确的启动反馈（火花层不拦截点击） */}
              <ClickSpark sparkColor="#5b5bd6" sparkCount={10} sparkLength={16}>
                <StarBorder disabled={!task.trim() || !configured}>
                  <button type="button" className="run-button" onClick={onRun} disabled={!task.trim() || !configured}>
                    <Play size={15} fill="currentColor" />运行分析
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
