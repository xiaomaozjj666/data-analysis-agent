// 分析完成/失败通知：用户切到其他 tab 时通过 Notification API + title 闪烁提醒
let _titleFlashTimer = null;
let _originalTitle = "";
function notifyAnalysisDone(title, body) {
  if (document.visibilityState === "hidden") {
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification(title, { body, icon: "/favicon.ico" }); } catch (_) { /* noop */ }
    }
    _originalTitle = _originalTitle || document.title;
    let toggle = false;
    const flash = () => {
      document.title = toggle ? `✅ ${title}` : _originalTitle;
      toggle = !toggle;
    };
    flash();
    _titleFlashTimer = window.setInterval(flash, 1000);
    window.addEventListener("focus", () => {
      if (_titleFlashTimer) { window.clearInterval(_titleFlashTimer); _titleFlashTimer = null; }
      document.title = _originalTitle;
    }, { once: true });
  }
}

export { notifyAnalysisDone };
