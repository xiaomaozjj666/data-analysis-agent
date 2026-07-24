import React, { useEffect, useRef, useState } from "react";

// React Bits 风格组件：Counter —— 数字从 0 平滑增长到目标值。
// 用 requestAnimationFrame + easeOutExpo 缓动，零依赖。
// 用于数据概览中的行数、列数、缺失值等统计数字。

interface CounterProps {
  value: number;
  /** 动画时长毫秒 */
  duration?: number;
  /** 小数位数 */
  decimals?: number;
  /** 是否千分位分隔 */
  separator?: boolean;
  className?: string;
  /** 前缀文字 */
  prefix?: string;
  /** 后缀文字 */
  suffix?: string;
}

const easeOutExpo = (t: number) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t));

const formatNum = (n: number, decimals: number, separator: boolean) => {
  const fixed = Number(n.toFixed(decimals));
  if (!separator) return String(fixed);
  return fixed.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
};

const Counter = React.memo(function Counter({
  value,
  duration = 1200,
  decimals = 0,
  separator = true,
  className = "",
  prefix = "",
  suffix = "",
}: CounterProps) {
  const [display, setDisplay] = useState(0);
  const rafRef = useRef<number>(0);
  const startRef = useRef<number>(0);
  const fromRef = useRef(0);

  useEffect(() => {
    // 值未变化或为 0 时不动画
    if (value === display) return;
    fromRef.current = display;
    startRef.current = 0;
    cancelAnimationFrame(rafRef.current);

    const step = (ts: number) => {
      if (!startRef.current) startRef.current = ts;
      const progress = Math.min((ts - startRef.current) / duration, 1);
      const eased = easeOutExpo(progress);
      const current = fromRef.current + (value - fromRef.current) * eased;
      setDisplay(current);
      if (progress < 1) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        setDisplay(value);
      }
    };

    rafRef.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, duration]);

  return (
    <span className={`rb-counter ${className}`}>
      {prefix}{formatNum(display, decimals, separator)}{suffix}
    </span>
  );
});

export default Counter;
