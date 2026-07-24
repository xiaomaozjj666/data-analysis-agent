import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../store/useAppStore";
import { api, describeApiError, requestHeaders } from "../utils/api";
import { API_URL, PREVIEW_CACHE_MAX } from "../constants";
import type { Artifact, Session } from "../types";

interface UseArtifactPreviewResult {
  openArtifactPreview: (item: Artifact) => Promise<void>;
  closeArtifactPreview: () => void;
  loadCompareChart: (item: Artifact) => Promise<void>;
  downloadPng: () => void;
  editChart: () => Promise<void>;
  // 图表内联编辑表单状态（标题 / 主色 / 保存中标记 / 面板开关）
  chartEditOpen: boolean;
  setChartEditOpen: React.Dispatch<React.SetStateAction<boolean>>;
  chartEditTitle: string;
  setChartEditTitle: React.Dispatch<React.SetStateAction<string>>;
  chartEditColor: string;
  setChartEditColor: React.Dispatch<React.SetStateAction<string>>;
  chartEditSaving: boolean;
  // 预览增强状态：全屏 / 对比 / PNG 导出
  previewFullscreen: boolean;
  setPreviewFullscreen: React.Dispatch<React.SetStateAction<boolean>>;
  compareMode: boolean;
  setCompareMode: React.Dispatch<React.SetStateAction<boolean>>;
  compareItem: Artifact | null;
  compareHtml: string;
  compareLoading: boolean;
  pngDownloading: boolean;
}

// 产物预览 hook：提取自 App.tsx 的图表预览模态逻辑——
// openArtifactPreview / closeArtifactPreview / loadCompareChart / downloadPng /
// editChart，以及预览全屏 / 对比 / PNG 导出 / 图表内联编辑等本地状态。
// 行为与原 App.tsx 完全一致：useCallback 依赖数组保持原样，预览 LRU 缓存与
// 控制器 ref 为 hook 内部状态（不导出，仅各 callback 共享）。
function useArtifactPreview(): UseArtifactPreviewResult {
  // 所有 UI 状态从 Zustand store 获取，与原 App.tsx 同源。
  const {
    session, previewItem,
    setPreviewItem, setPreviewHtml, setPreviewLoading, setPreviewError,
    setSession, setError,
  } = useAppStore();

  const previewController = useRef<AbortController | null>(null);
  // 预览 HTML LRU 缓存：每个图表 HTML 完全自包含（含 Plotly.js ~3.5MB），
  // 重复打开同一图表时秒开，避免重新 fetch + 解析。最多缓存 5 条。
  const previewCacheRef = useRef<Map<string, string>>(new Map());

  // === Batch 4：图表内联编辑 ===
  // 预览模态中可对图表产物进行标题/主色就地编辑，
  // 调用 PUT /api/sessions/{id}/artifacts/{filename}/edit 更新 HTML 和 JSON。
  const [chartEditOpen, setChartEditOpen] = useState(false);
  const [chartEditTitle, setChartEditTitle] = useState("");
  const [chartEditColor, setChartEditColor] = useState("#245C55");
  const [chartEditSaving, setChartEditSaving] = useState(false);

  // === Batch B1：图表预览增强 ===
  // previewFullscreen：全屏展示图表（#17）
  // compareMode/compareItem/compareHtml/compareLoading：图表对比并排展示（#22）
  // pngDownloading：通过 postMessage 通道触发 iframe 内图表导出 PNG（#23）
  const [previewFullscreen, setPreviewFullscreen] = useState(false);
  const [compareMode, setCompareMode] = useState(false);
  const [compareItem, setCompareItem] = useState<Artifact | null>(null);
  const [compareHtml, setCompareHtml] = useState("");
  const [compareLoading, setCompareLoading] = useState(false);
  const [pngDownloading, setPngDownloading] = useState(false);

  // useCallback：openArtifactPreview / downloadArtifact 作为 props 传给
  // React.memo(ArtifactCenter)。若每次渲染都创建新函数，memo 比较失败，
  // ArtifactCenter 仍然每次重渲染。useCallback 让函数身份稳定，memo 才
  // 能真正跳过无关重渲染。
  const openArtifactPreview = useCallback(async (item: Artifact) => {
    if (!item.preview_url) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setPreviewItem(item);
    setPreviewError("");
    // 打开新预览时重置对比/全屏状态，避免上一张图的对比布局残留到新图。
    setCompareMode(false);
    setCompareItem(null);
    setCompareHtml("");
    setPreviewFullscreen(false);
    // LRU 缓存命中：直接展示已加载的 HTML，跳过 fetch + 解析（Plotly.js ~3.5MB）。
    // 重复打开同一图表时秒开，多个图表来回切换无需重新加载。
    const cacheKey = item.preview_url;
    const cached = previewCacheRef.current.get(cacheKey);
    if (cached !== undefined) {
      // LRU：删除再插入，将命中条目移到 Map 末尾标记为最近使用
      previewCacheRef.current.delete(cacheKey);
      previewCacheRef.current.set(cacheKey, cached);
      setPreviewHtml(cached);
      setPreviewLoading(false);
      return;
    }
    setPreviewHtml("");
    setPreviewLoading(true);
    try {
      // 预览仍使用请求头鉴权，主访问令牌不会进入 URL、历史记录或服务器 access log。
      // 服务端返回完全离线的文档，再交给无同源权限的 sandbox iframe 执行。
      const response = await fetch(`${API_URL}${item.preview_url}`, {
        headers: requestHeaders(),
        signal: controller.signal,
      });
      const html = await response.text();
      if (!response.ok) {
        let payload: unknown = html;
        try {
          payload = JSON.parse(html);
        } catch {
          // 非 JSON 错误正文交给统一错误描述处理。
        }
        throw new Error(describeApiError(payload, response.status));
      }
      // 缓存结果：超过上限时淘汰 Map 中最旧（最久未使用）的条目
      previewCacheRef.current.set(cacheKey, html);
      if (previewCacheRef.current.size > PREVIEW_CACHE_MAX) {
        const oldest = previewCacheRef.current.keys().next().value;
        if (oldest !== undefined) previewCacheRef.current.delete(oldest);
      }
      setPreviewHtml(html);
      // loading 状态由 iframe onLoad 关闭，确保用户看到的是完成渲染的图表。
    } catch (err) {
      const error = err as Error;
      if (error.name !== "AbortError") {
        setPreviewLoading(false);
        setPreviewError(`图表加载失败：${error.message}`);
      }
    } finally {
      if (previewController.current === controller) previewController.current = null;
    }
  }, []);

  const closeArtifactPreview = useCallback(() => {
    previewController.current?.abort();
    previewController.current = null;
    setPreviewItem(null);
    setPreviewHtml("");
    setPreviewLoading(false);
    setPreviewError("");
    // 关闭预览时清空对比/全屏状态，下次打开时干净起步。
    setCompareMode(false);
    setCompareItem(null);
    setCompareHtml("");
    setCompareLoading(false);
    setPreviewFullscreen(false);
  }, []);

  // 加载对比图表：复用预览 LRU 缓存，命中则秒开，否则 fetch 后入缓存。
  // 与主预览共享 previewCacheRef，切换主图与对比图时互不重复请求。
  const loadCompareChart = useCallback(async (item: Artifact) => {
    if (!item.preview_url) return;
    setCompareItem(item);
    setCompareLoading(true);
    setCompareHtml("");
    const cacheKey = item.preview_url;
    const cached = previewCacheRef.current.get(cacheKey);
    if (cached !== undefined) {
      // LRU：删除再插入，将命中条目移到 Map 末尾标记为最近使用
      previewCacheRef.current.delete(cacheKey);
      previewCacheRef.current.set(cacheKey, cached);
      setCompareHtml(cached);
      setCompareLoading(false);
      return;
    }
    try {
      const response = await fetch(`${API_URL}${item.preview_url}`, {
        headers: requestHeaders(),
      });
      const html = await response.text();
      if (response.ok) {
        previewCacheRef.current.set(cacheKey, html);
        if (previewCacheRef.current.size > PREVIEW_CACHE_MAX) {
          const oldest = previewCacheRef.current.keys().next().value;
          if (oldest !== undefined) previewCacheRef.current.delete(oldest);
        }
        setCompareHtml(html);
      }
    } catch {
      // 对比加载失败时不阻塞主图，静默忽略
    } finally {
      setCompareLoading(false);
    }
  }, []);

  // 通过 postMessage 通道让 iframe 内图表导出 PNG（#23）。
  // iframe sandbox 只保留 allow-scripts（不含 allow-same-origin），
  // 因此不能用 contentDocument 直接读取，改由图表脚本回传 dataURL。
  // 5 秒超时兜底，避免图表脚本未注册监听时按钮永远 disabled。
  const downloadPng = useCallback(() => {
    const iframe = document.querySelector<HTMLIFrameElement>(".preview-frame iframe");
    if (!iframe?.contentWindow) return;
    setPngDownloading(true);
    const handler = (e: MessageEvent) => {
      if (e.data?.type === "png-data" && e.data.data) {
        const a = document.createElement("a");
        a.href = e.data.data as string;
        a.download = `${previewItem?.name?.replace(/\.html$/, "") || "chart"}.png`;
        a.click();
        window.removeEventListener("message", handler);
        setPngDownloading(false);
      }
    };
    window.addEventListener("message", handler);
    iframe.contentWindow.postMessage({ type: "download-png" }, "*");
    // 超时兜底：5 秒未收到回传则解绑监听并恢复按钮状态
    window.setTimeout(() => {
      window.removeEventListener("message", handler);
      setPngDownloading(false);
    }, 5000);
  }, [previewItem]);

  // === Batch 4：图表内联编辑 ===
  // 修改图表标题或主色，后端更新 HTML 和 JSON 产物；前端清除预览缓存后重新加载。
  const editChart = useCallback(async () => {
    if (!session || !previewItem) return;
    setChartEditSaving(true);
    try {
      const response = await fetch(`${API_URL}/api/sessions/${session.id}/artifacts/${previewItem.name}/edit`, {
        method: "PUT",
        headers: requestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title: chartEditTitle || undefined, color: chartEditColor || undefined }),
      });
      if (!response.ok) throw new Error("图表编辑失败");
      // 清除预览缓存：下次打开会重新拉取更新后的 HTML
      previewCacheRef.current.clear();
      setPreviewHtml("");
      setChartEditOpen(false);
      // 重新打开预览，加载更新后的图表
      openArtifactPreview(previewItem);
      // 刷新 session 以获取更新的 artifacts（描述等可能被后端覆盖）
      const updated = await api<Session>(`/api/sessions/${session.id}`);
      setSession(updated);
    } catch (err) {
      setError(`图表编辑失败：${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setChartEditSaving(false);
    }
  }, [session, previewItem, chartEditTitle, chartEditColor, openArtifactPreview]);

  // 打开预览时初始化编辑表单（标题取自产物描述，颜色用默认品牌色）
  useEffect(() => {
    if (previewItem) {
      setChartEditTitle(previewItem.description || previewItem.name);
      setChartEditColor("#245C55");
      setChartEditOpen(false);
    }
  }, [previewItem]);

  return {
    openArtifactPreview,
    closeArtifactPreview,
    loadCompareChart,
    downloadPng,
    editChart,
    chartEditOpen, setChartEditOpen,
    chartEditTitle, setChartEditTitle,
    chartEditColor, setChartEditColor,
    chartEditSaving,
    previewFullscreen, setPreviewFullscreen,
    compareMode, setCompareMode,
    compareItem,
    compareHtml,
    compareLoading,
    pngDownloading,
  };
}

export default useArtifactPreview;
