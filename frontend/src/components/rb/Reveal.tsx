// React Bits — Reveal
// Inspired by: https://reactbits.dev/animations/animated-content (ScrollReveal 理念)
// License: MIT
// IntersectionObserver 驱动的滚动入场：元素进入视口时上浮淡入，只播一次。
// 设计意图：长页面（数据概览/结果中心）滚动时内容依次显现，建立浏览节奏；
// 用原生 IO 而非 motion 的 whileInView，避免给纯展示区引入不必要的 JS 动画帧；
// reduced-motion / 不支持 IO 的环境直接显示，不做任何位移。

import { useEffect, useRef, useState } from "react";
import "./Reveal.css";

interface RevealProps {
  children: React.ReactNode;
  /** 入场延迟（毫秒），用于同屏多个元素的 stagger */
  delay?: number;
  /** 触发阈值：元素可见比例达到该值才入场 */
  amount?: number;
  className?: string;
}

const Reveal = ({ children, delay = 0, amount = 0.15, className = "" }: RevealProps) => {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // 降级：无 IO 支持或用户偏好减少动画时直接显示
    if (
      typeof IntersectionObserver === "undefined" ||
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    ) {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect(); // 只播一次，之后不再监听
        }
      },
      { threshold: amount }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [amount]);

  return (
    <div
      ref={ref}
      className={`rb-reveal${visible ? " is-visible" : ""} ${className}`}
      style={delay ? ({ "--reveal-delay": `${delay}ms` } as React.CSSProperties) : undefined}
    >
      {children}
    </div>
  );
};

export default Reveal;
