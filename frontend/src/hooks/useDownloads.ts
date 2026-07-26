import { useCallback, useState } from "react";
import { useAppStore } from "../store/useAppStore";
import { describeApiError, requestHeaders } from "../utils/api";
import { wait } from "../utils/format";
import { API_URL } from "../constants";
import type { Artifact, HistorySessionItem } from "../types";

interface UseDownloadsResult {
  downloadArtifact: (item: Artifact) => Promise<void>;
  batchDownload: (items: Artifact[]) => Promise<void>;
  exportSession: (item: HistorySessionItem) => Promise<void>;
  // 正在导出的会话 id：HistoryPanel 据此在对应条目上显示 loading，
  // 导出 ZIP 需后端打包，大会话可能耗时数秒，无反馈会让用户重复点击。
  exportingSessionId: string | null;
}

// 下载相关逻辑提取自 App.tsx：单产物下载 / 批量下载 / 会话导出。
// 三个 callback 均只依赖 setError（store setter 身份稳定），行为与原实现一致。
function useDownloads(): UseDownloadsResult {
  const { setError } = useAppStore();
  const [exportingSessionId, setExportingSessionId] = useState<string | null>(null);

  // useCallback：downloadArtifact 作为 props 传给 React.memo(ArtifactCenter)。
  // 若每次渲染都创建新函数，memo 比较失败，ArtifactCenter 仍然每次重渲染。
  // useCallback 让函数身份稳定，memo 才能真正跳过无关重渲染。
  const downloadArtifact = useCallback(async (item: Artifact) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetch(`${API_URL}${item.download_url}`, {
        headers: requestHeaders(),
        signal: controller.signal,
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const payload: unknown = contentType.includes("application/json")
          ? await response.json().catch(() => ({}))
          : await response.text().catch(() => "");
        throw new Error(describeApiError(payload, response.status));
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = item.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      const error = err as Error;
      setError(`下载失败：${error.name === "AbortError" ? "下载超时，请稍后重试。" : error.message}`);
    } finally {
      window.clearTimeout(timeout);
    }
  }, []);

  // 批量下载：用户在产物中心选中多张图表后一次性触发下载。
  // 顺序触发（间隔 300ms）避免浏览器并发下载限制和后端瞬时压力；
  // 任一文件失败不中断后续，最终汇总成功/失败数量。
  const batchDownload = useCallback(async (items: Artifact[]) => {
    if (!items.length) return;
    let succeeded = 0;
    // 记录失败文件名：只报数量用户无从知道该重下哪几个，最多列 3 个。
    const failedNames: string[] = [];
    for (const item of items) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 120000);
      try {
        const response = await fetch(`${API_URL}${item.download_url}`, {
          headers: requestHeaders(),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = item.name;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(link.href);
        succeeded += 1;
      } catch {
        failedNames.push(item.name);
      } finally {
        window.clearTimeout(timeout);
      }
      // 浏览器需要时间处理每次下载对话框，间隔过短会被合并或丢弃。
      await wait(300);
    }
    if (failedNames.length > 0) {
      const preview = failedNames.slice(0, 3).join("、");
      const suffix = failedNames.length > 3 ? ` 等 ${failedNames.length} 个文件` : "";
      setError(`批量下载完成：成功 ${succeeded} 个，失败 ${failedNames.length} 个（${preview}${suffix}）。可在产物中心单独重新下载失败项。`);
    }
  }, []);

  // === Batch 4：会话导出 ===
  // 导出会话为 ZIP：浏览器侧生成下载链接，文件名格式 `<filename>_<id>.zip`。
  // requestHeaders() 不带 Content-Type，避免后端按 JSON 解析二进制流。
  const exportSession = useCallback(async (item: HistorySessionItem) => {
    setExportingSessionId(item.id);
    try {
      const response = await fetch(`${API_URL}/api/sessions/${item.id}/export`, {
        headers: requestHeaders(),
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const payload: unknown = contentType.includes("application/json")
          ? await response.json().catch(() => ({}))
          : await response.text().catch(() => "");
        throw new Error(describeApiError(payload, response.status));
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${item.filename || "session"}_${item.id}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(`导出会话失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setExportingSessionId(null);
    }
  }, []);

  return { downloadArtifact, batchDownload, exportSession, exportingSessionId };
}

export default useDownloads;
