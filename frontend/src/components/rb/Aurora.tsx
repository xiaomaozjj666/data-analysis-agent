// React Bits — Aurora
// Inspired by: https://www.reactbits.dev/backgrounds/aurora
// License: MIT
// Pure CSS gradient blob animation — no canvas, no external deps

import { memo } from "react";
import "./Aurora.css";

interface AuroraProps {
  /** Primary color (CSS color value) */
  colorPrimary?: string;
  /** Secondary color */
  colorSecondary?: string;
  /** Tertiary color */
  colorTertiary?: string;
  /** Animation speed multiplier (1 = default 8s cycle) */
  speed?: number;
  /** Blur radius in px */
  blur?: number;
  /** Overall opacity (0-1) */
  opacity?: number;
  className?: string;
}

const Aurora = memo(({
  colorPrimary,
  colorSecondary,
  colorTertiary,
  speed = 1,
  blur = 60,
  opacity,
  className = "",
}: AuroraProps) => {
  // 只写"调用方显式给了"的变量：以前颜色与透明度都有默认值并总是内联写死，
  // 而内联样式优先级高于类选择器——主题相关的 CSS 覆盖（例如暗色下降低
  // alpha 免得整块画布被蓝紫糊满）会全部失效。
  const vars: Record<string, string> = {
    "--aurora-duration": `${8 / speed}s`,
    "--aurora-blur": `${blur}px`,
  };
  if (colorPrimary !== undefined) vars["--aurora-primary"] = colorPrimary;
  if (colorSecondary !== undefined) vars["--aurora-secondary"] = colorSecondary;
  if (colorTertiary !== undefined) vars["--aurora-tertiary"] = colorTertiary;
  if (opacity !== undefined) vars["--aurora-opacity"] = String(opacity);

  return (
    <div className={`aurora-container ${className}`} style={vars as React.CSSProperties} aria-hidden="true">
      <div className="aurora-blob aurora-blob-1" />
      <div className="aurora-blob aurora-blob-2" />
      <div className="aurora-blob aurora-blob-3" />
    </div>
  );
});

Aurora.displayName = "Aurora";
export default Aurora;
