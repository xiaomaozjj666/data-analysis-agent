// Scroll progress tracking: updates --scroll-progress CSS variable on the topbar
// for the visual scroll indicator. Uses passive scroll listener for zero-cost perf.

import { useEffect } from "react";

export default function useScrollProgress() {
  useEffect(() => {
    function update() {
      const scrollTop = window.scrollY || document.documentElement.scrollTop;
      const docHeight = document.documentElement.scrollHeight - document.documentElement.clientHeight;
      const progress = docHeight > 0 ? Math.min((scrollTop / docHeight) * 100, 100) : 0;
      const topbar = document.querySelector(".topbar") as HTMLElement | null;
      if (topbar) {
        topbar.style.setProperty("--scroll-progress", `${progress}%`);
      }
    }

    window.addEventListener("scroll", update, { passive: true });
    update(); // initialize
    return () => window.removeEventListener("scroll", update);
  }, []);
}
