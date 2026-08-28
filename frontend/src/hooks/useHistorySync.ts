import { useCallback, useEffect } from "react";
import { api } from "../utils/api";
import { useHistoryStore } from "../store/useHistoryStore";
import { useSessionStore } from "../store/useSessionStore";
import type { HistorySessionItem } from "../types";

// /api/sessions GET 列表响应
interface SessionListResponse {
  sessions?: HistorySessionItem[];
}

interface UseHistorySyncOptions {
  authReady: boolean;
  authRequired: boolean;
  authenticated: boolean;
  running: boolean;
}

// 历史会话列表同步 hook：fetchHistory + 两个自动刷新时机。
// 提取自 App.tsx，行为与原实现完全一致：
// - fetchHistory(manual)：manual=true 表示用户主动触发（点刷新按钮），
//   才设置 historyLoading 让按钮转圈；轮询调用 manual=false，不触发
//   按钮 disabled，避免 30 秒一次的轮询让整个历史列表短暂瘫痪。
// - 分析结束（running 转 false）时刷新一次，把当前会话的最新状态
//   同步到侧边栏（产物数、状态、相对时间）。
// - 历史列表自动轮询：默认 30 秒刷新一次（保持相对时间新鲜），
//   有分析在跑时缩短到 5 秒——让"运行中"圆点能及时变成"已完成"。
//   后台 tab 时暂停轮询节省请求。不在 running 时也轮询是为了：
//   用户在另一个 tab 启动分析，回到本 tab 时列表能反映最新状态；
//   相对时间"3 分钟前"也需要定期刷新才准确。
export function useHistorySync({ authReady, authRequired, authenticated, running }: UseHistorySyncOptions) {
  const setHistory = useHistoryStore((s) => s.setHistory);
  const setHistoryLoading = useHistoryStore((s) => s.setHistoryLoading);
  const setHistoryError = useHistoryStore((s) => s.setHistoryError);
  // 分析结束刷新需要判断当前是否有会话；deps 故意只留 running（原实现同），
  // session 走 zustand 订阅，无需进入依赖数组。
  const session = useSessionStore((s) => s.session);

  const fetchHistory = useCallback(async (manual = false) => {
    if (manual) setHistoryLoading(true);
    try {
      const payload = await api<SessionListResponse>("/api/sessions?limit=30");
      setHistory(payload.sessions || []);
      setHistoryError(false);
    } catch {
      // 加载失败不阻塞主流程，但记录错误状态，让用户能区分"没数据"
      // 和"加载失败"，并提供重试入口（之前是完全静默，用户有几十个
      // 会话却看到"还没有历史会话"，会以为数据丢了）。
      setHistoryError(true);
    } finally {
      if (manual) setHistoryLoading(false);
    }
  }, [setHistory, setHistoryLoading, setHistoryError]);

  // 分析结束（running 转 false）时刷新历史。
  // deps 故意只留 running（原实现同）：running 翻转时用最新闭包执行，
  // session 的中间变化不触发刷新。
  useEffect(() => {
    if (!running && session) fetchHistory();
  }, [running]);

  // 历史会话列表自动轮询
  useEffect(() => {
    if (!authReady || (authRequired && !authenticated)) return undefined;
    const interval = running ? 5000 : 30000;
    const poll = () => {
      if (document.hidden) return;
      fetchHistory();
    };
    const timer = window.setInterval(poll, interval);
    // 回到前台时立即刷新一次，避免等待下一个 interval tick
    const onVisible = () => { if (!document.hidden) fetchHistory(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [authReady, authRequired, authenticated, running, fetchHistory]);

  return { fetchHistory };
}

export default useHistorySync;
