import React, { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  AlertTriangle,
  Check,
  CornerDownLeft,
  Copy,
  Play,
  RefreshCw,
  Square,
} from "lucide-react";
import { ChatToolChip } from "./ToolTraceItem";
import { markdownComponents, ReasoningBlock, UsageChip } from "./ReportParts";
import { REMARK_PLUGINS } from "../constants";
import type { Artifact, FollowUpMessage } from "../types";

interface ConversationThreadProps {
  messages: FollowUpMessage[];
  input: string;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  running: boolean;
  disabled: boolean;
  onPreview?: (item: Artifact) => void;
  artifacts?: Artifact[] | null;
  onEditMessage?: (index: number, newContent: string) => void;
  theme?: "light" | "dark";
  inputRef?: React.RefObject<HTMLTextAreaElement | null>;
}

// 多轮对话线程：在主报告下方展示追问历史 + 追问输入框。
// 设计参考 ChatGPT / Claude / Linear 的对话流：
//   - 用户气泡右对齐 + 主色底，assistant 气泡左对齐 + 卡片底
//   - assistant 气泡内嵌 Markdown 渲染 + 工具调用 mini 时间线
//   - 流式时显示闪烁光标，让用户知道回答正在写出
//   - 底部固定追问输入框，支持 Ctrl+Enter 提交
const ConversationThread = React.memo(function ConversationThread({
  messages, input, onInputChange, onSubmit, onStop, running, disabled, onPreview, artifacts,
  onEditMessage, theme, inputRef,
}: ConversationThreadProps) {
  const listRef = useRef<HTMLDivElement>(null);
  // 智能滚动跟随：用户上滑阅读历史时暂停自动滚底，回到底部附近或
  // 发出新消息时恢复跟随，避免流式生成中强制把用户拽回底部。
  const stickToBottomRef = useRef(true);
  const prevCountRef = useRef(messages.length);
  const deferredLastContent = useDeferredValue(
    messages.length ? messages[messages.length - 1].content || "" : ""
  );

  const handleListScroll = () => {
    const el = listRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  // 流式时自动滚动到底部；新增消息（用户刚发送）时强制回底并重置跟随
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const hasNewMessage = messages.length !== prevCountRef.current;
    prevCountRef.current = messages.length;
    if (hasNewMessage) stickToBottomRef.current = true;
    if (stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [deferredLastContent, messages.length, running]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter（含 ⌘/Ctrl+Enter）发送；Shift+Enter 走 textarea 默认换行，不阻止。
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!running && input.trim() && !disabled) onSubmit();
    }
  };

  return (
    <section className="conversation-thread" aria-label="追问对话">
      <div className="conversation-header">
        <div>
          <span className="section-kicker">继续对话</span>
          <h2>追问与补充分析</h2>
        </div>
        <small>基于当前数据集直接回答，无需重跑完整流程</small>
      </div>
      {messages.length > 0 && (
        <div className="conversation-list" ref={listRef} onScroll={handleListScroll}>
          {messages.map((msg, index) => (
            <ConversationBubble
              key={index}
              message={msg}
              index={index}
              onPreview={onPreview}
              artifacts={artifacts}
              onEditMessage={onEditMessage}
              theme={theme}
              canEdit={!running && !disabled}
            />
          ))}
        </div>
      )}
      <div className="follow-up-composer">
        <textarea
          ref={inputRef}
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "请先配置 API Key 后再追问" : "追问任何关于数据的问题，如：把刚才那张图改成红色 / 解释这个 p 值"}
          rows={2}
          disabled={disabled}
        />
        <small className="input-hint">Enter 发送 · Shift+Enter 换行</small>
        <div className="follow-up-actions">
          {running ? (
            <button className="cancel-button" type="button" onClick={onStop} disabled={false}>
              <Square size={12} fill="currentColor" />停止
            </button>
          ) : (
            <button
              className="run-button"
              type="button"
              onClick={onSubmit}
              disabled={!input.trim() || disabled}
            >
              <Play size={14} fill="currentColor" />发送追问
            </button>
          )}
        </div>
      </div>
    </section>
  );
});

interface ConversationBubbleProps {
  message: FollowUpMessage;
  index: number;
  onPreview?: (item: Artifact) => void;
  artifacts?: Artifact[] | null;
  onEditMessage?: (index: number, newContent: string) => void;
  theme?: "light" | "dark";
  canEdit: boolean;
}

// 单条对话气泡：
//   - user 气泡支持 hover 显示编辑按钮，点击进入编辑模式，保存后调用 onEditMessage 截断重发
//   - assistant 气泡支持 reasoning（思考过程）展示、工具调用展开、usage chip、流式光标
const ConversationBubble = React.memo(function ConversationBubble({
  message, index, onPreview, artifacts, onEditMessage, theme, canEdit,
}: ConversationBubbleProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content || "");
  // 复制状态：copied 成功 / failed 失败（剪贴板权限被拒或 HTTP 环境），
  // 失败时明确告知而非静默吞掉，避免用户以为已复制成功。
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  // 进入编辑模式时同步 draft
  useEffect(() => {
    if (editing) setDraft(message.content || "");
  }, [editing, message.content]);

  // useMemo 缓存 markdownComponents，避免每次渲染重建对象导致 ReactMarkdown 重解析
  const mdComponents = useMemo(
    () => markdownComponents(artifacts, onPreview, theme),
    [artifacts, onPreview, theme]
  );

  // 复制消息内容到剪贴板：成功切 ✓ 图标 1.8s，失败切警告图标 3s 提示用户手动复制
  const copyMessage = (text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    }).catch(() => {
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 3000);
    });
  };

  if (message.role === "user") {
    return (
      <div className="chat-bubble is-user">
        {editing ? (
          <div>
            <textarea
              className="chat-bubble-edit-area"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              autoFocus
              rows={Math.min(8, Math.max(2, draft.split("\n").length))}
            />
            <div className="chat-bubble-edit-actions">
              <button type="button" className="btn-save" onClick={() => {
                if (draft.trim() && draft.trim() !== (message.content || "").trim()) {
                  onEditMessage?.(index, draft.trim());
                }
                setEditing(false);
              }}>
                <CornerDownLeft size={11} />保存并重发
              </button>
              <button type="button" className="btn-cancel" onClick={() => setEditing(false)}>取消</button>
            </div>
          </div>
        ) : (
          <>
            <div className="chat-bubble-content">{message.content}</div>
            {/* 用户气泡操作：复制 + 编辑重发，复用 chat-bubble-action-btn 保持白底半透明风格 */}
            <div className="chat-bubble-actions">
              <button
                type="button"
                className="chat-bubble-action-btn"
                title={copyState === "failed" ? "复制失败，请手动选择文本复制" : "复制"}
                aria-label="复制消息"
                onClick={() => copyMessage(message.content || "")}
              >
                {copyState === "copied" ? <Check size={12} /> : copyState === "failed" ? <AlertTriangle size={12} /> : <Copy size={12} />}
              </button>
              {canEdit && onEditMessage && (
                <button
                  type="button"
                  className="chat-bubble-action-btn"
                  title="编辑后重新发送（会清除后续对话）"
                  onClick={() => setEditing(true)}
                  aria-label="编辑消息"
                >
                  <RefreshCw size={11} />
                </button>
              )}
            </div>
          </>
        )}
      </div>
    );
  }
  // assistant 气泡：reasoning + Markdown 渲染 + 工具时间线 + 流式光标 + usage
  const isStreaming = message.streaming;
  const hasContent = !!message.content;
  const hasReasoning = !!message.reasoning;
  return (
    <div className="chat-bubble is-assistant">
      {(hasReasoning || (isStreaming && !hasContent)) && (
        <ReasoningBlock content={message.reasoning || ""} streaming={isStreaming && !hasContent} />
      )}
      {message.tools && message.tools.length > 0 && (
        <div className="chat-tools" aria-label="本次追问工具调用">
          {message.tools.map((tool) => (
            <ChatToolChip key={tool.call_id} tool={tool} />
          ))}
        </div>
      )}
      <div className={`chat-bubble-content ${isStreaming ? "is-streaming" : ""}`}>
        {isStreaming && !hasContent && !hasReasoning ? (
          <div className="thinking-placeholder">
            <span className="thinking-dots"><span /><span /><span /></span>
            正在思考…
          </div>
        ) : hasContent ? (
          <>
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={mdComponents}>
              {message.content || ""}
            </ReactMarkdown>
            {isStreaming && <span className="report-cursor" aria-hidden="true" />}
          </>
        ) : null}
      </div>
      {/* 助手气泡操作：复制按钮，hover 显示；流式生成中不显示避免干扰 */}
      {!isStreaming && hasContent && (
        <div className="chat-bubble-actions">
          <button
            type="button"
            className="chat-action"
            title={copyState === "failed" ? "复制失败，请手动选择文本复制" : "复制"}
            aria-label="复制回复"
            onClick={() => copyMessage(message.content || "")}
          >
            {copyState === "copied" ? <Check size={12} /> : copyState === "failed" ? <AlertTriangle size={12} /> : <Copy size={12} />}
          </button>
        </div>
      )}
      {!isStreaming && message.usage && <UsageChip usage={message.usage} />}
      {message.error && <div className="chat-bubble-error"><AlertTriangle size={12} />{message.error}</div>}
    </div>
  );
});

export default ConversationThread;
