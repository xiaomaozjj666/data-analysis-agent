// React Bits — RotatingText
// Inspired by: https://reactbits.dev/text-animations/rotating-text
// License: MIT
// 词语轮播：每隔 interval 切换一个词，进出场用 CSS 动画（上移淡出/下移进入）。
// 设计意图：空状态首屏用场景词轮播传达"能分析什么"，降低首次使用的想象成本；
// reduced-motion 下固定显示第一个词（频繁文本跳变对动效敏感用户同样是干扰）。

import { useEffect, useRef, useState } from "react";
import "./RotatingText.css";

interface RotatingTextProps {
  words: string[];
  /** 每个词的停留时长（毫秒） */
  interval?: number;
  className?: string;
}

const RotatingText = ({ words, interval = 2600, className = "" }: RotatingTextProps) => {
  const [index, setIndex] = useState(0);
  // 挂载时读取一次动画偏好：变更需刷新生效，避免每帧查询的开销
  const reducedMotion = useRef(
    typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  ).current;

  useEffect(() => {
    if (reducedMotion || words.length <= 1) return;
    const timer = window.setInterval(() => {
      setIndex((prev) => (prev + 1) % words.length);
    }, interval);
    return () => window.clearInterval(timer);
  }, [interval, words.length, reducedMotion]);

  if (words.length === 0) return null;

  return (
    <span className={`rotating-text ${className}`} aria-live="off">
      {/* key 变化触发重挂载，CSS 入场动画随之重播 */}
      <span key={index} className="rotating-text-word">
        {words[index]}
      </span>
    </span>
  );
};

export default RotatingText;
