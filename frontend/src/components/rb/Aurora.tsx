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
  colorPrimary = "rgba(91, 91, 214, 0.3)",
  colorSecondary = "rgba(139, 92, 246, 0.2)",
  colorTertiary = "rgba(59, 130, 246, 0.15)",
  speed = 1,
  blur = 60,
  opacity = 1,
  className = "",
}: AuroraProps) => {
  const vars = {
    "--aurora-primary": colorPrimary,
    "--aurora-secondary": colorSecondary,
    "--aurora-tertiary": colorTertiary,
    "--aurora-duration": `${8 / speed}s`,
    "--aurora-blur": `${blur}px`,
    "--aurora-opacity": String(opacity),
  } as React.CSSProperties;

  return (
    <div className={`aurora-container ${className}`} style={vars} aria-hidden="true">
      <div className="aurora-blob aurora-blob-1" />
      <div className="aurora-blob aurora-blob-2" />
      <div className="aurora-blob aurora-blob-3" />
    </div>
  );
});

Aurora.displayName = "Aurora";
export default Aurora;
