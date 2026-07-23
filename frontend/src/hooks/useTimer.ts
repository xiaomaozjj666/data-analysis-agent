import { useEffect, type RefObject } from "react";

// 实时耗时 hook：running 时每 250ms 刷新 elapsed = now - startedAtRef。
// 提取自 App.tsx 的计时 useEffect，行为与原实现完全一致：
//   - 用 ref（startedAtRef）而非 session.analysis_started_at 作依赖——后者在
//     SSE 期间不会刷新到前端，会让 setInterval 永远不启动
//   - 250ms tick，仅在显示秒数变化时 setState，减少不必要的重渲染
//   - 后台 tab 暂停 interval（visibilitychange hidden），回到前台立即 tick
//     一次追上真实耗时
//   - running 转 false 时 effect cleanup 清 interval，自然停止
//   - tick 每次都重新读 startedAtRef.current，complete 帧后用服务端精确
//     started_at 校正 ref 后下一帧能立即生效
// 依赖数组保持原样仅 [running]：startedAtRef 为稳定 ref，setElapsedSeconds
// 为 Zustand 稳定 setter，均无需进入依赖（与原 App.tsx 行为一致）。
function useTimer(
  running: boolean,
  startedAtRef: RefObject<number | null>,
  setElapsedSeconds: (v: number | null) => void,
): void {
  useEffect(() => {
    if (!running) return undefined;
    if (!startedAtRef.current) return undefined;
    let lastDisplayedSecond = -1;
    let timer: number | null = null;
    const tick = () => {
      const started = startedAtRef.current;
      if (!started) return;
      const elapsed = Math.max(0, Date.now() / 1000 - started);
      const currentSecond = Math.floor(elapsed);
      // 只有秒数实际变化才 setState，250ms 的 tick 大多数时候是 no-op。
      if (currentSecond !== lastDisplayedSecond) {
        lastDisplayedSecond = currentSecond;
        setElapsedSeconds(elapsed);
      }
    };
    const onVisibilityChange = () => {
      if (document.hidden) {
        if (timer != null) {
          window.clearInterval(timer);
          timer = null;
        }
      } else if (timer == null) {
        tick();
        timer = window.setInterval(tick, 250);
      }
    };
    tick();
    timer = window.setInterval(tick, 250);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      if (timer != null) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
    // 依赖与原 App.tsx 保持一致：startedAtRef/setElapsedSeconds 稳定，省略。
  }, [running]);
}

export default useTimer;
