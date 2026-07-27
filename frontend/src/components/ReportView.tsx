import React, { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import {
  Check,
  ChevronDown,
  Download,
  FileChartColumn,
  FileCode,
  FileSpreadsheet,
  FileText,
  LayoutDashboard,
  LoaderCircle,
  Printer,
} from "lucide-react";
import { API_URL, REMARK_PLUGINS } from "../constants";
import { describeApiError, requestHeaders } from "../utils/api";
import { markdownComponents, ReasoningBlock, UsageChip } from "./ReportParts";
import ShinyText from "./rb/ShinyText";
import GradientText from "./rb/GradientText";
import type { AnalysisResult, Artifact, TokenUsage } from "../types";

interface ReportViewProps {
  result: AnalysisResult;
  streaming?: boolean;
  onPreview?: (item: Artifact) => void;
  artifacts?: Artifact[] | null;
  reasoning?: string;
  reasoningStreaming?: boolean;
  theme?: "light" | "dark";
  usage?: TokenUsage | null;
  // 当前会话 id：用于向后端请求数据画像仪表盘导出（无会话时隐藏菜单项）
  sessionId?: string | null;
}

// 从 React 子节点提取纯文本：用于给标题生成稳定的 id
function extractText(children: React.ReactNode): string {
  let text = "";
  React.Children.forEach(children, (child) => {
    if (typeof child === "string") text += child;
    else if (typeof child === "number") text += String(child);
    else if (React.isValidElement(child)) {
      text += extractText((child.props as { children?: React.ReactNode }).children);
    }
  });
  return text;
}

// 标题文本转 slug：保留中文/字母/数字，空白转连字符，用作锚点 id
function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^\u4e00-\u9fa5a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

// 转义 HTML 特殊字符：导出 HTML 时防止报告原文里的 <>&" 破坏结构
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 极简 markdown→HTML：覆盖常见语法（标题/粗斜体/代码块/行内代码/列表/表格/
// 链接/引用/分隔线/段落）。仅供离线导出 HTML 使用，不追求 GFM 完整性。
// marked 等库未引入，这里手写最小实现，避免新增依赖。
function markdownToHtml(md: string): string {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let i = 0;

  // 内联标记：先转义，再用占位符隔离行内代码与链接（保护 URL 不被粗斜体破坏），
  // 最后处理粗体/删除线/斜体并还原占位符。
  const inline = (text: string): string => {
    const codes: string[] = [];
    const links: string[] = [];
    let s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, (_m, c: string) => {
      codes.push(c);
      return `\u0000C${codes.length - 1}\u0000`;
    });
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label: string, url: string) => {
      links.push(`<a href="${url}">${label}</a>`);
      return `\u0000L${links.length - 1}\u0000`;
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/(^|[^\w*])\*([^*]+)\*(?![\w*])/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^\w_])_([^_]+)_(?![\w_])/g, "$1<em>$2</em>");
    s = s.replace(/\u0000L(\d+)\u0000/g, (_m, idx: string) => links[Number(idx)]);
    s = s.replace(/\u0000C(\d+)\u0000/g, (_m, idx: string) => `<code>${codes[Number(idx)]}</code>`);
    return s;
  };

  const isTableSep = (line: string) =>
    /^\s*\|?[\s:|-]+$/.test(line) && line.includes("-") && line.includes("|");
  const isBlank = (line: string) => /^\s*$/.test(line);
  const isHeading = (line: string) => /^#{1,6}\s+/.test(line);
  const isUlItem = (line: string) => /^\s*[-*+]\s+/.test(line);
  const isOlItem = (line: string) => /^\s*\d+\.\s+/.test(line);
  const isQuote = (line: string) => /^>\s?/.test(line);
  const isCodeFence = (line: string) => /^```/.test(line);
  const isHr = (line: string) => /^\s*([-*_])\1{2,}\s*$/.test(line);

  // 段落结束条件：空行 / 任一结构块起点 / 表格首行
  const breakParagraph = (idx: number) =>
    idx >= lines.length ||
    isBlank(lines[idx]) ||
    isHeading(lines[idx]) ||
    isUlItem(lines[idx]) ||
    isOlItem(lines[idx]) ||
    isQuote(lines[idx]) ||
    isCodeFence(lines[idx]) ||
    isHr(lines[idx]) ||
    (/\|/.test(lines[idx]) && idx + 1 < lines.length && isTableSep(lines[idx + 1]));

  while (i < lines.length) {
    const line = lines[i];

    // 围栏代码块
    if (isCodeFence(line)) {
      const lang = line.replace(/^```/, "").trim();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !isCodeFence(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // 跳过结束围栏
      const cls = lang ? ` class="language-${escapeHtml(lang)}"` : "";
      out.push(`<pre><code${cls}>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // 标题
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    if (isHr(line)) { out.push("<hr />"); i++; continue; }

    // GFM 表格：当前行含 | 且下一行是分隔行
    if (/\|/.test(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const splitRow = (r: string) =>
        r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && /\|/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const thead = `<thead><tr>${header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`;
      const tbody = `<tbody>${rows
        .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
        .join("")}</tbody>`;
      out.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    // 引用块
    if (isQuote(line)) {
      const buf: string[] = [];
      while (i < lines.length && isQuote(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    // 无序列表
    if (isUlItem(line)) {
      const items: string[] = [];
      while (i < lines.length && isUlItem(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*[-*+]\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // 有序列表
    if (isOlItem(line)) {
      const items: string[] = [];
      while (i < lines.length && isOlItem(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    if (isBlank(line)) { i++; continue; }

    // 段落：连续非结构行合并
    const buf: string[] = [];
    while (!breakParagraph(i)) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }

  return out.join("\n");
}

// 从报告首个 H1 推导文件名，回退"分析报告"；去除文件名非法字符
function deriveFilename(md: string): string {
  const m = /^#\s+(.+)$/m.exec(md.trim());
  let title = m ? m[1].trim() : "分析报告";
  title = title.replace(/[\\/:*?"<>|]/g, "").replace(/\s+/g, " ").trim();
  return title || "分析报告";
}

// 导出 HTML 的内联样式：衬线正文 + 800px 正文宽 + 打印友好的代码块/表格
const REPORT_HTML_STYLE = `
*, *::before, *::after { box-sizing: border-box; }
body { font-family: Georgia, "Noto Serif SC", "Songti SC", "Source Han Serif SC", serif; max-width: 800px; margin: 40px auto; padding: 0 24px; color: #1a1d29; line-height: 1.75; background: #fff; }
h1 { font-size: 28px; margin: 0 0 16px; line-height: 1.3; font-weight: 700; }
h2 { font-size: 22px; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 1px solid #eef0f3; font-weight: 700; }
h3 { font-size: 18px; margin: 24px 0 10px; font-weight: 700; }
h4 { font-size: 15px; margin: 18px 0 8px; font-weight: 600; }
h5, h6 { font-size: 13px; margin: 14px 0 6px; color: #6b7280; font-weight: 600; }
p { margin: 12px 0; }
ul, ol { margin: 12px 0; padding-left: 26px; }
li { margin: 4px 0; }
code { font-family: "IBM Plex Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; padding: 2px 6px; background: #f4f5f7; border-radius: 4px; }
pre { background: #f4f5f7; border: 1px solid #eef0f3; border-radius: 8px; padding: 14px 16px; overflow: auto; margin: 16px 0; }
pre code { background: transparent; padding: 0; font-size: 13px; line-height: 1.55; }
a { color: #5b5bd6; text-decoration: none; border-bottom: 1px solid #ddd; }
blockquote { margin: 16px 0; padding: 6px 0 6px 16px; border-left: 3px solid #5b5bd6; color: #6b7280; background: #f4f5f7; border-radius: 0 4px 4px 0; }
blockquote p { margin: 4px 0; }
hr { border: 0; border-top: 1px solid #eef0f3; margin: 24px 0; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }
th, td { border: 1px solid #e4e6ea; padding: 8px 12px; text-align: left; vertical-align: top; }
thead th { background: #f4f5f7; font-weight: 700; }
tbody tr:nth-child(even) td { background: #fafbfc; }
img { max-width: 100%; height: auto; }
/* 暗色适配：导出的 HTML 跟随系统主题，深底亮字避免晚间阅读刺眼 */
@media (prefers-color-scheme: dark) {
  body { background: #16171a; color: #e8eaed; }
  h2 { border-bottom-color: #2e2f33; }
  h5, h6 { color: #9aa0a6; }
  code, pre, blockquote { background: #242528; }
  pre { border-color: #2e2f33; }
  a { color: #8a8af2; border-bottom-color: #3a3b40; }
  blockquote { border-left-color: #8a8af2; color: #9aa0a6; }
  hr { border-top-color: #2e2f33; }
  th, td { border-color: #2e2f33; }
  thead th { background: #242528; }
  tbody tr:nth-child(even) td { background: #1d1e21; }
}
@media print {
  /* 打印始终用亮色：深底打印耗墨且黑白打印机下对比度差 */
  body { margin: 0; max-width: none; padding: 0 16px; background: #fff; color: #1a1d29; }
  pre, blockquote, table, tr { page-break-inside: avoid; }
  a { border-bottom: 0; }
}
`;

interface HeadingItem {
  id: string;
  text: string;
  level: 2 | 3;
}

// 报告区独立组件：负责渲染最终 Markdown 报告，并附带时间戳、复制按钮、
// 长 report-body 展开/收起。把这块从主组件拆出来也让 props 校验更清晰。
// React.memo：App 在用户输入 task、刷新历史等场景下会重渲染，但 result
// 通常不变。memo 让 ReportView 跳过这些无关重渲染，避免 ReactMarkdown
// 重新解析 markdown AST（report 可能长达数千字）。
const ReportView = React.memo(function ReportView({
  result,
  streaming,
  onPreview,
  artifacts,
  reasoning,
  reasoningStreaming,
  theme,
  usage,
  sessionId,
}: ReportViewProps) {
  // copyState: "idle" | "copied" | "failed"。之前只有 copied boolean，
  // 复制失败时静默吞掉错误，用户切到其他应用粘贴才发现是旧内容。
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [expanded, setExpanded] = useState(false);
  // 导出下拉菜单展开状态 + 容器 ref（用于点击外部收起）
  const [exportOpen, setExportOpen] = useState(false);
  // 仪表盘导出需后端实时组装 HTML（含质量剖析），可能耗时数秒，
  // 用 loading 态防重复点击并给用户反馈。
  const [dashboardBusy, setDashboardBusy] = useState(false);
  const exportWrapRef = useRef<HTMLDivElement>(null);
  const reportBodyRef = useRef<HTMLDivElement>(null);
  // useDeferredValue: 流式追加时 ReactMarkdown 重解析整个 AST 会卡顿，
  // defer 让高优先级更新（输入框交互）先走，Markdown 渲染延后。
  const deferredResponse = useDeferredValue(result.response || "");
  const deferredReasoning = useDeferredValue(reasoning || "");

  // useMemo 缓存 markdownComponents：否则每次渲染都返回新对象，ReactMarkdown
  // 会因 components prop 引用变化而全量重解析 AST，流式时每个 chunk 都重解析。
  // 覆盖 h2/h3：基于文本生成 id，供目录锚定与 scrollIntoView 跳转。
  const mdComponents = useMemo(() => {
    const base = markdownComponents(artifacts, onPreview, theme);
    return {
      ...base,
      h2: (({ children }: { children?: React.ReactNode }) => (
        <h2 id={slugify(extractText(children))}>{children}</h2>
      )) as Components["h2"],
      h3: (({ children }: { children?: React.ReactNode }) => (
        <h3 id={slugify(extractText(children))}>{children}</h3>
      )) as Components["h3"],
    };
  }, [artifacts, onPreview, theme]);

  // 目录数据与当前阅读位置：从渲染后的 DOM 采集标题，保证目录 id 与实际
  // 元素 id 一致（避免 markdown 内联标记导致解析不一致）。
  const [headings, setHeadings] = useState<HeadingItem[]>([]);
  const [activeId, setActiveId] = useState<string>("");

  // 流式时自动滚动到底部，让用户看到最新生成的文字
  useEffect(() => {
    if (streaming && reportBodyRef.current) {
      reportBodyRef.current.scrollTop = reportBodyRef.current.scrollHeight;
    }
  }, [deferredResponse, streaming]);

  // 渲染完成后从 DOM 采集 h2/h3，构建目录数据。流式期间不显示目录，
  // 避免边生成边变动导致目录闪烁。
  useEffect(() => {
    if (streaming) {
      setHeadings([]);
      setActiveId("");
      return;
    }
    const root = reportBodyRef.current;
    if (!root) return;
    const els = Array.from(root.querySelectorAll("h2, h3")) as HTMLHeadingElement[];
    const items: HeadingItem[] = els
      .map((el) => ({
        id: el.id,
        text: (el.textContent || "").trim(),
        level: (el.tagName === "H2" ? 2 : 3) as 2 | 3,
      }))
      .filter((it) => it.id);
    setHeadings(items);
  }, [deferredResponse, streaming]);

  const showToc = !streaming && headings.length > 3;

  // IntersectionObserver 高亮当前阅读位置的标题：取视口内最靠上的标题。
  // rootMargin 让标题接近顶部时即触发，符合"正在读这一节"的直觉。
  useEffect(() => {
    if (!showToc) return;
    const els = headings
      .map((h) => document.getElementById(h.id))
      .filter(Boolean) as HTMLElement[];
    if (!els.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId((visible[0].target as HTMLElement).id);
      },
      { rootMargin: "0px 0px -70% 0px", threshold: 0 }
    );
    els.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [headings, showToc]);

  // 点击目录项：折叠状态需先展开才能让目标标题可见，再平滑滚动到位。
  const handleTocClick = useCallback(
    (id: string) => {
      const el = document.getElementById(id);
      if (!el) return;
      const scroll = () => el.scrollIntoView({ behavior: "smooth", block: "start" });
      if (!expanded) {
        setExpanded(true);
        // 等展开 max-height 过渡启动后再滚动，避免目标仍在裁剪区。
        requestAnimationFrame(() => requestAnimationFrame(scroll));
      } else {
        scroll();
      }
    },
    [expanded]
  );

  // 导出下拉：点击菜单容器外部即收起（mousedown + closest 判定）
  useEffect(() => {
    if (!exportOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!exportWrapRef.current) return;
      if (!exportWrapRef.current.contains(e.target as Node)) setExportOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [exportOpen]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(result.response || "");
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      // HTTP 部署、iframe 受限或浏览器禁用 clipboard 时明确告知用户，
      // 让用户知道需要手动选择文本复制，而不是以为复制成功了。
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 3000);
    }
  };

  // 构建自包含 HTML 字符串：内联 CSS + markdown 转 HTML，供 HTML 下载与打印复用
  const buildReportHtml = useCallback((): string => {
    const md = result?.response || "";
    const title = deriveFilename(md);
    const body = markdownToHtml(md);
    return [
      "<!DOCTYPE html>",
      '<html lang="zh-CN">',
      "<head>",
      '<meta charset="utf-8">',
      '<meta name="viewport" content="width=device-width, initial-scale=1">',
      `<title>${escapeHtml(title)}</title>`,
      `<style>${REPORT_HTML_STYLE}</style>`,
      "</head>",
      "<body>",
      body,
      "</body>",
      "</html>",
    ].join("\n");
  }, [result]);

  // 导出 Markdown：Blob → ObjectURL → 临时 <a> 点击下载 → 释放
  const exportMarkdown = useCallback(() => {
    if (!result?.response) return;
    const blob = new Blob([result.response], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `分析报告_${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    setExportOpen(false);
  }, [result]);

  // 导出自包含 HTML 文件：内联样式，离线可打开
  const exportHtml = useCallback(() => {
    if (!result?.response) return;
    const html = buildReportHtml();
    const blob = new Blob([html], { type: "text/html;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${deriveFilename(result.response)}.html`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    setExportOpen(false);
  }, [result, buildReportHtml]);

  // 导出数据画像仪表盘：后端实时组装 KPI + 质量告警 + 全部图表的自包含 HTML，
  // 走鉴权 fetch → blob → 临时 <a> 下载（与 useDownloads 同模式）。
  const exportDashboard = useCallback(async () => {
    if (!sessionId || dashboardBusy) return;
    setDashboardBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/sessions/${sessionId}/dashboard`, {
        headers: requestHeaders(),
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const payload: unknown = contentType.includes("application/json")
          ? await response.json().catch(() => ({}))
          : await response.text().catch(() => "");
        throw new Error(describeApiError(payload, response.status));
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `数据画像仪表盘_${new Date().toISOString().slice(0, 10)}.html`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setExportOpen(false);
    } catch (err) {
      window.alert(`导出仪表盘失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setDashboardBusy(false);
    }
  }, [sessionId, dashboardBusy]);

  // 打印 / PDF：新窗口写入自包含 HTML，触发 window.print()，由浏览器另存 PDF
  const printReport = useCallback(() => {
    if (!result?.response) return;
    const html = buildReportHtml();
    const win = window.open("", "_blank");
    if (!win) {
      // 弹窗被拦截：提示用户允许弹窗或改用 HTML 导出
      window.alert("无法打开打印窗口，请允许浏览器弹窗后重试，或选择 HTML 导出。");
      setExportOpen(false);
      return;
    }
    win.document.open();
    win.document.write(html);
    win.document.close();
    let printed = false;
    const doPrint = () => {
      if (printed) return;
      printed = true;
      win.focus();
      win.print();
    };
    // load 触发打印；setTimeout 兜底，避免个别浏览器 load 不触发
    win.addEventListener("load", doPrint);
    window.setTimeout(doPrint, 600);
    setExportOpen(false);
  }, [result, buildReportHtml]);

  return (
    <article className="report">
      <div className="report-meta">
        <div className="report-title">
          <FileChartColumn size={15} />
          {/* 流式中用 ShinyText 循环微光暗示"进行中"，完成后切 GradientText
              静态流动渐变，作为报告完成的视觉奖励 */}
          {streaming ? (
            <ShinyText text="分析报告" color="var(--text-primary)" shineColor="var(--accent-color, #5b5bd6)" speed={4} yoyo />
          ) : (
            <GradientText speed={7}>分析报告</GradientText>
          )}
          {streaming ? (
            <small className="report-count is-streaming"><LoaderCircle size={11} className="spin" />生成中</small>
          ) : (
            <small className="report-count">{result.artifacts?.length || 0} 个产物</small>
          )}
        </div>
        <div className="report-actions">
          {!streaming && <UsageChip usage={usage} />}
          <button
            type="button"
            className={`report-copy ${copyState === "failed" ? "is-failed" : ""}`}
            onClick={handleCopy}
            title="复制全文"
            aria-label="复制报告全文"
            disabled={streaming}
          >
            {copyState === "copied" ? <Check size={13} /> : <FileSpreadsheet size={13} />}
            {copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制"}
          </button>
          <div className="export-menu" ref={exportWrapRef}>
            <button
              type="button"
              className="report-copy export-trigger"
              onClick={() => setExportOpen((v) => !v)}
              disabled={streaming}
              aria-haspopup="menu"
              aria-expanded={exportOpen}
              title="导出报告"
            >
              <Download size={13} />导出<ChevronDown size={12} className={exportOpen ? "rot-180" : ""} />
            </button>
            {exportOpen && (
              <div className="export-dropdown" role="menu">
                <button type="button" role="menuitem" className="export-item" onClick={exportMarkdown}>
                  <FileText size={13} />Markdown (.md)
                </button>
                <button type="button" role="menuitem" className="export-item" onClick={exportHtml}>
                  <FileCode size={13} />HTML (.html)
                </button>
                <button type="button" role="menuitem" className="export-item" onClick={printReport}>
                  <Printer size={13} />打印 / PDF
                </button>
                {sessionId && (
                  <button
                    type="button"
                    role="menuitem"
                    className="export-item"
                    onClick={exportDashboard}
                    disabled={dashboardBusy}
                  >
                    {dashboardBusy ? (
                      <LoaderCircle size={13} className="spin" />
                    ) : (
                      <LayoutDashboard size={13} />
                    )}
                    {dashboardBusy ? "生成仪表盘中…" : "数据画像仪表盘 (.html)"}
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
      <ReasoningBlock content={deferredReasoning} streaming={reasoningStreaming} />
      <div className={`report-content-wrap ${showToc ? "has-toc" : ""}`}>
        {showToc && (
          <nav className="report-toc" aria-label="报告目录">
            <div className="report-toc-title">目录</div>
            <ul className="report-toc-list">
              {headings.map((h) => (
                <li
                  key={h.id}
                  className={`report-toc-item ${h.level === 3 ? "is-sub" : ""} ${activeId === h.id ? "is-active" : ""}`}
                >
                  <button type="button" onClick={() => handleTocClick(h.id)} title={h.text}>
                    {h.text}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        )}
        <div className={`report-body ${expanded ? "is-expanded" : ""} ${streaming ? "is-streaming" : ""}`} ref={reportBodyRef}>
          {streaming && !deferredResponse ? (
            <div className="report-placeholder"><LoaderCircle size={14} className="spin" />正在生成报告…</div>
          ) : (
            <>
              <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={mdComponents}>
                {deferredResponse}
              </ReactMarkdown>
              {streaming && <span className="report-cursor" aria-hidden="true" />}
            </>
          )}
        </div>
      </div>
      {!streaming && (
        <button
          type="button"
          className="report-toggle"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
        >
          <ChevronDown size={13} className={expanded ? "rot-180" : ""} />
          {expanded ? "收起报告" : "展开完整报告"}
        </button>
      )}
    </article>
  );
});

export default ReportView;
