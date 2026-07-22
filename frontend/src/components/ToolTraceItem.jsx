import React, { useState } from "react";
import { ChevronRight, LoaderCircle } from "lucide-react";
import { TOOL_LABELS } from "../constants";
import { tryFormatJson } from "../utils/format";

// 工具调用展开/折叠项：点击行展开 input_preview / output_preview JSON。
// 之前 toolTrace 已经携带这两段数据但 UI 没渲染，是"半成品"。
// 这里补上交互，让用户能像 Claude/ChatGPT 那样点开看工具实际做了什么。
const ToolTraceItem = React.memo(function ToolTraceItem({ tool, defaultExpanded = false }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const hasDetail = !!(tool.input_preview || tool.output_preview);
  return (
    <li className={`tool-trace-item ${expanded ? "is-expanded" : ""}`}>
      <div
        className="tool-trace-row"
        onClick={() => hasDetail && setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (hasDetail && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        role={hasDetail ? "button" : undefined}
        tabIndex={hasDetail ? 0 : undefined}
        aria-expanded={hasDetail ? expanded : undefined}
      >
        {hasDetail ? <ChevronRight size={11} className="tool-chevron" /> : <span style={{ width: 11 }} />}
        <span className="tool-dot" aria-hidden="true" />
        <span className="tool-name">{TOOL_LABELS[tool.name] || tool.name}</span>
        {tool.status === "running" ? (
          <LoaderCircle size={10} className="spin" />
        ) : (
          <span className="tool-duration">{tool.duration_ms ? `${tool.duration_ms}ms` : ""}</span>
        )}
      </div>
      {expanded && hasDetail && (
        <div className="tool-trace-detail">
          {tool.input_preview && (
            <div className="tool-trace-detail-section">
              <span className="tool-trace-detail-label">输入</span>
              <pre>{tryFormatJson(tool.input_preview)}</pre>
            </div>
          )}
          {tool.output_preview && (
            <div className="tool-trace-detail-section">
              <span className="tool-trace-detail-label">输出</span>
              <pre>{tryFormatJson(tool.output_preview)}</pre>
            </div>
          )}
        </div>
      )}
    </li>
  );
});

// 对话气泡内的工具 chip：支持点击展开查看 input/output preview
const ChatToolChip = React.memo(function ChatToolChip({ tool }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = !!(tool.input_preview || tool.output_preview);
  return (
    <span
      className={`chat-tool-chip ${tool.status === "running" ? "is-running" : "is-done"}`}
      onClick={() => hasDetail && setExpanded((v) => !v)}
      onKeyDown={(e) => {
        if (hasDetail && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          setExpanded((v) => !v);
        }
      }}
      role={hasDetail ? "button" : undefined}
      tabIndex={hasDetail ? 0 : undefined}
      aria-expanded={hasDetail ? expanded : undefined}
    >
      <span className="tool-dot" aria-hidden="true" />
      {TOOL_LABELS[tool.name] || tool.name}
      {tool.status === "running" ? (
        <LoaderCircle size={9} className="spin" />
      ) : (
        <em>{tool.duration_ms ? `${tool.duration_ms}ms` : ""}</em>
      )}
      {expanded && hasDetail && (
        <div className="chat-tool-detail" onClick={(e) => e.stopPropagation()}>
          {tool.input_preview && <div><strong>输入</strong><pre>{tryFormatJson(tool.input_preview)}</pre></div>}
          {tool.output_preview && <div><strong>输出</strong><pre>{tryFormatJson(tool.output_preview)}</pre></div>}
        </div>
      )}
    </span>
  );
});

export { ToolTraceItem, ChatToolChip };
