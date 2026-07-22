import type { ComponentType } from "react";

// SSE 事件类型联合
export type SSEEventType =
  | "started" | "progress" | "validate_dataset" | "plan_analysis"
  | "execute_step" | "replan" | "thinking_chunk" | "finalize"
  | "report_chunk" | "tool_call" | "tool_result" | "complete"
  | "cancelled" | "error" | "heartbeat" | "chat_chunk" | "chat_done"
  | "plan_ready" | "step_progress";

// 分析结果
// 注意：流式渲染阶段会构造部分字段的对象（仅 response + artifacts + plan +
// completed_steps），完整 trace / dataset_profile 由 complete 帧或历史恢复时填充，
// 因此 trace / dataset_profile 设为可选，避免构造中间对象时报类型错误。
export interface AnalysisResult {
  response: string;
  trace?: TraceEntry[];
  artifacts: Artifact[];
  dataset_profile?: DatasetProfile;
  plan: PlanStep[];
  completed_steps: CompletedStep[];
  usage?: TokenUsage;
  reasoning?: string;
}

// 会话（前端使用的完整结构，含 chat / artifacts / preview 等）
export interface Session {
  id: string;
  filename: string;
  preview: Record<string, unknown>[];
  profile: DatasetProfile;
  artifacts: Artifact[];
  analysis_status: string;
  analysis_started_at?: number | null;
  current_task?: string;
  task?: string;
  last_result?: AnalysisResult;
  elapsed_seconds?: number | null;
  has_result?: boolean;
  artifact_count?: number;
  created_at?: number;
  chat?: ChatMessage[];
  // 计划审批：plan_only=true 时后端在 plan_analysis 后结束流，并写入待审阅计划
  pending_plan?: PlanStep[] | null;
  [key: string]: unknown;
}

// 历史会话列表中的轻量条目（/api/sessions 返回）
export interface HistorySessionItem {
  id: string;
  filename: string;
  title?: string;
  analysis_status: string;
  current_task?: string;
  task?: string;
  last_result?: { response?: string } | null;
  has_result?: boolean;
  artifact_count?: number;
  created_at?: number;
  [key: string]: unknown;
}

// Plan Step
export interface PlanStep {
  id: string;
  title: string;
  instruction: string;
  success_criteria: string;
}

// Completed Step
export interface CompletedStep {
  id: string;
  title: string;
  status: "running" | "completed" | "failed" | "skipped";
  summary?: string;
}

// Artifact
export interface Artifact {
  name: string;
  kind: string;
  path: string;
  description: string;
  preview_url?: string;
  download_url?: string;
  size_bytes?: number;
  thumbnail_url?: string;  // 图表缩略图 URL（仅 Plotly 图表提供）
  engine?: "plotly" | "echarts";  // 图表引擎标识
  [key: string]: unknown;
}

// Token Usage
export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

// Trace Entry
export interface TraceEntry {
  type: string;
  name: string;
  detail?: string;
}

// Dataset Profile
export interface DatasetProfile {
  row_count: number;
  column_count: number;
  column_info: ColumnInfo[];
  rows?: number;
  columns?: number;
  load_warnings?: string[];
  [key: string]: unknown;
}

// Column Info
export interface ColumnInfo {
  name: string;
  dtype: string;
  non_null: number;
  unique: number;
  missing?: number;
  [key: string]: unknown;
}

// Tool Trace（PlanPanel 工具调用时间线 + 对话气泡内 chip 共用）
export interface ToolTraceItem {
  call_id: string;
  tool?: string;
  name: string;
  status: "running" | "done" | "error";
  input_preview?: string;
  output_preview?: string;
  duration_ms?: number;
  started_at?: number;
  [key: string]: unknown;
}

// Settings（/api/settings 返回；含若干 UI 展示字段）
export interface Settings {
  provider: string;
  model: string;
  configured: boolean;
  thinking_enabled: boolean;
  reasoning_effort: string;
  max_iterations: number;
  max_plan_steps: number;
  langsmith_tracing?: boolean;
  storage_status?: string;
  warning?: string;
  [key: string]: unknown;
}

// Follow-up Message（多轮对话气泡）
export interface FollowUpMessage {
  role: "user" | "assistant";
  content: string;
  tools?: ToolTraceItem[];
  reasoning?: string;
  usage?: TokenUsage;
  streaming?: boolean;
  error?: string;
  [key: string]: unknown;
}

// 持久化的 chat 消息（session.chat 数组元素）
export interface ChatMessage {
  role: "user" | "assistant";
  content?: string;
  tools?: ToolTraceItem[];
  reasoning?: string;
  usage?: TokenUsage;
  [key: string]: unknown;
}

// Retry offer（断点续跑 / 重新运行）
export interface RetryOffer {
  task: string;
  reason?: "cancelled" | "idle" | "network" | "error" | "ready";
  canResume?: boolean;
  plan?: PlanStep[];
  completed?: CompletedStep[];
  [key: string]: unknown;
}

// lucide-react 图标组件类型
export type IconComponent = ComponentType<{ size?: number; className?: string; fill?: string }>;

// 命令面板动作
export interface CommandAction {
  id: string;
  icon: IconComponent;
  title: string;
  subtitle: string;
  section: string;
}

// 命令面板分组（actions 按字段分组后产生）
export interface CommandActionGroup {
  label: string;
  items: CommandAction[];
}

// 历史会话按时间分组
export interface SessionGroup {
  label: string;
  items: HistorySessionItem[];
}

// 历史状态描述（dot class + 中文标签）
export interface HistoryStatusDescriptor {
  dot: string;
  label: string;
}

// 图标前缀映射条目（CHART_ICON_BY_PREFIX 元素）
export interface ChartIconEntry {
  prefix: string;
  Icon: IconComponent;
  label: string;
}

// 预设任务条目
export interface PresetEntry {
  title: string;
  detail: string;
  icon: IconComponent;
  task: string;
}

// 快捷键条目
export interface ShortcutItem {
  keys: string[];
  desc: string;
}

// 快捷键分组
export interface ShortcutGroup {
  section: string;
  items: ShortcutItem[];
}
