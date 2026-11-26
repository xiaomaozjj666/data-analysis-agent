import type { ComponentType } from "react";

// SSE 事件类型联合
export type SSEEventType =
  | "started" | "progress" | "validate_dataset" | "plan_analysis"
  | "execute_step" | "replan" | "thinking_chunk" | "finalize"
  | "report_chunk" | "tool_call" | "tool_result" | "complete"
  | "cancelled" | "error" | "heartbeat" | "chat_chunk" | "chat_done"
  | "plan_ready" | "step_progress";

export type WorkspaceTab = "analysis" | "data" | "artifacts" | "audit" | "comments";

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

export interface DemoChart {
  type:
    | "line"
    | "bar"
    | "histogram"
    | "scatter"
    | "heatmap"
    | "donut"
    | "multiline"
    | "stacked"
    | "area"
    | "treemap"
    | "funnel"
    | "radar"
    | "gauge"
    | "box"
    | "bubble"
    | "waterfall";
  title: string;
  subtitle?: string;
  unit?: string;
  points?: { label: string; value: number }[];
  items?: {
    label: string;
    value: number;
    x?: number;
    y?: number;
    size?: number;
    kind?: "total" | "delta";
  }[];
  raw_items?: { label: string; value: number }[] | null;
  excluded?: number;
  dimension?: string;
  measure?: string;
  labels?: string[];
  series?: { name: string; points?: number[]; values?: number[]; dash?: boolean }[];
  indicators?: string[];
  groups?: {
    label: string;
    min: number;
    q1: number;
    median: number;
    q3: number;
    max: number;
  }[];
  value?: number;
  target?: number;
  x_label?: string;
  y_label?: string;
  samples?: [number, number][];
  outliers?: [number, number][];
  cols?: number;
  rows?: number;
  values?: number[][];
}

export interface DemoInsights {
  kind: string;
  headline: string;
  subline: string;
  kpis: { label: string; value: string; note?: string; unit?: string }[];
  charts: DemoChart[];
  findings: string[];
  scope?: {
    rows: number;
    visible: number;
    rls_column?: string | null;
    pii_columns?: string[] | null;
    principal_id?: string | null;
  };
}

export interface DemoPayload {
  kind: string;
  title: string;
  blurb: string;
  highlight: string;
  open_audit: boolean;
  insights?: DemoInsights | null;
  principals?: { id: string; label: string; policy: Record<string, unknown> }[];
}

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
  // 候选数据集：join 等工具产出、尚未采纳为活动数据集的派生表
  candidate_datasets?: CandidateDataset[];
  // 会话级分析内核快照：计划/步骤/当前节点/工具时间线，用于切换会话后恢复面板
  kernel?: SessionKernel;
  demo?: DemoPayload;
  [key: string]: unknown;
}

// 会话级分析内核快照（后端 session_kernel.SessionKernel）
export interface SessionKernel {
  objective: string;
  plan: PlanStep[];
  plan_source: string;
  completed_steps: CompletedStep[];
  current_node: string;
  status: string;
  tool_trace: ToolTraceItem[];
  updated_at?: number | null;
}

// 定时任务（/api/automation/tasks 概览条目）
export interface AutomationTask {
  id: string;
  name: string;
  enabled: boolean;
  schedule: { interval_minutes?: number; daily_at?: string };
  due: boolean;
  next_due_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_alerts: number;
  last_alert_headline: string | null;
  last_error: string | null;
  last_webhook_status?: string | null;
  next_retry_at?: string | null;
  state_corrupt: boolean;
}

export interface AutomationOverview {
  generated_at: string;
  summary: { total: number; enabled: number; due: number; alerts: number; errors: number };
  tasks: AutomationTask[];
}

// 定时任务运行历史行（history.jsonl 的一条记录）
export interface AutomationHistoryRow {
  [key: string]: unknown;
}

export interface CandidateDataset {
  name: string;
  rows: number;
  note?: string;
  created_at?: string;
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
  // /api/sessions 列表可能省略 demo；即使返回也可能只是部分字段。
  // 打开会话时以 GET /api/sessions/{id} 返回的完整 demo 元数据为准。
  demo?: Partial<DemoPayload>;
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

// 步骤内进度（后端 step_progress 事件）：百分比 / 工具调用数 / 提示文案，
// stepIndex/totalSteps 为复合进度上下文（"步骤 2/4"），旧事件无此字段时隐藏前缀。
export interface StepProgress {
  progress: number;
  toolCalls: number;
  message: string;
  stepIndex?: number;
  totalSteps?: number;
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
  // 执行预算（只读信息，来自 /api/settings）：单模型调用上限、计划步数上限、
  // 单步工具调用上限。用于设置面板展示"一轮分析为什么这么久"。
  max_iterations: number;
  max_plan_steps: number;
  max_tool_calls_per_step: number;
  langsmith_tracing?: boolean;
  langsmith_status?: "configured" | "missing_key" | "key_unconfirmed" | "local" | "disabled";
  langsmith_project?: string | null;
  storage_status?: string;
  warning?: string;
  // 服务端上传大小上限（字节）；旧后端无此字段时前端回退本地默认值
  max_upload_bytes?: number;
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
