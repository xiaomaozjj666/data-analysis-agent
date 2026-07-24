import React from "react";

// React Bits 风格组件：Stagger Reveal —— 子元素依次淡入上移。
// 纯 CSS animation-delay 实现交错入场，零依赖。
// 用于报告段落、数据卡片等列表式内容的入场动画。

interface StaggerRevealProps {
  children: React.ReactNode;
  /** 每个子元素的延迟增量秒 */
  stagger?: number;
  /** 初始延迟秒 */
  initialDelay?: number;
  /** 动画时长秒 */
  duration?: number;
  className?: string;
  /** 是否启用（false 则直接渲染无动画） */
  enabled?: boolean;
}

const StaggerReveal = React.memo(function StaggerReveal({
  children,
  stagger = 0.06,
  initialDelay = 0,
  duration = 0.5,
  className = "",
  enabled = true,
}: StaggerRevealProps) {
  if (!enabled) return <>{children}</>;

  const items = React.Children.toArray(children);

  return (
    <>
      {items.map((child, i) => (
        <StaggerItem
          key={i}
          delay={initialDelay + i * stagger}
          duration={duration}
        >
          {child}
        </StaggerItem>
      ))}
    </>
  );
});

const StaggerItem = React.memo(function StaggerItem({
  children,
  delay,
  duration,
}: {
  children: React.ReactNode;
  delay: number;
  duration: number;
}) {
  return (
    <div
      className="rb-stagger-item"
      style={{
        animationDelay: `${delay}s`,
        animationDuration: `${duration}s`,
      }}
    >
      {children}
    </div>
  );
});

export default StaggerReveal;
