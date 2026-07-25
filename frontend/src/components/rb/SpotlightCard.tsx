// React Bits — SpotlightCard
// Inspired by: https://www.reactbits.dev/components/spotlight-card
// License: MIT
// Mouse-tracking spotlight + 3D tilt effect for premium card interactions

import { useCallback, useRef, memo } from "react";
import "./SpotlightCard.css";

interface SpotlightCardProps {
  children: React.ReactNode;
  /** Spotlight radius in px */
  spotlightRadius?: number;
  /** Spotlight color (CSS color with alpha) */
  spotlightColor?: string;
  /** Enable 3D tilt on hover */
  tilt?: boolean;
  /** Max tilt angle in degrees */
  tiltMax?: number;
  /** Perspective distance in px */
  perspective?: number;
  /** Scale on hover (1 = no scale) */
  hoverScale?: number;
  className?: string;
  style?: React.CSSProperties;
  onClick?: () => void;
}

const SpotlightCard = memo(({
  children,
  spotlightRadius = 250,
  spotlightColor = "rgba(91, 91, 214, 0.12)",
  tilt = true,
  tiltMax = 5,
  perspective = 800,
  hoverScale = 1.02,
  className = "",
  style = {},
  onClick,
}: SpotlightCardProps) => {
  const cardRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number>(0);

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const card = cardRef.current;
    if (!card) return;

    // Cancel previous frame to throttle updates
    if (rafRef.current) cancelAnimationFrame(rafRef.current);

    rafRef.current = requestAnimationFrame(() => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      // Spotlight position
      card.style.setProperty("--spotlight-x", `${x}px`);
      card.style.setProperty("--spotlight-y", `${y}px`);

      if (tilt) {
        // 3D tilt calculation
        const centerX = rect.width / 2;
        const centerY = rect.height / 2;
        const rotateX = ((y - centerY) / centerY) * -tiltMax;
        const rotateY = ((x - centerX) / centerX) * tiltMax;
        card.style.setProperty("--tilt-x", `${rotateX}deg`);
        card.style.setProperty("--tilt-y", `${rotateY}deg`);
      }
    });
  }, [tilt, tiltMax]);

  const handleMouseLeave = useCallback(() => {
    const card = cardRef.current;
    if (!card) return;
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    // Reset spotlight and tilt
    card.style.setProperty("--spotlight-x", "-9999px");
    card.style.setProperty("--spotlight-y", "-9999px");
    card.style.setProperty("--tilt-x", "0deg");
    card.style.setProperty("--tilt-y", "0deg");
  }, []);

  const vars = {
    "--spotlight-radius": `${spotlightRadius}px`,
    "--spotlight-color": spotlightColor,
    "--spotlight-perspective": `${perspective}px`,
    "--spotlight-hover-scale": String(hoverScale),
    ...style,
  } as React.CSSProperties;

  return (
    <div
      ref={cardRef}
      className={`spotlight-card ${tilt ? "has-tilt" : ""} ${className}`}
      style={vars}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      onClick={onClick}
    >
      <div className="spotlight-card-overlay" />
      <div className="spotlight-card-content">
        {children}
      </div>
    </div>
  );
});

SpotlightCard.displayName = "SpotlightCard";
export default SpotlightCard;
