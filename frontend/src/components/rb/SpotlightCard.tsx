import React, { useCallback, useRef } from "react";

// React Bits 风格组件：Spotlight Card —— 卡片表面有跟随鼠标的光斑。
// 纯 CSS radial-gradient + CSS 变量，零依赖。
// 用于图表卡片、产物列表项等需要交互反馈的卡片。

interface SpotlightCardProps {
  children: React.ReactNode;
  className?: string;
  /** 光斑半径 px */
  radius?: number;
  /** 光斑颜色 */
  color?: string;
  /** 光斑透明度 */
  intensity?: number;
  onClick?: () => void;
}

const SpotlightCard = React.memo(function SpotlightCard({
  children,
  className = "",
  radius = 200,
  color = "255, 255, 255",
  intensity = 0.08,
  onClick,
}: SpotlightCardProps) {
  const ref = useRef<HTMLDivElement>(null);

  const handleMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    el.style.setProperty("--spot-x", `${e.clientX - rect.left}px`);
    el.style.setProperty("--spot-y", `${e.clientY - rect.top}px`);
  }, []);

  const handleEnter = useCallback(() => {
    ref.current?.style.setProperty("--spot-opacity", String(intensity));
  }, [intensity]);

  const handleLeave = useCallback(() => {
    ref.current?.style.setProperty("--spot-opacity", "0");
  }, []);

  return (
    <div
      ref={ref}
      className={`rb-spotlight-card ${className}`}
      style={{
        "--spot-radius": `${radius}px`,
        "--spot-color": color,
      } as React.CSSProperties}
      onMouseMove={handleMove}
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
      onClick={onClick}
    >
      {children}
    </div>
  );
});

export default SpotlightCard;
