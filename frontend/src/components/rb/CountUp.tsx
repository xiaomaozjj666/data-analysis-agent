// React Bits — CountUp
// Inspired by: https://www.reactbits.dev/text-animations/count-up
// License: MIT
// Animated number counter using requestAnimationFrame + easing

import { useEffect, useRef, useState, memo } from "react";
import { useInView } from "motion/react";

interface CountUpProps {
  /** Target number to count to */
  end: number;
  /** Starting number */
  start?: number;
  /** Duration in milliseconds */
  duration?: number;
  /** Number of decimal places */
  decimals?: number;
  /** Suffix to append (e.g., "行", "%") */
  suffix?: string;
  /** Prefix to prepend (e.g., "$") */
  prefix?: string;
  /** Use locale number formatting (e.g., 1,234) */
  useLocale?: boolean;
  /** Custom easing function */
  easing?: (t: number) => number;
  /** Only start when in viewport */
  triggerOnView?: boolean;
  className?: string;
}

// Default easeOutExpo for a satisfying deceleration
function easeOutExpo(t: number): number {
  return t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
}

const CountUp = memo(({
  end,
  start = 0,
  duration = 1500,
  decimals = 0,
  suffix = "",
  prefix = "",
  useLocale = true,
  easing = easeOutExpo,
  triggerOnView = true,
  className = "",
}: CountUpProps) => {
  const ref = useRef<HTMLSpanElement>(null);
  const isInView = useInView(ref, { once: true, amount: 0.3 });
  const [displayValue, setDisplayValue] = useState(start);
  // 记录已动画到的目标值：end 变化时（如产物数运行中增长）从当前
  // 显示值继续滚到新目标，而非停在首次动画的旧值。
  const animatedEndRef = useRef<number | null>(null);
  const displayRef = useRef(start);

  useEffect(() => {
    const shouldStart = triggerOnView ? isInView : true;
    if (!shouldStart || animatedEndRef.current === end) return;
    // 首次从 start 起步，后续 end 变化从当前显示值接续，避免数字回跳
    const from = animatedEndRef.current === null ? start : displayRef.current;
    animatedEndRef.current = end;

    let startTime: number | null = null;
    let raf: number;

    function tick(timestamp: number) {
      if (!startTime) startTime = timestamp;
      const elapsed = timestamp - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const easedProgress = easing(progress);
      const current = from + (end - from) * easedProgress;

      displayRef.current = current;
      setDisplayValue(current);

      if (progress < 1) {
        raf = requestAnimationFrame(tick);
      }
    }

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [isInView, triggerOnView, start, end, duration, easing]);

  const formatted = useLocale
    ? displayValue.toLocaleString("zh-CN", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })
    : displayValue.toFixed(decimals);

  return (
    <span ref={ref} className={`count-up ${className}`}>
      {prefix}{formatted}{suffix}
    </span>
  );
});

CountUp.displayName = "CountUp";
export default CountUp;
