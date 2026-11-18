import { MessageSquare, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../utils/api";

interface CommentItem {
  id: string;
  text: string;
  author: string;
  created_at: string;
}

interface CommentsPanelProps {
  sessionId: string;
}

const AUTHOR_KEY = "data-agent-comment-author";

// 会话评论：轻量协作层。纯文本渲染（不解析 Markdown/HTML），避免把评论
// 变成 XSS 注入面；作者名只用于展示，不作为权限身份（写权限由访问令牌决定）。
export default function CommentsPanel({ sessionId }: CommentsPanelProps) {
  const [comments, setComments] = useState<CommentItem[]>([]);
  const [text, setText] = useState("");
  const [author, setAuthor] = useState(() => window.localStorage.getItem(AUTHOR_KEY) || "协作者");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await api<{ comments: CommentItem[] }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/comments`,
      );
      setComments(payload.comments);
    } catch (err) {
      setError(err instanceof Error ? err.message : "评论加载失败。");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async () => {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError("");
    try {
      const created = await api<CommentItem>(
        `/api/sessions/${encodeURIComponent(sessionId)}/comments`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: trimmed, author: author.trim() }),
        },
      );
      setComments((items) => [...items, created]);
      setText("");
      window.localStorage.setItem(AUTHOR_KEY, author.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : "评论发送失败。");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (commentId: string) => {
    setBusy(true);
    setError("");
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`, {
        method: "DELETE",
      });
      setComments((items) => items.filter((item) => item.id !== commentId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="comments-panel" aria-label="会话评论">
      <header className="comments-panel-header">
        <div>
          <span className="section-kicker">协作</span>
          <h2><MessageSquare size={16} /> 会话评论</h2>
        </div>
        <span className="comments-panel-count">{comments.length}</span>
      </header>

      {loading ? (
        <p className="comments-panel-empty">正在加载评论…</p>
      ) : comments.length === 0 ? (
        <p className="comments-panel-empty">还没有评论。把口径疑问或复核意见留在这里，队友打开同一会话就能看到。</p>
      ) : (
        <ul className="comments-panel-list">
          {comments.map((item) => (
            <li key={item.id} className="comments-panel-item">
              <div className="comments-panel-meta">
                <strong>{item.author}</strong>
                <time dateTime={item.created_at}>{item.created_at.replace("T", " ").slice(0, 16)}</time>
                <button
                  type="button"
                  className="comments-panel-delete"
                  aria-label={`删除 ${item.author} 的评论`}
                  disabled={busy}
                  onClick={() => void remove(item.id)}
                >
                  <Trash2 size={13} />
                </button>
              </div>
              <p className="comments-panel-text">{item.text}</p>
            </li>
          ))}
        </ul>
      )}

      <div className="comments-panel-compose">
        <label className="comments-panel-label">
          署名
          <input
            value={author}
            maxLength={40}
            onChange={(event) => setAuthor(event.target.value)}
            className="comments-panel-author"
          />
        </label>
        <label className="comments-panel-label comments-panel-label-text">
          评论
          <textarea
            value={text}
            maxLength={2000}
            rows={3}
            placeholder="例如：净销售额口径是否包含退货？"
            onChange={(event) => setText(event.target.value)}
          />
        </label>
        <div className="comments-panel-actions">
          {error && <span role="alert" className="comments-panel-error">{error}</span>}
          <span className="comments-panel-hint">{text.length}/2000</span>
          <button type="button" disabled={busy || !text.trim()} onClick={() => void submit()}>
            {busy ? "处理中…" : "发表评论"}
          </button>
        </div>
      </div>
    </section>
  );
}
