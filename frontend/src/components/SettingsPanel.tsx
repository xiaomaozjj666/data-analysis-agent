import { useCallback, useState } from "react";
import { Check, Eye, EyeOff, KeyRound, LoaderCircle, Settings2, X } from "lucide-react";
import { useAppStore } from "../store/useAppStore";
import { api, requestHeaders } from "../utils/api";
import { API_URL } from "../constants";
import type { Settings } from "../types";

// 侧栏"分析引擎"区块：model-line 状态按钮 + 设置面板（API Key / 思考模式 /
// 推理强度 / 连接测试 / 保存）。提取自 App.tsx，所有状态仍走 Zustand store，
// 测试连接的 testingKey / testResult 为组件本地状态（无需跨组件共享）。
function SettingsPanel() {
  const {
    settings, apiKey, effort, thinking,
    setSettings, setApiKey, setEffort, setThinking,
    showKey, keyOpen,
    setShowKey, setKeyOpen,
    savingSettings, setSavingSettings,
    setError,
  } = useAppStore();

  // === Batch A2：设置面板连接测试 ===
  // testingKey：测试进行中；testResult：测试结果（ok 标识成功/失败，message 为提示文案）
  const [testingKey, setTestingKey] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

  // 连接测试：在保存前先用当前 Key 发起一次轻量 PUT /api/settings 请求，
  // 仅验证连通性与 Key 有效性，不写入持久化（不带 persist_key）。
  // 成功时展示后端 warning（如配额提示），失败时展示 detail / HTTP 状态码。
  // 与 saveSettings 的区别：不 setSettings / 不 setKeyOpen / 不清空 apiKey，
  // 让用户在确认 Key 有效后再正式保存。
  const testConnection = useCallback(async () => {
    setTestingKey(true);
    setTestResult(null);
    try {
      const response = await fetch(`${API_URL}/api/settings`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          provider: settings?.provider || "deepseek",
          api_key: apiKey,
          model: settings?.model || "deepseek-chat",
          base_url: settings?.base_url,
          thinking_enabled: thinking,
          reasoning_effort: effort,
        }),
      });
      if (response.ok) {
        const data = await response.json() as { warning?: string };
        setTestResult({ ok: true, message: data.warning || "连接成功" });
      } else {
        const data = await response.json().catch(() => ({})) as { detail?: string };
        setTestResult({ ok: false, message: data.detail || `HTTP ${response.status}` });
      }
    } catch (err) {
      setTestResult({ ok: false, message: err instanceof Error ? err.message : "连接失败" });
    } finally {
      setTestingKey(false);
    }
  }, [apiKey, settings, thinking, effort]);

  async function saveSettings() {
    if (savingSettings) return;
    setError("");
    setSavingSettings(true);
    try {
      const payload = await api<Settings>("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey || undefined,
          thinking_enabled: thinking,
          reasoning_effort: effort,
          persist_key: true,
        }),
      });
      setSettings(payload);
      setApiKey("");
      setKeyOpen(false);
      if (payload.warning) setError(payload.warning);
    } catch (err) {
      const error = err as Error;
      setError(error.message);
    } finally {
      setSavingSettings(false);
    }
  }

  return (
    <div className="provider-block">
      <span className="sidebar-label">分析引擎</span>
      <button type="button" className="model-line" onClick={() => setKeyOpen((value) => !value)}>
        <span>
          <i className={settings?.configured ? "online" : ""} />
          <span><strong>{settings?.model || "deepseek-chat"}</strong><small>{settings?.configured ? "已连接" : "等待配置"}</small></span>
        </span>
        <Settings2 size={15} />
      </button>
      {keyOpen && (
        <>
          {/* 遮罩层：固定定位覆盖全屏，点击任意位置收起设置面板。
              z-index 19 低于 settings-form 的 20，避免遮挡表单交互 */}
          <div className="settings-backdrop" onClick={() => setKeyOpen(false)} aria-hidden="true" />
          <form className="settings-form" onSubmit={(event) => { event.preventDefault(); saveSettings(); }}>
          <div className="settings-title">
            <strong>模型设置</strong>
            <button type="button" title="收起设置（Esc）" aria-label="收起设置" onClick={() => { setKeyOpen(false); setApiKey(""); }}><X size={15} /></button>
          </div>
          <label>API Key</label>
          <div className="secret-input">
            <KeyRound size={14} />
            <input
              type={showKey ? "text" : "password"}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={settings?.configured ? "已安全保存，留空则不变" : "输入 DeepSeek Key"}
            />
            <button type="button" title={showKey ? "隐藏 Key" : "显示 Key"} onClick={() => setShowKey((value) => !value)}>
              {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          <label className="toggle-row">
            <span>思考模式</span>
            <input type="checkbox" checked={thinking} onChange={(event) => setThinking(event.target.checked)} />
          </label>
          <label>推理强度</label>
          <div className="segment">
            {[{ value: "high", label: "标准" }, { value: "max", label: "深度" }].map(({ value, label }) => (
              <button type="button" key={value} title={value === "high" ? "标准推理速度，适合大多数场景" : "最深推理，效果更好但更慢"} className={effort === value ? "selected" : ""} onClick={() => setEffort(value)}>{label}</button>
            ))}
          </div>
          {/* 连接测试结果：成功绿色、失败红色，提示文案来自后端 warning / detail */}
          {testResult && (
            <div className={`test-result ${testResult.ok ? "ok" : "fail"}`}>
              {testResult.ok ? "✓ " : "✗ "}{testResult.message}
            </div>
          )}
          {/* 操作区：测试连接（次操作）+ 保存（主操作）并排展示 */}
          <div className="settings-actions">
            <button type="button" className="test-button" onClick={testConnection} disabled={testingKey || !apiKey}>
              {testingKey ? "测试中…" : "测试连接"}
            </button>
            <button type="submit" className="save-button" disabled={savingSettings}>
              {savingSettings ? <><LoaderCircle size={14} className="spin" />保存中…</> : <><Check size={14} />保存设置</>}
            </button>
          </div>
          </form>
        </>
      )}
    </div>
  );
}

export default SettingsPanel;
