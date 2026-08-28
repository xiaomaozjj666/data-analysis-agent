import { useCallback } from "react";
import type { RefObject } from "react";
import { API_URL } from "../constants";
import { api, describeApiError, requestHeaders } from "../utils/api";
import { useAppStore } from "../store/useAppStore";
import type { HistorySessionItem, Session } from "../types";

interface UseSessionMutationsOptions {
  // 切换到指定会话（App.tsx 的 selectSession）：导入会话成功后跳转。
  selectSession: (item: HistorySessionItem) => Promise<void>;
  // 历史刷新回调（useHistorySync 的 fetchHistory）：增删改后保持列表新鲜。
  fetchHistory: (manual?: boolean) => Promise<void>;
  // 删除当前会话时需要中断的三条进行中请求（重试轮询 / 分析 SSE / 追问 SSE）。
  retryController: RefObject<AbortController | null>;
  analysisController: RefObject<AbortController | null>;
  chatControllerRef: RefObject<AbortController | null>;
}

// 会话增删改 hook：导入 / 删除 / 重命名历史会话。提取自 App.tsx，
// 行为与原实现完全一致；所有状态 setter 从 store 获取（与
// useArtifactPreview 同模式），App 只保留 selectSession 本体。
export function useSessionMutations({
  selectSession, fetchHistory, retryController, analysisController, chatControllerRef,
}: UseSessionMutationsOptions) {
  const {
    session, history,
    setSession, setTask, setPlan, setCompleted, setResult,
    setCurrentNodeTitle, setRetryOffer, setFollowUps,
    setAwaitingApproval, setPendingObjective, setStepProgress,
    setUploading, setError,
    setHistory,
  } = useAppStore();

  // 导入会话：multipart 上传 ZIP，后端返回完整 session payload；
  // 成功后刷新历史并切到新会话。FormData 不能带 Content-Type，
  // 浏览器会自动设置 multipart/form-data 边界。
  const importSession = useCallback(async (file: File) => {
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetch(`${API_URL}/api/sessions/import`, {
        method: "POST",
        headers: requestHeaders(),
        body: formData,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string };
        throw new Error(payload.detail || "导入失败");
      }
      const sessionPayload = await response.json() as Session;
      await fetchHistory();
      selectSession({ id: sessionPayload.id, filename: sessionPayload.filename, analysis_status: sessionPayload.analysis_status } as HistorySessionItem);
    } catch (err) {
      setError(`导入会话失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setUploading(false);
    }
  }, [fetchHistory, selectSession, setUploading, setError]);

  // 删除会话：调用 DELETE 端点清理服务端数据，成功后刷新历史列表。
  // 若删除的是当前正在查看的会话，清空前端状态回到空状态，让用户
  // 重新上传数据开始新分析，避免停留在已失效的会话视图上。
  const deleteSession = useCallback(async (item: HistorySessionItem) => {
    try {
      const response = await fetch(`${API_URL}/api/sessions/${item.id}`, {
        method: "DELETE",
        headers: requestHeaders(),
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const payload: unknown = contentType.includes("application/json")
          ? await response.json().catch(() => ({}))
          : await response.text().catch(() => "");
        throw new Error(describeApiError(payload, response.status));
      }
      // 删除的是当前会话：清空状态回到空工作台
      if (session?.id === item.id) {
        setSession(null);
        setResult(null);
        setPlan([]);
        setCompleted([]);
        setCurrentNodeTitle("");
        setRetryOffer(null);
        setFollowUps([]);
        setAwaitingApproval(false);
        setPendingObjective("");
        setStepProgress(null);
        setTask("");
        retryController.current?.abort();
        analysisController.current?.abort();
        chatControllerRef.current?.abort();
      }
      fetchHistory();
    } catch (err) {
      setError(`删除会话失败：${err instanceof Error ? err.message : "未知错误"}`);
    }
  }, [session?.id, fetchHistory, retryController, analysisController, chatControllerRef, setSession, setResult, setPlan, setCompleted, setCurrentNodeTitle, setRetryOffer, setFollowUps, setAwaitingApproval, setPendingObjective, setStepProgress, setTask, setError]);

  // 重命名会话：PATCH 更新服务端 title，乐观更新本地 history 列表与当前 session。
  // 空串视为清除自定义标题（回退 filename），后端会存 None。
  const renameSession = useCallback(async (item: HistorySessionItem, title: string) => {
    try {
      const response = await fetch(`${API_URL}/api/sessions/${item.id}`, {
        method: "PATCH",
        headers: { ...requestHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string };
        throw new Error(payload.detail || "重命名失败");
      }
      const data = await response.json() as { title: string };
      // 乐观更新 history 列表中该 item 的 title
      const updated = (history || []).map((s) => s.id === item.id ? { ...s, title: data.title || undefined } : s);
      setHistory(updated);
      // 当前会话同步更新标题
      if (session?.id === item.id) {
        setSession((prev) => prev ? { ...prev, title: data.title || undefined } : prev);
      }
    } catch (err) {
      // 失败时把用户输入的新名字带在提示里：编辑框已关闭，不说明哪个
      // 名字没保存，用户得重新想一遍刚才输了什么。
      setError(`重命名会话失败，新名称「${title}」未保存，可重试：${err instanceof Error ? err.message : "未知错误"}`);
    }
  }, [session?.id, history, setHistory, setSession, setError]);

  return { importSession, deleteSession, renameSession };
}

export default useSessionMutations;
