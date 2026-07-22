import React, { useState } from "react";
import { api } from "../utils/api";
import { ACCESS_TOKEN_KEY } from "../constants";

interface AccessGateProps {
  onAuthenticated: () => void;
}

interface AuthStatus {
  required?: boolean;
  authenticated?: boolean;
  [key: string]: unknown;
}

function AccessGate({ onAuthenticated }: AccessGateProps) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setMessage("");
    window.localStorage.setItem(ACCESS_TOKEN_KEY, token.trim());
    try {
      const status = await api<AuthStatus>("/api/auth");
      if (!status.authenticated) throw new Error("访问令牌无效。");
      onAuthenticated();
    } catch (error) {
      const err = error as Error;
      window.localStorage.removeItem(ACCESS_TOKEN_KEY);
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-gate">
      <form className="auth-card" onSubmit={submit}>
        <span className="section-kicker">DATA DESK</span>
        <h1>进入数据工作台</h1>
        <p>请输入部署管理员提供的访问令牌。</p>
        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="应用访问令牌"
          autoFocus
        />
        <button className="primary" type="submit" disabled={busy || !token.trim()}>
          {busy ? "验证中" : "进入工作台"}
        </button>
        {message && <small className="auth-error">{message}</small>}
      </form>
    </main>
  );
}

interface MetricProps {
  label: string;
  value: React.ReactNode;
  unit?: string;
}

function Metric({ label, value, unit }: MetricProps) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}<small>{unit}</small></strong>
    </div>
  );
}

export { AccessGate, Metric };
