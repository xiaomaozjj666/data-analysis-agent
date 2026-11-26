import { useEffect, useRef, useState } from "react";
import { Check, Eye, EyeOff, KeyRound, LoaderCircle, Settings2, X } from "lucide-react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";
import { useAppStore } from "../store/useAppStore";
import type { Locale } from "../store/useUIStore";
import { api } from "../utils/api";
import type { Settings } from "../types";

// 侧栏"分析引擎"区块：model-line 状态按钮 + 设置面板（API Key / 思考模式 /
// 推理强度 / 连接测试 / 保存）。提取自 App.tsx，所有状态仍走 Zustand store，
// 测试连接的 testingKey / testResult 为组件本地状态（无需跨组件共享）。
function SettingsPanel() {
  const { locale, t, setLocale } = useI18n();
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
  const testRequest = useRef<AbortController | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!keyOpen || !dialog) return;
    dialog.showModal();
    return () => dialog.close();
  }, [keyOpen]);

  function resetConnectionTest() {
    testRequest.current?.abort();
    testRequest.current = null;
    setTestingKey(false);
    setTestResult(null);
  }

  function closeSettings() {
    resetConnectionTest();
    dialogRef.current?.close();
    setKeyOpen(false);
    setApiKey("");
    setShowKey(false);
  }

  async function testConnection() {
    const request = new AbortController();
    testRequest.current = request;
    setTestingKey(true);
    setTestResult(null);
    const timeout = window.setTimeout(() => {
      if (testRequest.current !== request) return;
      request.abort();
      setTestResult({ ok: false, message: "测试连接超时，请重试。" });
    }, 10000);
    try {
      const response = await api<{ message: string }>("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey.trim() || undefined }),
        signal: request.signal,
      });
      if (!request.signal.aborted) setTestResult({ ok: true, message: response.message });
    } catch (err) {
      if (!request.signal.aborted) {
        setTestResult({ ok: false, message: err instanceof Error ? err.message : "连接失败" });
      }
    } finally {
      window.clearTimeout(timeout);
      if (testRequest.current === request) {
        testRequest.current = null;
        setTestingKey(false);
      }
    }
  }

  async function saveSettings() {
    if (savingSettings || testingKey) return;
    setError("");
    setSavingSettings(true);
    try {
      const payload = await api<Settings>("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey.trim() || undefined,
          thinking_enabled: thinking,
          reasoning_effort: effort,
          persist_key: true,
        }),
      });
      setSettings(payload);
      closeSettings();
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
      <span className="sidebar-label">{t("nav.analysisEngine")}</span>
      <button type="button" className="model-line" aria-haspopup="dialog" aria-expanded={keyOpen} onClick={() => setKeyOpen(true)}>
        <span>
          <i className={settings?.configured ? "online" : ""} />
          <span><strong>{settings?.model || "deepseek-chat"}</strong><small>{settings?.configured ? t("nav.configured") : t("nav.waitingConfig")}</small></span>
        </span>
        <Settings2 size={15} />
      </button>
      {keyOpen && createPortal(
        <dialog
          ref={dialogRef}
          className="settings-dialog"
          aria-labelledby="model-settings-title"
          onCancel={(event) => { event.preventDefault(); closeSettings(); }}
          onClick={(event) => { if (event.target === event.currentTarget) closeSettings(); }}
        >
          <form className="settings-form" autoComplete="off" onSubmit={(event) => { event.preventDefault(); void saveSettings(); }}>
          <div className="settings-title">
            <h2 id="model-settings-title">{t("settings.title")}</h2>
            <button type="button" title={t("settings.closeHint")} aria-label={t("settings.close")} onClick={closeSettings}><X size={15} /></button>
          </div>
          <label htmlFor="model-api-key">API Key</label>
          <div className="secret-input">
            <KeyRound size={14} />
            <input
              id="model-api-key"
              type={showKey ? "text" : "password"}
              autoComplete="new-password"
              spellCheck={false}
              value={apiKey}
              onChange={(event) => { resetConnectionTest(); setApiKey(event.target.value); }}
              placeholder={settings?.configured ? "留空以使用当前密钥" : "输入 DeepSeek Key"}
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
          {/* 界面语言：零依赖 i18n，选择后立即生效并写入 localStorage */}
          <label htmlFor="ui-language">{t("settings.language")}</label>
          <select
            id="ui-language"
            className="language-select"
            value={locale}
            onChange={(event) => setLocale(event.target.value as Locale)}
          >
            <option value="zh-CN">{t("settings.zh")}</option>
            <option value="en-US">{t("settings.en")}</option>
          </select>
          {/* 连接测试结果：成功绿色、失败红色，提示文案来自后端 warning / detail */}
          {testResult && (
            <div className={`test-result ${testResult.ok ? "ok" : "fail"}`} role="status">
              {testResult.ok ? "✓ " : "✗ "}{testResult.message}
            </div>
          )}
          {/* 操作区：测试连接（次操作）+ 保存（主操作）并排展示 */}
          <div className="settings-actions">
            <button type="button" className="test-button" onClick={testConnection} disabled={testingKey || savingSettings || (!apiKey.trim() && !settings?.configured)}>
              {testingKey ? "测试中…" : "测试连接"}
            </button>
            <button type="submit" className="save-button" disabled={savingSettings || testingKey}>
              {savingSettings ? <><LoaderCircle size={14} className="spin" />保存中…</> : <><Check size={14} />保存设置</>}
            </button>
          </div>
          </form>
        </dialog>,
        document.body,
      )}
    </div>
  );
}

export default SettingsPanel;
