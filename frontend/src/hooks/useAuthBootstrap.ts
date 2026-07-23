import { useEffect } from "react";
import { api } from "../utils/api";
import type { Settings } from "../types";

// /api/auth 返回的轻量结构（原 App.tsx 内的局部 interface，随 effect 一并迁入）。
interface AuthStatus {
  required?: boolean;
  authenticated?: boolean;
}

interface UseAuthBootstrapDeps {
  setAuthRequired: (v: boolean) => void;
  setAuthenticated: (v: boolean) => void;
  setAuthReady: (v: boolean) => void;
  setSettings: (v: Settings) => void;
  setEffort: (v: string) => void;
  setThinking: (v: boolean) => void;
  setKeyOpen: (v: boolean) => void;
  setError: (v: string) => void;
  fetchHistory: () => void;
  authReady: boolean;
  authRequired: boolean;
  authenticated: boolean;
}

// 认证引导 hook：合并原 App.tsx 中两个 useEffect——
//   1. 首次挂载拉取 /api/auth → /api/settings，设置认证与配置状态（原 deps []）
//   2. 鉴权就绪后拉取历史会话列表（原 deps [authReady, authRequired, authenticated]）
// 行为与原实现完全一致，依赖数组保持原样。
function useAuthBootstrap(deps: UseAuthBootstrapDeps): void {
  const {
    setAuthRequired, setAuthenticated, setAuthReady,
    setSettings, setEffort, setThinking, setKeyOpen, setError,
    fetchHistory, authReady, authRequired, authenticated,
  } = deps;

  // 初次挂载：探测后端鉴权要求，按需拉取 settings。失败时仍标记 authReady
  // 让 UI 不至于永久卡在"正在连接…"，并展示连接错误。依赖 [] —— 仅挂载时
  // 执行一次；用到的 setter 均为 Zustand 稳定引用，省略与原行为一致。
  useEffect(() => {
    api<AuthStatus>("/api/auth")
      .then((status) => {
        setAuthRequired(!!status.required);
        setAuthenticated(!!status.authenticated);
        if (!status.required || status.authenticated) return api<Settings>("/api/settings");
        return null;
      })
      .then((value) => {
        if (value) {
          setSettings(value);
          setEffort(value.reasoning_effort);
          setThinking(value.thinking_enabled);
          setKeyOpen(!value.configured);
        }
        setAuthReady(true);
      })
      .catch((err: Error) => {
        setAuthReady(true);
        setError(`后端连接失败：${err.message}`);
      });
    // 依赖与原 App.tsx 保持一致：setter 稳定，省略。
  }, []);

  // 鉴权通过后立即拉取一次历史会话列表，让用户初次进入就能看到之前的会话。
  // 依赖与原 App.tsx 保持一致 [authReady, authRequired, authenticated]；
  // fetchHistory 刻意省略（每次渲染重建，但内部仅用稳定 setter，省略无副作用）。
  useEffect(() => {
    if (authReady && (!authRequired || authenticated)) fetchHistory();
  }, [authReady, authRequired, authenticated]);
}

export default useAuthBootstrap;
