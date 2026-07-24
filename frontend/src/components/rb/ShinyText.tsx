import React from "react";

// React Bits 风格组件：Shiny Text —— 文字表面有光泽流动效果。
// 纯 CSS background-clip: text + 动画渐变，零依赖。
// 用于报告标题、空状态主标题等需要视觉强调的场景。

interface ShinyTextProps {
  text: string;
  /** 基础文字色 */
  color?: string;
  /** 光泽高亮色 */
  shineColor?: string;
  /** 动画周期秒数 */
  speed?: number;
  /** 循环间隔秒数 */
  delay?: number;
  /** 渐变角度（度） */
  spread?: number;
  /** true=来回往复，false=单向循环 */
  yoyo?: boolean;
  /** 悬停暂停 */
  pauseOnHover?: boolean;
  /** 光泽方向 */
  direction?: "left" | "right";
  className?: string;
}

const ShinyText = React.memo(function ShinyText({
  text,
  color = "currentColor",
  shineColor = "#ffffff",
  speed = 2.5,
  delay = 0,
  spread = 120,
  yoyo = false,
  pauseOnHover = false,
  direction = "left",
  className = "",
}: ShinyTextProps) {
  const style: React.CSSProperties = {
    "--shiny-color": color,
    "--shiny-shine": shineColor,
    "--shiny-speed": `${speed}s`,
    "--shiny-delay": `${delay}s`,
    "--shiny-spread": `${spread}deg`,
    animationDirection: yoyo ? "alternate" : "normal",
    animationName: direction === "left" ? "shiny-sweep-left" : "shiny-sweep-right",
  } as React.CSSProperties;

  return (
    <span
      className={`rb-shiny-text${pauseOnHover ? " pause-on-hover" : ""}${className ? ` ${className}` : ""}`}
      style={style}
    >
      {text}
    </span>
  );
});

export default ShinyText;
