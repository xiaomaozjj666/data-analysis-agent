import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Ruler } from "lucide-react";
import { api } from "../utils/api";

const EMPTY_TEMPLATE = `{
  "metrics": [
    {
      "name": "净销售额",
      "description": "销售额减去退货金额",
      "expression": "销售额 - 退货金额",
      "aliases": ["净销"],
      "source_columns": ["销售额", "退货金额"]
    }
  ]
}`;

interface MetricsPayload {
  metrics: unknown[];
  source: "env" | "workspace" | null;
  env_overrides: boolean;
}

interface MetricsEditorProps {
  sessionId: string;
}

export default function MetricsEditor({ sessionId }: MetricsEditorProps) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('{\n  "metrics": []\n}');
  const [count, setCount] = useState(0);
  const [envOverrides, setEnvOverrides] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    const payload = await api<MetricsPayload>(`/api/sessions/${sessionId}/metrics`);
    setText(JSON.stringify({ metrics: payload.metrics }, null, 2));
    setCount(payload.metrics.length);
    setEnvOverrides(payload.env_overrides);
    setMessage("");
  }, [sessionId]);

  useEffect(() => {
    void load().catch((err: unknown) => {
      setMessage(err instanceof Error ? err.message : "无法读取指标口径");
    });
  }, [load]);

  async function save() {
    setBusy(true);
    setMessage("");
    try {
      const parsed = JSON.parse(text) as unknown;
      const saved = await api<MetricsPayload>(`/api/sessions/${sessionId}/metrics`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      setText(JSON.stringify({ metrics: saved.metrics }, null, 2));
      setCount(saved.metrics.length);
      setMessage(saved.metrics.length ? `已保存 ${saved.metrics.length} 条口径` : "已清空口径");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`metrics-editor ${open ? "is-open" : ""}`}>
      <button
        type="button"
        className="metrics-editor-toggle"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <Ruler size={14} />
        <span>业务口径</span>
        <small>{count ? `${count} 条` : "未登记"}</small>
        <ChevronDown size={14} className={open ? "rot-180" : ""} />
      </button>
      {open && (
        <div className="metrics-editor-body">
          <p>
            登记后，规划会按这些公式计算同名指标，不再现场另推口径。表达式只能用列名与加减乘除。
          </p>
          {envOverrides && (
            <p className="metrics-editor-warn" role="status">
              当前生效的是全局 DATA_AGENT_METRICS_PATH，会话内无法改写。
            </p>
          )}
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            spellCheck={false}
            rows={10}
            disabled={envOverrides}
            aria-label="指标口径 JSON"
          />
          <div className="metrics-editor-actions">
            <button type="button" onClick={() => setText(EMPTY_TEMPLATE)} disabled={envOverrides}>
              插入示例
            </button>
            <button type="button" className="metrics-save" onClick={() => void save()} disabled={busy || envOverrides}>
              {busy ? "保存中…" : "保存口径"}
            </button>
            {message && <span>{message}</span>}
          </div>
        </div>
      )}
    </section>
  );
}
