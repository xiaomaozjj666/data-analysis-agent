// React Bits — GradientText
// Inspired by: https://reactbits.dev/text-animations/gradient-text
// License: MIT
// 纯 CSS 实现：background-clip: text + 渐变位移动画，零 JS 开销。
// 设计意图：品牌名/完成态标题的低调流光，速度默认较慢避免喧宾夺主；
// reduced-motion 下退化为静态渐变（仍保留配色，只停掉位移动画）。

import "./GradientText.css";

interface GradientTextProps {
  children: React.ReactNode;
  /** 渐变色数组，首尾相同色可保证循环无跳变 */
  colors?: string[];
  /** 一轮流动耗时（秒），越大越慢 */
  speed?: number;
  className?: string;
}

const GradientText = ({
  children,
  colors = ["var(--accent-fg)", "#8b5cf6", "#3b82f6", "var(--accent-fg)"],
  speed = 6,
  className = "",
}: GradientTextProps) => {
  const vars: React.CSSProperties = {
    "--gt-gradient": `linear-gradient(90deg, ${colors.join(", ")})`,
    "--gt-duration": `${speed}s`,
  } as React.CSSProperties;

  return (
    <span className={`gradient-text ${className}`} style={vars}>
      {children}
    </span>
  );
};

export default GradientText;
