import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore, useUIStore } from "../store/useAppStore";
import { api, describeApiError, requestHeaders } from "../utils/api";
import { API_URL, PREVIEW_CACHE_MAX } from "../constants";
import type { Artifact, Session } from "../types";

// 编辑请求体构造：颜色只在用户主动修改过色块时才随请求发送。图表
// 生成时按分类分配多色（如按品类着色的每组一个颜色），编辑表单的
// 色块默认值是品牌单色——若"只改标题"也把默认色发给后端，后端会
// 把整张图的所有 trace 刷成同一颜色，分组配色永久丢失。
export function buildChartEditPayload(
  title: string | undefined,
  color: string | undefined,
  colorTouched: boolean,
): Record<string, string> {
  const payload: Record<string, string> = {};
  if (title) payload.title = title;
  if (colorTouched && color) payload.color = color;
  return payload;
}

export interface UseArtifactPreviewResult {
  openArtifactPreview: (item: Artifact) => Promise<void>;
  closeArtifactPreview: () => void;
  loadCompareChart: (item: Artifact) => Promise<void>;
  downloadPng: () => void;
  editChart: () => Promise<void>;
  // iframe onLoad 回调：清掉 openArtifactPreview 启动的兜底超时定时器，
  // 表示 iframe 已成功加载（无论图表脚本是否渲染完成，至少文档框架已就绪）。
  onPreviewIframeLoaded: () => void;
  // 图表内联编辑表单状态（标题 / 主色 / 保存中标记 / 面板开关）
  chartEditOpen: boolean;
  setChartEditOpen: React.Dispatch<React.SetStateAction<boolean>>;
  chartEditTitle: string;
  setChartEditTitle: React.Dispatch<React.SetStateAction<string>>;
  chartEditColor: string;
  setChartEditColor: React.Dispatch<React.SetStateAction<string>>;
  // 用户是否主动改过主色（未动过时编辑请求不携带 color，避免覆盖分组配色）
  setChartEditColorTouched: React.Dispatch<React.SetStateAction<boolean>>;
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
  const previewCacheRef = useRef<Map<string, { html: string; etag: string }>>(new Map());
  // iframe onLoad 超时定时器：若 iframe 加载超时未触发 onLoad，则标记为失败，
  // 避免 loading 状态永久卡住（曾导致预览空白无任何提示）。
  const loadTimeoutRef = useRef<number | null>(null);

  // === Batch 4：图表内联编辑 ===
  // 预览模态中可对图表产物进行标题/主色就地编辑，
  // 调用 PUT /api/sessions/{id}/artifacts/{filename}/edit 更新 HTML 和 JSON。
  const [chartEditOpen, setChartEditOpen] = useState(false);
  const [chartEditTitle, setChartEditTitle] = useState("");
  const [chartEditColor, setChartEditColor] = useState("#245C55");
  // 用户是否主动改过主色：编辑表单的色块默认值是品牌色，改标题时若
  // 未经用户确认就把默认色发给后端，会把图表原本的分组配色（如按
  // 品类着色的 4 个颜色）全部刷成同一个颜色。
  const [chartEditColorTouched, setChartEditColorTouched] = useState(false);
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

  // 校验预览 HTML 是否为后端内联 JS 库后的自包含文档。
  // 历史问题：旧版本后端未内联 echarts.min.js / plotly.min.js，iframe 在
  // about:blank 环境下无法解析相对路径脚本导致图表空白；前端 LRU 缓存又
  // 持续复用这份坏 HTML，用户每次点开都空白。这里在写入缓存和 state 前
  // 做一次防御性校验：若 HTML 仍引用外部脚本或缺少关键库标记，丢弃并报错。
  const isValidPreviewHtml = useCallback((html: string): boolean => {
    if (!html || html.length < 200) return false;
    // 必须包含内联的 ECharts 或 Plotly 库标记（后端 _inline_*_bundle 注入）
    const hasEcharts = html.includes("echarts.init") || html.includes("echarts.min.js");
    const hasPlotly = html.includes("Plotly.newPlot") || html.includes("plotly.min.js");
    if (!hasEcharts && !hasPlotly) return false;
    // 不能残留相对路径 <script src="xxx.min.js">：iframe srcdoc 无同源基础，
    // 相对路径会解析为 about:blank/xxx.min.js 永远 404，图表静默失败。
    if (/<script\s+src=["'](?!https?:|blob:|data:)[^"']*\.js["']/i.test(html)) {
      return false;
    }
    return true;
  }, []);

  // 图表运行时错误上报脚本：新版后端已在生成 HTML 时注入，但历史会话
  // 里的旧图表 HTML 不含该脚本。这里在展示前统一补注入（已含 'chart-error'
  // 标记的跳过），让历史会话的图表渲染失败时同样能回传具体错误。
  const withChartErrorReporter = useCallback((html: string): string => {
    if (html.includes("chart-error")) return html;
    const reporter =
      "<script>(function(){window.addEventListener('error',function(e){setTimeout(function(){" +
      "var ok=document.querySelector('.plotly-graph-div .main-svg')||window.__echartsInstance;" +
      "if(!ok){try{parent.postMessage({type:'chart-error',message:String((e&&e.message)||'图表脚本执行失败')},'*');}catch(_){}}" +
      "},300);});})();</" + "script>";
    return html.replace(/<head>/i, "<head>" + reporter);
  }, []);

  // 主题桥接（#1）：让 iframe 内图表主题与 React 顶栏统一，消灭"要点两次才能全暗"。
  // 统一在前端注入，无需重建历史 HTML——所有图表（新/历史）都内置了
  // #theme-toggle 按钮 + 监听 data-theme 变化的 MutationObserver，因此：
  //   父 → 子：父页面 postMessage({type:'set-theme'})，此桥接改写 data-theme，
  //           触发图表自身的 observer 重新应用主题；
  //   子 → 父：图表内按钮点击后回传 {type:'chart-theme-changed'}，父页面同步 React 主题。
  // 消息里都带显式 theme 值，父页面用 setTheme(值) 幂等赋值，重复消息不会来回抖动。
  const withThemeBridge = useCallback((html: string): string => {
    if (html.includes("chart-theme-bridge")) return html;
    const bridge =
      "<script>/*chart-theme-bridge*/(function(){" +
      "window.addEventListener('message',function(e){" +
      "if(e.data&&e.data.type==='set-theme'&&(e.data.theme==='dark'||e.data.theme==='light')){" +
      "if(document.documentElement.dataset.theme!==e.data.theme){" +
      "document.documentElement.dataset.theme=e.data.theme;" +
      "try{localStorage.setItem('echarts-theme',e.data.theme);}catch(_){}}}});" +
      "function bind(){var b=document.getElementById('theme-toggle');if(b){b.addEventListener('click',function(){" +
      "setTimeout(function(){try{parent.postMessage({type:'chart-theme-changed',theme:document.documentElement.dataset.theme||'light'},'*');}catch(_){}} ,60);});}}" +
      "if(document.readyState!=='loading'){bind();}else{document.addEventListener('DOMContentLoaded',bind);}" +
      "})();</" + "script>";
    return html.replace(/<head>/i, "<head>" + bridge);
  }, []);

  // 组合注入：错误上报 + 主题桥接，两者都幂等（含标记防重复注入）。
  const prepareHtml = useCallback(
    (html: string): string => withThemeBridge(withChartErrorReporter(html)),
    [withThemeBridge, withChartErrorReporter],
  );

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
    // 清理上一轮可能残留的 onLoad 超时定时器
    if (loadTimeoutRef.current !== null) {
      window.clearTimeout(loadTimeoutRef.current);
      loadTimeoutRef.current = null;
    }
    // LRU 缓存：命中后不再“无条件秒开”，而是带上 If-None-Match 做一次条件
    // 请求——文件没变时服务端返回 304（极小响应，避免重下 ~3.5MB 内联 HTML），
    // 文件一旦被重写（如重新生成图表）则 200 返回最新内容，缓存立即失效。
    // 这彻底消灭“URL 不变但文件已变、前端 LRU 长期复用旧乱码 HTML”的问题。
    const cacheKey = item.preview_url;
    const cached = previewCacheRef.current.get(cacheKey);
    setPreviewHtml("");
    setPreviewLoading(true);
    try {
      // 预览仍使用请求头鉴权，主访问令牌不会进入 URL、历史记录或服务器 access log。
      // 服务端返回完全离线的文档，再交给无同源权限的 sandbox iframe 执行。
      const reqHeaders = cached?.etag
        ? requestHeaders({ "If-None-Match": cached.etag })
        : requestHeaders();
      const response = await fetch(`${API_URL}${item.preview_url}`, {
        headers: reqHeaders,
        signal: controller.signal,
      });
      // 304：文件未变，直接复用内存缓存（已是编码正确的 HTML）。
      if (response.status === 304 && cached) {
        previewCacheRef.current.delete(cacheKey);
        previewCacheRef.current.set(cacheKey, cached); // LRU 触活
        setPreviewHtml(prepareHtml(cached.html));
        setPreviewLoading(false);
        return;
      }
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
      // 写入缓存前校验：拒绝缓存非自包含的 HTML，防止坏 HTML 在 LRU 中
      // 长期复用导致后续每次打开都空白。
      if (!isValidPreviewHtml(html)) {
        throw new Error("预览文档缺少内联图表库，请重新生成产物或刷新页面后重试。");
      }
      // 缓存结果：带上 ETag，下次打开时用于条件请求校验新鲜度。
      // 超过上限时淘汰 Map 中最旧（最久未使用）的条目。
      const etag = response.headers.get("etag") || "";
      previewCacheRef.current.set(cacheKey, { html, etag });
      if (previewCacheRef.current.size > PREVIEW_CACHE_MAX) {
        const oldest = previewCacheRef.current.keys().next().value;
        if (oldest !== undefined) previewCacheRef.current.delete(oldest);
      }
      setPreviewHtml(prepareHtml(html));
      // loading 状态由 iframe onLoad 关闭，确保用户看到的是完成渲染的图表。
      // 兜底超时：若 8 秒内 onLoad 未触发（极端情况下 iframe 静默失败），
      // 强制关闭 loading 并提示错误，避免用户盯着空白骨架屏。
      if (loadTimeoutRef.current !== null) {
        window.clearTimeout(loadTimeoutRef.current);
      }
      loadTimeoutRef.current = window.setTimeout(() => {
        loadTimeoutRef.current = null;
        setPreviewLoading(false);
        setPreviewError("图表加载超时，请检查网络或重新生成产物后重试。");
      }, 8000);
    } catch (err) {
      const error = err as Error;
      if (error.name !== "AbortError") {
        setPreviewLoading(false);
        setPreviewError(`图表加载失败：${error.message}`);
      }
    } finally {
      if (previewController.current === controller) previewController.current = null;
    }
  }, [isValidPreviewHtml, prepareHtml]);

  const closeArtifactPreview = useCallback(() => {
    previewController.current?.abort();
    previewController.current = null;
    // 关闭预览时清理 onLoad 超时定时器，避免定时器在模态关闭后仍触发错误态。
    if (loadTimeoutRef.current !== null) {
      window.clearTimeout(loadTimeoutRef.current);
      loadTimeoutRef.current = null;
    }
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

  // iframe onLoad 回调：由 App.tsx 的 <iframe onLoad> 调用。
  // 清掉 openArtifactPreview 启动的兜底超时定时器，并关闭 loading 状态。
  // 即使图表脚本因 CSP/sandbox 限制未能渲染，文档框架 onLoad 触发也意味着
  // iframe 本身没有静默失败，可信任后续图表脚本的异步渲染。
  const onPreviewIframeLoaded = useCallback(() => {
    if (loadTimeoutRef.current !== null) {
      window.clearTimeout(loadTimeoutRef.current);
      loadTimeoutRef.current = null;
    }
    setPreviewLoading(false);
  }, []);

  // 组件卸载时清理定时器和 AbortController，避免内存泄漏与卸载后 setState。
  useEffect(() => {
    return () => {
      if (loadTimeoutRef.current !== null) {
        window.clearTimeout(loadTimeoutRef.current);
        loadTimeoutRef.current = null;
      }
      previewController.current?.abort();
    };
  }, []);

  // 图表运行时错误上报：iframe 内图表脚本执行失败且画布未渲染时，
  // 后端注入的脚本会 postMessage({type:'chart-error', message}) 回传。
  // 这里转成可见的错误提示 + 重试按钮，消灭“图表静默空白”这一
  // 最难排查的失败形态；同时清掉坏 HTML 的预览缓存，重试时重新拉取。
  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (e.data?.type === "chart-error" && typeof e.data.message === "string") {
        // 用 slice store 的 getState 读当前预览项，避免把 previewItem 加进
        // 依赖数组导致监听器反复解绑/重绑。
        const item = useUIStore.getState().previewItem;
        if (!item) return;
        if (item.preview_url) previewCacheRef.current.delete(item.preview_url);
        setPreviewLoading(false);
        setPreviewError(`图表渲染出错：${e.data.message.slice(0, 200)}。可重试或重新生成该图表。`);
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
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
    try {
      const reqHeaders = cached?.etag
        ? requestHeaders({ "If-None-Match": cached.etag })
        : requestHeaders();
      const response = await fetch(`${API_URL}${item.preview_url}`, {
        headers: reqHeaders,
      });
      // 304：文件未变，复用缓存（已校验过的正确 HTML）。
      if (response.status === 304 && cached) {
        previewCacheRef.current.delete(cacheKey);
        previewCacheRef.current.set(cacheKey, cached); // LRU 触活
        setCompareHtml(prepareHtml(cached.html));
        return;
      }
      const html = await response.text();
      if (response.ok && isValidPreviewHtml(html)) {
        const etag = response.headers.get("etag") || "";
        previewCacheRef.current.set(cacheKey, { html, etag });
        if (previewCacheRef.current.size > PREVIEW_CACHE_MAX) {
          const oldest = previewCacheRef.current.keys().next().value;
          if (oldest !== undefined) previewCacheRef.current.delete(oldest);
        }
        setCompareHtml(prepareHtml(html));
      }
    } catch {
      // 对比加载失败时不阻塞主图，静默忽略
    } finally {
      setCompareLoading(false);
    }
  }, [isValidPreviewHtml, prepareHtml]);

  // 通过 postMessage 通道让 iframe 内图表导出 PNG（#23）。
  // iframe sandbox 只保留 allow-scripts（不含 allow-same-origin），
  // 因此不能用 contentDocument 直接读取，改由图表脚本回传 dataURL。
  // 5 秒超时兜底，避免图表脚本未注册监听时按钮永远 disabled。
  const downloadPng = useCallback(() => {
    const iframe = document.querySelector<HTMLIFrameElement>(".preview-frame iframe");
    if (!iframe?.contentWindow) return;
    setPngDownloading(true);
    const handler = (e: MessageEvent) => {
      // 校验消息来源：必须来自预览 iframe，且 data 必须是 PNG base64
      if (e.source !== iframe.contentWindow) return;
      if (e.data?.type === "png-data" && typeof e.data.data === "string"
          && e.data.data.startsWith("data:image/png;base64,")) {
        const a = document.createElement("a");
        a.href = e.data.data;
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
        body: JSON.stringify(buildChartEditPayload(chartEditTitle || undefined, chartEditColor, chartEditColorTouched)),
      });
      if (!response.ok) throw new Error("图表编辑失败");
      // 仅清除被编辑图表的缓存条目，保留其他图表的缓存（LRU 设计）。
      // 之前用 clear() 会清空全部缓存，导致其他已缓存图表需重新 fetch ~3.5MB。
      if (previewItem.preview_url) {
        previewCacheRef.current.delete(previewItem.preview_url);
      }
      setPreviewHtml("");
      setChartEditOpen(false);
      // 重新打开预览，加载更新后的图表
      openArtifactPreview(previewItem);
      // 刷新 session 以获取更新的 artifacts（描述等可能被后端覆盖）
      const updated = await api<Session>(`/api/sessions/${session.id}`);
      setSession(updated);
      // 同步更新 previewItem：模态头部标题与产物卡片标题立即反映编辑
      // 结果，不必等下次打开。旧 previewItem 是编辑前的对象，session
      // 刷新不会自动同步它。
      const fresh = updated.artifacts?.find((a) => a.name === previewItem.name);
      if (fresh) setPreviewItem({ ...previewItem, ...fresh });
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
      setChartEditColorTouched(false);
      setChartEditOpen(false);
    }
  }, [previewItem]);

  return {
    openArtifactPreview,
    closeArtifactPreview,
    loadCompareChart,
    downloadPng,
    editChart,
    onPreviewIframeLoaded,
    chartEditOpen, setChartEditOpen,
    chartEditTitle, setChartEditTitle,
    chartEditColor, setChartEditColor, setChartEditColorTouched,
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
