import React, { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  Check,
  ChevronDown,
  Download,
  FileChartColumn,
  FileSpreadsheet,
  LoaderCircle,
} from "lucide-react";
import { REMARK_PLUGINS } from "../constants";
import { markdownComponents, ReasoningBlock, UsageChip } from "./ReportParts";
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
}: ReportViewProps) {
  // copyState: "idle" | "copied" | "failed"。之前只有 copied boolean，
  // 复制失败时静默吞掉错误，用户切到其他应用粘贴才发现是旧内容。
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [expanded, setExpanded] = useState(false);
  const reportBodyRef = useRef<HTMLDivElement>(null);
  // useDeferredValue: 流式追加时 ReactMarkdown 重解析整个 AST 会卡顿，
  // defer 让高优先级更新（输入框交互）先走，Markdown 渲染延后。
  const deferredResponse = useDeferredValue(result.response || "");
  const deferredReasoning = useDeferredValue(reasoning || "");

  // useMemo 缓存 markdownComponents：否则每次渲染都返回新对象，ReactMarkdown
  // 会因 components prop 引用变化而全量重解析 AST，流式时每个 chunk 都重解析。
  const mdComponents = useMemo(
    () => markdownComponents(artifacts, onPreview, theme),
    [artifacts, onPreview, theme]
  );

  // 流式时自动滚动到底部，让用户看到最新生成的文字
  useEffect(() => {
    if (streaming && reportBodyRef.current) {
      reportBodyRef.current.scrollTop = reportBodyRef.current.scrollHeight;
    }
  }, [deferredResponse, streaming]);

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

  // 导出报告为 Markdown 文件：Blob → ObjectURL → 临时 <a> 点击下载 → 释放。
  // 让用户能把报告存档/分享，而不只能在线查看或手动复制粘贴。
  const exportReport = useCallback(() => {
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
  }, [result]);

  return (
    <article className="report">
      <div className="report-meta">
        <div className="report-title">
          <FileChartColumn size={15} />
          <span>分析报告</span>
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
          <button
            type="button"
            className="report-copy"
            onClick={exportReport}
            title="导出为 Markdown"
            aria-label="导出报告为 Markdown 文件"
            disabled={streaming}
          >
            <Download size={13} />导出
          </button>
        </div>
      </div>
      <ReasoningBlock content={deferredReasoning} streaming={reasoningStreaming} />
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
