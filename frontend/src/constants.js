import remarkGfm from "remark-gfm";
import {
  Activity,
  BarChart3,
  Boxes,
  FileChartColumn,
  FileCheck2,
  FilePlus2,
  FileSpreadsheet,
  Grid3x3,
  Keyboard,
  LineChart,
  Moon,
  Network,
  PieChart,
  ScatterChart,
  Settings2,
  Table2,
} from "lucide-react";

const API_URL = (
  import.meta.env.VITE_API_URL ||
  (import.meta.env.PROD ? window.location.origin : "http://127.0.0.1:8000")
).replace(/\/$/, "");
const ACCESS_TOKEN_KEY = "data-desk-access-token";
const THEME_KEY = "data-desk-theme";
const ACTIVE_ANALYSIS_STATES = new Set(["running", "cancelling"]);

// 快捷键帮助面板的内容定义。集中维护，避免散落在多处 JSX。
// 每条快捷键对应一个真实可用的全局或上下文快捷键（见 App 内的 keydown 监听）。
const HELP_SHORTCUTS = [
  {
    section: "通用",
    items: [
      { keys: ["⌘", "K"], desc: "打开命令面板" },
      { keys: ["?"], desc: "查看键盘快捷键" },
      { keys: ["⌘", "B"], desc: "展开/收起历史会话侧栏" },
      { keys: ["Esc"], desc: "关闭当前弹层" },
    ],
  },
  {
    section: "分析",
    items: [
      { keys: ["⌘", "Enter"], desc: "运行分析任务 / 发送追问" },
      { keys: ["⌘", "."], desc: "停止正在运行的分析" },
      { keys: ["T"], desc: "切换亮色 / 暗色主题" },
      { keys: ["1", "2", "3"], desc: "切换 分析 / 数据 / 产物 三个 Tab" },
    ],
  },
];

// 命令面板可执行的动作。每个动作的 run 接收 App 上下文所需的回调。
// 这里只声明静态元数据，动态回调通过 props 注入。
const COMMAND_ACTIONS = [
  { id: "new-analysis", icon: FilePlus2, title: "新建分析", subtitle: "上传新的数据集", section: "操作" },
  { id: "toggle-theme", icon: Moon, title: "切换主题", subtitle: "亮色 ↔ 暗色", section: "操作" },
  { id: "open-settings", icon: Settings2, title: "打开模型设置", subtitle: "API Key、推理强度", section: "操作" },
  { id: "tab-analysis", icon: BarChart3, title: "切换到分析视图", subtitle: "报告与对话", section: "导航" },
  { id: "tab-data", icon: Table2, title: "切换到数据视图", subtitle: "原始记录预览", section: "导航" },
  { id: "tab-artifacts", icon: FileSpreadsheet, title: "切换到产物视图", subtitle: "图表与导出文件", section: "导航" },
  { id: "show-help", icon: Keyboard, title: "查看键盘快捷键", subtitle: "全部快捷键列表", section: "帮助" },
];

// Module-level constant: remarkPlugins array is recreated on every ReportView
// render if declared inline, which forces ReactMarkdown to re-process the
// markdown AST even when the content hasn't changed. Hoisting it to module
// scope keeps the array identity stable across renders.
const REMARK_PLUGINS = [remarkGfm];

// Maximum upload size hint for client-side validation. The server enforces the
// real limit (max_upload_bytes); this mirror lets us fail fast in the browser
// instead of uploading 100MB before getting a 422.
const MAX_UPLOAD_BYTES_CLIENT = 100 * 1024 * 1024;

const presets = [
  {
    title: "完整分析",
    detail: "质量、统计与图表",
    icon: FileCheck2,
    task: "对当前数据执行完整分析：检查数据质量，采用保守策略完成必要清洗，进行描述统计和关键关系分析，创建最有解释力的图表，并导出清洗后的数据。",
  },
  {
    title: "关键驱动",
    detail: "相关与回归诊断",
    icon: Network,
    task: "识别核心数值指标之间的关系和潜在驱动因素，完成必要清洗、相关分析和适用的回归分析，并生成关系图表。",
  },
  {
    title: "异常诊断",
    detail: "缺失、离群与分布",
    icon: Activity,
    task: "诊断缺失、重复和异常值，分析主要数值字段的分布与离群点，采用谨慎的清洗方式并创建分布图和箱线图。",
  },
];

// 后端工具名 → 中文短标签。PlanPanel 工具调用时间线用它把 inspect_data
// 这种程序化名字翻译成"检查数据"，让用户看懂 ReAct 内部在做什么。
// 缺失映射时回退到原始工具名，保证新工具上线也不会显示 undefined。
const TOOL_LABELS = {
  inspect_data: "检查数据",
  repair_data_format: "修复格式",
  clean_data: "清洗数据",
  transform_data: "派生变换",
  statistical_analysis: "统计分析",
  create_visualization: "生成图表",
  export_data: "导出数据",
};

// React.memo：artifacts 仅在 session 切换或分析完成时变化。memo 让产物
// 列表跳过 task 输入、历史刷新等无关重渲染。onDownload/onPreview 用
// useCallback 稳定身份，否则 memo 失效。
// 根据图表文件名前缀推断图表类型，选择对应的有意义图标。
// 后端 _chart_filename_stem 用中文标签命名（如 "柱状图_1.html"），
// 前端据此匹配 lucide 图标，让用户一眼看出图表类型。
const CHART_ICON_BY_PREFIX = [
  { prefix: "柱状图", Icon: BarChart3, label: "柱状图" },
  { prefix: "折线图", Icon: LineChart, label: "折线图" },
  { prefix: "面积图", Icon: LineChart, label: "面积图" },
  { prefix: "散点矩阵", Icon: Grid3x3, label: "散点矩阵" },
  { prefix: "散点图", Icon: ScatterChart, label: "散点图" },
  { prefix: "三维散点", Icon: ScatterChart, label: "三维散点" },
  { prefix: "直方图", Icon: Activity, label: "直方图" },
  { prefix: "箱线图", Icon: Boxes, label: "箱线图" },
  { prefix: "小提琴图", Icon: Boxes, label: "小提琴图" },
  { prefix: "饼图", Icon: PieChart, label: "饼图" },
  { prefix: "相关性热力图", Icon: Grid3x3, label: "相关性热力图" },
  { prefix: "热力图", Icon: Grid3x3, label: "热力图" },
  { prefix: "旭日图", Icon: Network, label: "旭日图" },
  { prefix: "矩形树图", Icon: Network, label: "矩形树图" },
];

// 预览 HTML LRU 缓存上限：每个图表 HTML 完全自包含（含 Plotly.js ~3.5MB），
// 缓存最近 5 个图表的 HTML，重复打开时秒开，避免重新 fetch + 解析。
const PREVIEW_CACHE_MAX = 5;

function pickChartIcon(name = "") {
  for (const entry of CHART_ICON_BY_PREFIX) {
    if (name.startsWith(entry.prefix)) return entry;
  }
  return { prefix: "", Icon: FileChartColumn, label: "图表" };
}

export {
  API_URL,
  ACCESS_TOKEN_KEY,
  THEME_KEY,
  ACTIVE_ANALYSIS_STATES,
  HELP_SHORTCUTS,
  COMMAND_ACTIONS,
  REMARK_PLUGINS,
  MAX_UPLOAD_BYTES_CLIENT,
  presets,
  TOOL_LABELS,
  CHART_ICON_BY_PREFIX,
  PREVIEW_CACHE_MAX,
  pickChartIcon,
};
