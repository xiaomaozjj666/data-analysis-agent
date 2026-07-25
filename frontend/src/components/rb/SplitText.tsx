// React Bits — SplitText
// Inspired by: https://www.reactbits.dev/text-animations/split-text
// License: MIT
// Dependency: motion (motion/react)

import { useRef, memo } from "react";
import { motion, useInView } from "motion/react";
import "./SplitText.css";

interface SplitTextProps {
  text: string;
  className?: string;
  delay?: number;
  /** Duration per character in seconds */
  duration?: number;
  /** Stagger between characters in seconds */
  stagger?: number;
  /** Animation type */
  animationType?: "spring" | "tween";
  /** Split by "char" or "word" */
  splitBy?: "char" | "word";
  /** Start animation only when in view */
  triggerOnView?: boolean;
  /** Callback when animation completes */
  onComplete?: () => void;
}

const SplitText = memo(({
  text,
  className = "",
  delay = 0,
  duration = 0.5,
  stagger = 0.03,
  animationType = "spring",
  splitBy = "char",
  triggerOnView = true,
  onComplete,
}: SplitTextProps) => {
  const ref = useRef<HTMLSpanElement>(null);
  const isInView = useInView(ref, { once: true, amount: 0.3 });
  const shouldAnimate = triggerOnView ? isInView : true;

  const segments = splitBy === "word"
    ? text.split(" ").map((word, i, arr) => (i < arr.length - 1 ? word + " " : word))
    : text.split("");

  const containerVariants = {
    hidden: {},
    visible: {
      transition: {
        staggerChildren: stagger,
        delayChildren: delay,
      },
    },
  };

  const transition = animationType === "spring"
    ? { type: "spring" as const, damping: 20, stiffness: 200 }
    : { type: "tween" as const, duration, ease: [0.16, 1, 0.3, 1] as [number, number, number, number] };

  const charVariants = {
    hidden: {
      opacity: 0,
      y: 20,
      filter: "blur(8px)",
    },
    visible: {
      opacity: 1,
      y: 0,
      filter: "blur(0px)",
      transition,
    },
  };

  return (
    <motion.span
      ref={ref}
      className={`split-text ${className}`}
      variants={containerVariants}
      initial="hidden"
      animate={shouldAnimate ? "visible" : "hidden"}
      onAnimationComplete={() => {
        if (shouldAnimate && onComplete) onComplete();
      }}
      aria-label={text}
    >
      {segments.map((segment, index) => (
        <motion.span
          key={index}
          className="split-text-char"
          variants={charVariants}
          aria-hidden="true"
        >
          {segment === " " ? "\u00A0" : segment}
        </motion.span>
      ))}
    </motion.span>
  );
});

SplitText.displayName = "SplitText";
export default SplitText;
