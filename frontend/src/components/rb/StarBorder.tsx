// React Bits — StarBorder
// Inspired by: https://reactbits.dev/animations/star-border
// License: MIT
// 纯 CSS 实现：上下两条 radial-gradient 光带沿容器边缘往复移动，
// 形成"流光描边"效果，用于强调主 CTA（如运行分析按钮）。
// 设计意图：光带只在 wrapper 的 1px 内边距缝隙中露出，不改变子元素
// 自身样式；disabled 时关闭动画避免"可点击"的错误暗示。

import "./StarBorder.css";

interface StarBorderProps {
  children: React.ReactNode;
  /** 光带颜色，默认跟随主题强调色 */
  color?: string;
  /** 一轮流动耗时（秒） */
  speed?: number;
  /** 关闭流光（如按钮 disabled 时），仅保留普通容器 */
  disabled?: boolean;
  className?: string;
}

const StarBorder = ({
  children,
  color = "var(--accent-fg)",
  speed = 5,
  disabled = false,
  className = "",
}: StarBorderProps) => {
  const vars: React.CSSProperties = {
    "--sb-color": color,
    "--sb-duration": `${speed}s`,
  } as React.CSSProperties;

  return (
    <span
      className={`star-border${disabled ? " star-border--off" : ""} ${className}`}
      style={vars}
    >
      {!disabled && (
        <>
          <span className="star-border-glow star-border-glow--top" aria-hidden="true" />
          <span className="star-border-glow star-border-glow--bottom" aria-hidden="true" />
        </>
      )}
      <span className="star-border-inner">{children}</span>
    </span>
  );
};

export default StarBorder;
