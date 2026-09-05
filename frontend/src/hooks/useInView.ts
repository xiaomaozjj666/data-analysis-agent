import { useEffect, useRef, useState } from "react";

// 元素可见性追踪：进入视口（含 rootMargin 提前量，默认提前 240px 预渲染）
// → true；离开视口（含提前量）→ false。供产物网格的交互迷你图懒加载——
// 出屏的卡片不发请求、不初始化/常驻图表实例。
// 两层降级，宁可多渲染也绝不留白：
// 1. IntersectionObserver 不存在（旧浏览器 / jsdom 测试环境）→ 立即可见；
// 2. IO 存在但宿主不派发回调（实测：被遮挡/后台的 webview 里 Chromium
//    连初始回调都不给，缩略图会永久空白）→ observe 后 1.5s 仍无任何
//    回调则视为可见并停止观测，回到懒加载前的"立即渲染"行为。
export function useInView<T extends HTMLElement>(rootMargin = "240px") {
  const ref = useRef<T | null>(null);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    if (typeof IntersectionObserver === "undefined") {
      setInView(true);
      return undefined;
    }
    let delivered = false;
    let fallbackTimer = 0;
    const observer = new IntersectionObserver(
      (entries) => {
        delivered = true;
        for (const entry of entries) setInView(entry.isIntersecting);
      },
      { rootMargin },
    );
    observer.observe(el);
    fallbackTimer = window.setTimeout(() => {
      if (delivered) return;
      observer.disconnect();
      setInView(true);
    }, 1500);
    return () => {
      observer.disconnect();
      window.clearTimeout(fallbackTimer);
    };
  }, [rootMargin]);

  return { ref, inView };
}

export default useInView;
