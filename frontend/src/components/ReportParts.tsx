import React, { useEffect, useState } from "react";
import {
  Brain,
  ChevronRight,
  Clock,
  ExternalLink,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import CodeBlock from "./CodeBlock";
import { API_URL, REMARK_PLUGINS, pickChartIcon } from "../constants";
import { formatTokens } from "../utils/format";
import type { Artifact, TokenUsage } from "../types";

interface ReasoningBlockProps {
  content?: string;
  streaming?: boolean;
  expanded?: boolean;
}

// 思考过程展示：可折叠的推理区域，把 DeepSeek 的 reasoning_content 实时
// 流式展示。默认折叠，避免推理内容过长挤占正文；流式时显示光标。
const ReasoningBlock = React.memo(function ReasoningBlock({ content, streaming }: ReasoningBlockProps) {
  const [expanded, setExpanded] = useState(false);
  // 流式期间自动展开，让用户看到模型在想什么；结束后自动收起
  useEffect(() => {
    if (streaming) setExpanded(true);
  }, [streaming]);
  if (!content && !streaming) return null;
  return (
    <div className={`reasoning-block ${expanded ? "is-expanded" : ""}`}>
      <button
        type="button"
        className="reasoning-toggle"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <ChevronRight size={11} className="reasoning-chevron" />
        <Brain size={11} />
        <span>{streaming ? "正在思考…" : "思考过程"}</span>
        {content && <em style={{ marginLeft: "auto", color: "var(--fg-subtle)", fontWeight: 400 }}>{content.length} 字</em>}
      </button>
      {expanded && (
        <div className={`reasoning-body ${streaming ? "is-streaming" : ""}`}>
          {/* 用 ReactMarkdown 渲染思考内容：模型常会输出 **加粗**、`代码`、
              - 列表 等 markdown 语法，纯文本会显示原始字符，影响可读性。
              复用 REMARK_PLUGINS（与 ReportView 一致）以支持 GFM 表格/删除线等。 */}
          {content ? (
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>{content}</ReactMarkdown>
          ) : streaming ? "…" : ""}
          {streaming && <span className="reasoning-cursor" aria-hidden="true" />}
        </div>
      )}
    </div>
  );
});

interface UsageChipProps {
  usage?: TokenUsage | null;
}

// Token 用量 chip：在报告头部和追问气泡末尾展示本次回答的 token 用量。
// 主流 Agent（ChatGPT/Claude）都展示这个指标，让用户感知模型消耗。
const UsageChip = React.memo(function UsageChip({ usage }: UsageChipProps) {
  if (!usage || (!usage.prompt_tokens && !usage.completion_tokens && !usage.total_tokens)) return null;
  const total = usage.total_tokens || ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
  return (
    <span className="usage-chip" title="本次 LLM 调用的 token 用量">
      <Clock size={11} />
      {usage.prompt_tokens > 0 && <>
        <span>输入 <strong>{formatTokens(usage.prompt_tokens)}</strong></span>
        <span className="usage-sep">·</span>
      </>}
      {usage.completion_tokens > 0 && <>
        <span>输出 <strong>{formatTokens(usage.completion_tokens)}</strong></span>
        <span className="usage-sep">·</span>
      </>}
      <span>共 <strong>{formatTokens(total)}</strong></span>
    </span>
  );
});

type ArtifactPreviewHandler = (artifact: Artifact) => void;

// ReactMarkdown 自定义 components：识别 ![描述](artifact:图表文件名) 语法，
// 把图表占位符渲染为可点击的内嵌图表卡片，点击在模态框打开交互版。
// 让图表直接嵌在报告正文中图文混排，而不是只能切到产物 tab 查看。
// code 组件用 CodeBlock 渲染（语法高亮 + 复制按钮）。
function markdownComponents(
  artifacts: Artifact[] | null | undefined,
  onPreview: ArtifactPreviewHandler | undefined,
  theme: "light" | "dark" | undefined,
): Components {
  return {
    img: ({ src, alt }) => {
      if (typeof src === "string" && src.startsWith("artifact:")) {
        const name = src.slice("artifact:".length);
        const artifact = artifacts?.find((a) => a.name === name);
        if (artifact) {
          const { Icon, label } = pickChartIcon(artifact.name);
          // 优先展示缩略图（仅 Plotly 图表提供 thumbnail_url），让报告读者
          // 一眼看到图表概貌；ECharts 或缩略图加载失败时回退到类型图标。
          return (
            <button type="button" className="embedded-chart" onClick={() => onPreview?.(artifact)}>
              {artifact.thumbnail_url ? (
                <img
                  className="embedded-chart-thumb"
                  src={`${API_URL}${artifact.thumbnail_url}`}
                  alt={alt || label || artifact.description || artifact.name}
                  loading="lazy"
                  onError={(e) => {
                    // 缩略图加载失败时隐藏图片，避免显示破损图标；
                    // 由于同按钮内没有图标回退，此处仅隐藏避免视觉污染，
                    // 文字描述仍可点击进入预览。
                    (e.currentTarget as HTMLImageElement).style.display = "none";
                  }}
                />
              ) : (
                <Icon size={18} />
              )}
              <span>{alt || label || artifact.description || artifact.name}</span>
              <ExternalLink size={12} />
            </button>
          );
        }
        return <em className="embedded-chart-missing">图表 {name} 已丢失</em>;
      }
      return <img src={src} alt={alt} />;
    },
    code: (({ inline, className, children, ...props }: { inline?: boolean; className?: string; children?: React.ReactNode } & Record<string, unknown>) => {
      if (inline) {
        return <code className="inline-code" {...props}>{children}</code>;
      }
      // 从 className "language-xxx" 中提取语言
      const match = /language-(\w+)/.exec(className || "");
      const language = match ? match[1] : "";
      const value = String(children || "").replace(/\n$/, "");
      return <CodeBlock language={language} value={value} theme={theme} />;
    }) as Components["code"],
    pre: ({ children }) => <>{children}</>,
  };
}

export { markdownComponents, ReasoningBlock, UsageChip };
