// React Bits — ClickSpark
// Inspired by: https://www.reactbits.dev/animations/click-spark
// License: MIT
// Burst of spark lines on click using SVG + CSS animation

import { useCallback, useRef, memo } from "react";
import "./ClickSpark.css";

interface ClickSparkProps {
  children: React.ReactNode;
  /** Number of spark lines */
  sparkCount?: number;
  /** Color of the sparks */
  sparkColor?: string;
  /** Spark line length in px */
  sparkLength?: number;
  /** Animation duration in ms */
  duration?: number;
  className?: string;
}

interface Spark {
  id: number;
  x: number;
  y: number;
}

const ClickSpark = memo(({
  children,
  sparkCount = 8,
  sparkColor = "var(--accent-fg)",
  sparkLength = 12,
  duration = 400,
  className = "",
}: ClickSparkProps) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const sparksRef = useRef<HTMLDivElement>(null);
  const idRef = useRef(0);

  const handleClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const container = containerRef.current;
    const sparksEl = sparksRef.current;
    if (!container || !sparksEl) return;

    const rect = container.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    // Create spark SVG
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "click-spark-svg");
    svg.setAttribute("width", String(sparkLength * 4));
    svg.setAttribute("height", String(sparkLength * 4));
    svg.style.left = `${x - sparkLength * 2}px`;
    svg.style.top = `${y - sparkLength * 2}px`;
    svg.style.setProperty("--spark-duration", `${duration}ms`);

    const cx = sparkLength * 2;
    const cy = sparkLength * 2;

    for (let i = 0; i < sparkCount; i++) {
      const angle = (360 / sparkCount) * i;
      const rad = (angle * Math.PI) / 180;
      const x1 = cx + Math.cos(rad) * (sparkLength * 0.4);
      const y1 = cy + Math.sin(rad) * (sparkLength * 0.4);
      const x2 = cx + Math.cos(rad) * sparkLength;
      const y2 = cy + Math.sin(rad) * sparkLength;

      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", String(x1));
      line.setAttribute("y1", String(y1));
      line.setAttribute("x2", String(x2));
      line.setAttribute("y2", String(y2));
      line.setAttribute("stroke", sparkColor.startsWith("var(") ? "currentColor" : sparkColor);
      line.setAttribute("stroke-width", "2");
      line.setAttribute("stroke-linecap", "round");
      svg.appendChild(line);
    }

    sparksEl.appendChild(svg);

    // Clean up after animation
    setTimeout(() => {
      svg.remove();
    }, duration + 50);
  }, [sparkCount, sparkColor, sparkLength, duration]);

  return (
    <div
      ref={containerRef}
      className={`click-spark ${className}`}
      onClick={handleClick}
      style={{ color: sparkColor.startsWith("var(") ? undefined : sparkColor }}
    >
      <div ref={sparksRef} className="click-spark-container" />
      {children}
    </div>
  );
});

ClickSpark.displayName = "ClickSpark";
export default ClickSpark;
