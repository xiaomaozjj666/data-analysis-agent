import React, { useMemo } from "react";

// React Bits 风格组件：Dot Field —— 网格点阵背景，呼吸式渐变遮罩。
// 纯 CSS 实现，零依赖。用于空状态背景，替代静态网格。

interface DotFieldProps {
  /** 点间距 px */
  gap?: number;
  /** 点大小 px */
  size?: number;
  /** 点颜色 */
  color?: string;
  /** 渐变色 1 */
  glowColor?: string;
  className?: string;
}

const DotField = React.memo(function DotField({
  gap = 24,
  size = 2,
  color = "rgba(128, 128, 145, 0.25)",
  glowColor = "rgba(91, 91, 214, 0.15)",
  className = "",
}: DotFieldProps) {
  const dots = useMemo(() => {
    const cols = 20;
    const rows = 12;
    return Array.from({ length: cols * rows }, (_, i) => ({
      x: (i % cols) * gap,
      y: Math.floor(i / cols) * gap,
      delay: (i % cols + i / cols) * 0.08,
    }));
  }, [gap]);

  return (
    <div
      className={`rb-dot-field ${className}`}
      aria-hidden="true"
      style={{
        "--dot-size": `${size}px`,
        "--dot-gap": `${gap}px`,
        "--dot-color": color,
        "--dot-glow": glowColor,
      } as React.CSSProperties}
    >
      {dots.map((d, i) => (
        <span
          key={i}
          className="rb-dot"
          style={{
            left: d.x,
            top: d.y,
            animationDelay: `${d.delay}s`,
          }}
        />
      ))}
    </div>
  );
});

export default DotField;
