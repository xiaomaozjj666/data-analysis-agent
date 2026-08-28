import { useEffect, useRef, useState } from "react";

// 元素可见性追踪：进入视口（含 rootMargin 提前量，默认提前 240px 预渲染）
// → true；离开视口（含提前量）→ false。供产物网格的交互迷你图懒加载——
// 出屏的卡片不发请求、不初始化/常驻图表实例。
// IntersectionObserver 不可用（旧浏览器 / jsdom 测试环境）时直接视为
// 可见，功能降级为"立即渲染"，行为与懒加载引入前一致。
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
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) setInView(entry.isIntersecting);
      },
      { rootMargin },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [rootMargin]);

  return { ref, inView };
}

export default useInView;
