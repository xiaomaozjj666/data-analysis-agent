import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CalendarClock, ChevronDown, Play, RefreshCw, X } from "lucide-react";
import { useI18n } from "../i18n";
import type { AutomationHistoryRow, AutomationOverview, AutomationTask } from "../types";
import { api, ApiError } from "../utils/api";

interface SchedulePanelProps {
  onClose: () => void;
}

// 定时任务面板：后端 automation 能力的只读/触发入口。
// 任务定义仍由 automations.json / CLI 管理，这里只做列表、启停、立即运行、
// 试运行与历史查看——保持"数据找人"闭环的最小 UI。
export default function SchedulePanel({ onClose }: SchedulePanelProps) {
  const { locale, t } = useI18n();
  const [overview, setOverview] = useState<AutomationOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [historyTaskId, setHistoryTaskId] = useState("");
  const [historyRows, setHistoryRows] = useState<AutomationHistoryRow[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await api<AutomationOverview>("/api/automation/tasks");
      setOverview(payload);
    } catch (err) {
      setError(t("schedules.loadFailed", { message: (err as Error).message }));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const toggleTask = async (task: AutomationTask) => {
    if (busy) return;
    setBusy(`toggle:${task.id}`);
    setError("");
    setNotice("");
    try {
      const payload = await api<AutomationOverview>(
        `/api/automation/tasks/${encodeURIComponent(task.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !task.enabled }),
        },
      );
      setOverview(payload);
    } catch (err) {
      setError(t("schedules.actionFailed", { message: (err as Error).message }));
    } finally {
      setBusy("");
    }
  };

  const runTask = async (task: AutomationTask, dryRun: boolean) => {
    if (busy) return;
    setBusy(`${dryRun ? "dry" : "run"}:${task.id}`);
    setError("");
    setNotice("");
    try {
      const result = await api<{ alerts?: unknown[] }>(
        `/api/automation/tasks/${encodeURIComponent(task.id)}/run${dryRun ? "?dry_run=true" : ""}`,
        { method: "POST" },
      );
      const count = Array.isArray(result.alerts) ? result.alerts.length : 0;
      setNotice(t(dryRun ? "schedules.dryRunDone" : "schedules.runDone", { count }));
      if (!dryRun) await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("schedules.locked"));
      } else {
        setError(t("schedules.actionFailed", { message: (err as Error).message }));
      }
    } finally {
      setBusy("");
    }
  };

  const toggleHistory = async (task: AutomationTask) => {
    if (historyTaskId === task.id) {
      setHistoryTaskId("");
      return;
    }
    setHistoryTaskId(task.id);
    setHistoryRows([]);
    setHistoryError("");
    setHistoryLoading(true);
    try {
      const payload = await api<{ history: AutomationHistoryRow[] }>(
        `/api/automation/tasks/${encodeURIComponent(task.id)}/history`,
      );
      setHistoryRows((payload.history || []).slice().reverse());
    } catch (err) {
      setHistoryError(t("schedules.historyFailed", { message: (err as Error).message }));
    } finally {
      setHistoryLoading(false);
    }
  };

  const formatTime = (value: string | null | undefined): string => {
    if (!value) return t("schedules.never");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(locale);
  };

  const statusLabel = (task: AutomationTask): string => {
    if (task.last_status === "ok") return t("schedules.statusOk");
    if (task.last_status === "partial") return t("schedules.statusPartial");
    if (task.last_status === "error") return t("schedules.statusError");
    return t("schedules.never");
  };

  return createPortal(
    <div
      className="schedule-panel-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="schedule-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="schedule-panel-title"
      >
        <header className="schedule-panel-head">
          <div>
            <h2 id="schedule-panel-title">
              <CalendarClock size={17} aria-hidden="true" />
              {t("schedules.title")}
            </h2>
            <p>{t("schedules.subtitle")}</p>
          </div>
          <div className="schedule-panel-actions">
            <button
              type="button"
              className="icon-button"
              title={t("schedules.refresh")}
              aria-label={t("schedules.refresh")}
              onClick={() => void load()}
              disabled={loading}
            >
              <RefreshCw size={15} className={loading ? "spin" : ""} />
            </button>
            <button
              ref={closeRef}
              type="button"
              className="icon-button"
              title={t("schedules.close")}
              aria-label={t("schedules.close")}
              onClick={onClose}
            >
              <X size={15} />
            </button>
          </div>
        </header>

        {error && (
          <p className="schedule-error" role="alert">
            <AlertTriangle size={14} />
            {error}
          </p>
        )}
        {notice && <p className="schedule-notice" role="status">{notice}</p>}
        {loading && <p className="schedule-empty" role="status">{t("schedules.loading")}</p>}
        {!loading && overview && overview.tasks.length === 0 && (
          <p className="schedule-empty">{t("schedules.empty")}</p>
        )}

        <ul className="schedule-list">
          {(overview?.tasks || []).map((task) => (
            <li key={task.id} className={`schedule-item${task.enabled ? "" : " is-disabled"}`}>
              <div className="schedule-item-main">
                <div className="schedule-item-title">
                  <strong>{task.name}</strong>
                  <span className={`schedule-status is-${task.last_status || "never"}`}>
                    {statusLabel(task)}
                  </span>
                  {task.state_corrupt && (
                    <span className="schedule-corrupt">{t("schedules.corrupt")}</span>
                  )}
                </div>
                <div className="schedule-meta">
                  <span>
                    {task.schedule.interval_minutes
                      ? `${task.schedule.interval_minutes} min`
                      : task.schedule.daily_at}
                  </span>
                  <span>{t("schedules.nextDue")}: {formatTime(task.next_due_at)}</span>
                  <span>{t("schedules.lastRun")}: {formatTime(task.last_run_at)}</span>
                  {task.last_alerts > 0 && (
                    <span className="schedule-alerts">
                      {t("schedules.alerts", { count: task.last_alerts })}
                    </span>
                  )}
                </div>
                {task.last_alert_headline && (
                  <p className="schedule-headline">{task.last_alert_headline}</p>
                )}
                {task.last_error && <p className="schedule-item-error">{task.last_error}</p>}
              </div>
              <div className="schedule-item-actions">
                <button
                  type="button"
                  className="schedule-toggle"
                  aria-pressed={task.enabled}
                  disabled={!!busy}
                  onClick={() => void toggleTask(task)}
                >
                  {task.enabled ? t("schedules.enabled") : t("schedules.disabled")}
                </button>
                <button
                  type="button"
                  className="schedule-run"
                  disabled={!!busy}
                  aria-busy={busy === `run:${task.id}`}
                  onClick={() => void runTask(task, false)}
                >
                  <Play size={13} />
                  {t("schedules.run")}
                </button>
                <button
                  type="button"
                  className="schedule-dry"
                  disabled={!!busy}
                  aria-busy={busy === `dry:${task.id}`}
                  onClick={() => void runTask(task, true)}
                >
                  {t("schedules.dryRun")}
                </button>
                <button
                  type="button"
                  className="schedule-history-toggle"
                  aria-expanded={historyTaskId === task.id}
                  onClick={() => void toggleHistory(task)}
                >
                  {historyTaskId === task.id
                    ? t("schedules.hideHistory")
                    : t("schedules.history")}
                  <ChevronDown
                    size={13}
                    className={historyTaskId === task.id ? "is-open" : ""}
                  />
                </button>
              </div>
              {historyTaskId === task.id && (
                <div className="schedule-history">
                  {historyLoading && <p>{t("schedules.loading")}</p>}
                  {historyError && <p role="alert">{historyError}</p>}
                  {!historyLoading && !historyError && historyRows.length === 0 && (
                    <p>{t("schedules.historyEmpty")}</p>
                  )}
                  {historyRows.length > 0 && (
                    <ul>
                      {historyRows.map((row, index) => (
                        <li key={String(row.ran_at ?? index)}>
                          <span>{formatTime(String(row.ran_at ?? ""))}</span>
                          <span>{String(row.status ?? "")}</span>
                          <span>
                            {t("schedules.alerts", {
                              count: Array.isArray(row.alerts) ? row.alerts.length : 0,
                            })}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
