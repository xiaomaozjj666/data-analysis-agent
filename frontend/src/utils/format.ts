import type {
  HistorySessionItem,
  HistoryStatusDescriptor,
  SessionGroup,
} from "../types";

// 把任意字符串尝试格式化为缩进 JSON；若不是合法 JSON 则原样返回。
// 用于工具调用 input_preview / output_preview 的展示：很多 LangChain 工具
// 的输入输出本身就是 JSON 字符串，缩进后可读性大幅提升。
function tryFormatJson(text: unknown): string {
  if (!text) return "";
  const str = String(text);
  const trimmed = str.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return str;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return str;
  }
}

// 简单的 token 用量格式化：< 1000 显示原数，≥ 1000 显示 1.0k 形式
function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

// 把秒数格式化为细颗粒时长：
//   < 60s    → "23 秒"   （直观，避免 "0:23" 显得突兀）
//   < 1h     → "12:34"   （分秒，业界通用格式）
//   ≥ 1h     → "1:23:45" （时:分:秒）
// 参考了 GitHub Actions / Vercel deployment / Linear cycle 的显示风格。
function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0 || !Number.isFinite(seconds)) return "";
  const total = Math.floor(seconds);
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

// 把时间戳（秒）格式化为相对时间（"3 分钟前"），用于历史会话列表。
function formatRelativeTime(timestamp: number | undefined): string {
  if (!timestamp || !Number.isFinite(timestamp)) return "";
  const now = Date.now() / 1000;
  const diff = Math.max(0, now - timestamp);
  if (!Number.isFinite(diff)) return "";
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  // 超过一周显示具体日期。
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

// 把会话按 created_at 分组：今天 / 昨天 / 本周 / 更早。
// 参考 Linear / Notion / VSCode 的历史列表分组惯例——人类记不住具体时间，
// 但能记住"昨天那次分析"，分组让用户快速定位。
function groupSessionsByTime(sessions: HistorySessionItem[] | null | undefined): SessionGroup[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const startOfYesterday = startOfToday - 86400;
  // 本周从周一开始（中国惯例），getDay() 周日是 0 要转成 7。
  const dayOfWeek = now.getDay() === 0 ? 7 : now.getDay();
  const startOfWeek = startOfToday - (dayOfWeek - 1) * 86400;
  const groups: { today: HistorySessionItem[]; yesterday: HistorySessionItem[]; thisWeek: HistorySessionItem[]; earlier: HistorySessionItem[] } = { today: [], yesterday: [], thisWeek: [], earlier: [] };
  for (const item of sessions || []) {
    const ts = item.created_at || 0;
    if (ts >= startOfToday) groups.today.push(item);
    else if (ts >= startOfYesterday) groups.yesterday.push(item);
    else if (ts >= startOfWeek) groups.thisWeek.push(item);
    else groups.earlier.push(item);
  }
  return [
    { label: "今天", items: groups.today },
    { label: "昨天", items: groups.yesterday },
    { label: "本周", items: groups.thisWeek },
    { label: "更早", items: groups.earlier },
  ].filter((group) => group.items.length > 0);
}

// 历史会话状态描述：圆点 class + 中文标签，供 list item 渲染。
function describeHistoryStatus(status: string | undefined): HistoryStatusDescriptor {
  switch (status) {
    case "completed":
      return { dot: "is-done", label: "已完成" };
    case "running":
      return { dot: "is-running", label: "运行中" };
    case "cancelling":
      return { dot: "is-cancelling", label: "取消中" };
    case "cancelled":
      return { dot: "is-cancelled", label: "已取消" };
    case "failed":
      return { dot: "is-failed", label: "失败" };
    default:
      return { dot: "is-idle", label: "未运行" };
  }
}

function formatBytes(value: number = 0): string {
  if (!value) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export {
  tryFormatJson,
  formatTokens,
  wait,
  formatDuration,
  formatRelativeTime,
  groupSessionsByTime,
  describeHistoryStatus,
  formatBytes,
};
