"""定时分析任务：调度、指标异常检测与告警推送（最小闭环）。

来源：2026-11 竞品实地复核（docs/competitive-analysis.md 第六节）确认三家
一手能力——WorkBuddy 的周期任务、Trae 的 Automation、Tableau Pulse 的
"指标洞察推送给关注者"。本项目已有可执行指标与确定性配方，这里补上
"数据找人"的最短路径。

设计取舍：
- **不引入调度框架**：定时由外部调度器（cron / Windows 任务计划）调用
  ``run-due``，或前台 ``loop`` 模式；进程内不起守护线程。
- **异常检测只用确定性统计**：历史窗口上的 z-score 与环比变化，不调用 LLM。
  样本不足（默认 <3）不下结论；基线为 0 时退化为"从 0 起跳"的绝对增量提示。
- **推送失败不丢告警**：历史与告警先落盘，再尝试 webhook；发送失败把错误
  写回状态，而不是让整个任务失败。
- **口径可追溯**：每次运行按**加载时读到的同一份字节**计算指标定义哈希，
  历史里只与相同哈希的样本比较；口径一改，基线自动重置，避免新旧口径混比。
- **单任务失败隔离**：一个任务的数据坏了只影响它自己；失败会记录
  ``last_status=error`` 与重试时间，不会让轮询每 60 秒空转重试。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from data_agent._workspace_bundles import _atomic_write_text
from data_agent.metrics import compute_metrics_on_frame, parse_metric_definitions
from data_agent.workspace import DataWorkspace

_log = logging.getLogger(__name__)

AUTOMATION_DIRNAME = "automation"
SPECS_FILENAME = "automations.json"
HISTORY_FILENAME = "history.jsonl"
ALERTS_FILENAME = "alerts.jsonl"
STATE_FILENAME = "state.json"
LOCK_FILENAME = ".lock"
DEFAULT_HISTORY_LIMIT = 30
DEFAULT_MIN_SAMPLES = 3
WEBHOOK_TIMEOUT_SECONDS = 5.0
#: 追加式 JSONL 的轮转阈值：超过则把旧文件改名为 ``*.1``（只保留一代）。
#: history/alerts 是 append-only，长期 loop 运行会无限增长——history_limit
#: 只限制读取窗口，不限制文件体积（2026-11 实测告警文件无轮转）。
JSONL_ROTATE_BYTES = 2 * 1024 * 1024
#: 失败后的重试间隔：避免 loop 模式每秒重试坏任务，也避免整批被一个坏任务拖住。
RETRY_BACKOFF_MINUTES = 5

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_PRIVATE_FLAGS = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_reserved",
    "is_unspecified",
)


@dataclass(frozen=True)
class AutomationSpec:
    """一条定时分析任务。"""

    id: str
    name: str
    dataset_path: str
    metrics_path: str = ""
    interval_minutes: int = 0
    daily_at: str = ""
    metric_names: tuple[str, ...] = ()
    zscore_threshold: float = 3.0
    pct_change_threshold: float = 0.2
    history_limit: int = DEFAULT_HISTORY_LIMIT
    min_samples: int = DEFAULT_MIN_SAMPLES
    webhook_url: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metric_names"] = list(self.metric_names)
        return payload


# ---------------------------------------------------------------------------
# webhook 安全
# ---------------------------------------------------------------------------


def _unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(getattr(address, flag) for flag in _PRIVATE_FLAGS)


def _unsafe_host_reason(host: str, *, resolve: bool = True) -> str | None:
    """返回主机不安全的原因；安全或无法判断时返回 None。

    ``resolve=False`` 只检查 IP 字面量，用于解析/登记阶段（不做网络 I/O）；
    发送阶段用 ``resolve=True`` 再查一次 DNS，并逐跳校验重定向。注意：这里
    不做 IP 钉扎，解析与连接仍是两次解析——这是已知边界，文档中明确写出，
    不宣称能完全防御 DNS rebinding。
    """
    candidate = host.strip().strip("[]").lower()
    if not candidate:
        return "主机为空"
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is not None:
        return "目标是内网/环回/保留地址" if _unsafe_address(address) else None
    if not resolve:
        return None
    try:
        infos = socket.getaddrinfo(candidate, None)
    except OSError:
        return None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _unsafe_address(address):
            return "域名解析到内网/环回/保留地址"
    return None


def validate_webhook_url(url: str, *, resolve: bool = True) -> str | None:
    """校验 webhook 地址；返回错误文本，合法时返回 None。"""
    value = (url or "").strip()
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return "webhook 只支持 http/https"
    if parsed.username or parsed.password:
        return "webhook 地址不能包含账号密码"
    if not parsed.hostname:
        return "webhook 地址缺少主机名"
    return _unsafe_host_reason(parsed.hostname, resolve=resolve)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新校验目标，避免公网地址 302 到内网/元数据地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        unsafe = validate_webhook_url(newurl)
        if unsafe:
            raise urllib.error.HTTPError(newurl, code, f"redirect blocked: {unsafe}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _notify_result(
    webhook_url: str, payload: dict[str, Any]
) -> tuple[str | None, int | None, int]:
    """POST 告警到 webhook；返回 ``(错误文本, HTTP 状态码, 耗时 ms)``。

    成功时错误为 None。状态码/耗时用于事后判断「推没推出去、推了多久」——
    只有 ok/失败两个字段时，无法区分 4xx（地址/鉴权配错）与 5xx（对方故障）。
    """
    if not webhook_url:
        return None, None, 0
    unsafe = validate_webhook_url(webhook_url)
    if unsafe:
        return f"webhook 被拒绝：{unsafe}", None, 0
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with _post_request(request) as response:
            status = getattr(response, "status", None) or getattr(response, "code", None)
            try:
                code = int(status) if status is not None else None
            except (TypeError, ValueError):
                code = None
            return None, code, int((time.monotonic() - started) * 1000)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return str(exc)[:300], None, int((time.monotonic() - started) * 1000)


def _notify(webhook_url: str, payload: dict[str, Any]) -> str | None:
    """POST 告警到 webhook；成功返回 None，失败返回错误文本（不抛异常）。"""
    return _notify_result(webhook_url, payload)[0]


def _post_request(request: urllib.request.Request):
    """发送 webhook 请求（独立函数便于测试替换；重定向逐跳校验）。"""
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    return opener.open(request, timeout=WEBHOOK_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# 定义解析与存储
# ---------------------------------------------------------------------------


def parse_spec(payload: Any) -> AutomationSpec:
    """校验并解析一条任务定义；结构不对时报错而不是跳过。"""
    if not isinstance(payload, dict):
        raise ValueError("automation 任务必须是 JSON 对象。")
    task_id = str(payload.get("id") or "").strip()
    if not _ID_RE.match(task_id):
        raise ValueError("id 只能包含字母、数字、下划线或连字符（1–64 位）。")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError(f"任务 {task_id} 缺少 name。")
    dataset = str(payload.get("dataset_path") or "").strip()
    if not dataset:
        raise ValueError(f"任务 {task_id} 缺少 dataset_path。")
    interval_raw = payload.get("interval_minutes", 0)
    try:
        interval = int(interval_raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"任务 {task_id} 的 interval_minutes 必须是整数。") from exc
    daily_at = str(payload.get("daily_at") or "").strip()
    if bool(interval) == bool(daily_at):
        raise ValueError(f"任务 {task_id} 必须且只能设置 interval_minutes 或 daily_at 之一。")
    if interval and interval < 1:
        raise ValueError(f"任务 {task_id} 的 interval_minutes 必须 ≥ 1。")
    if daily_at and not _TIME_RE.match(daily_at):
        raise ValueError(f"任务 {task_id} 的 daily_at 必须是 HH:MM（24 小时制）。")
    try:
        zscore = float(payload.get("zscore_threshold", 3.0))
        pct = float(payload.get("pct_change_threshold", 0.2))
        history_limit = int(payload.get("history_limit", DEFAULT_HISTORY_LIMIT))
        min_samples = int(payload.get("min_samples", DEFAULT_MIN_SAMPLES))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"任务 {task_id} 的阈值/窗口参数必须是数字。") from exc
    if not math.isfinite(zscore) or zscore <= 0:
        raise ValueError(f"任务 {task_id} 的 zscore_threshold 必须为正数。")
    if not math.isfinite(pct) or pct <= 0:
        raise ValueError(f"任务 {task_id} 的 pct_change_threshold 必须为正数。")
    if history_limit < 1 or min_samples < 1:
        raise ValueError(f"任务 {task_id} 的 history_limit / min_samples 必须 ≥ 1。")
    if min_samples > history_limit:
        raise ValueError(f"任务 {task_id} 的 min_samples 不能大于 history_limit。")
    metric_names = payload.get("metric_names") or []
    if not isinstance(metric_names, (list, tuple)):
        raise ValueError(f"任务 {task_id} 的 metric_names 必须是字符串数组。")
    names = tuple(str(item).strip() for item in metric_names if str(item).strip())
    webhook_url = str(payload.get("webhook_url") or "").strip()
    # 登记阶段不做 DNS（纯解析不应触发网络 I/O，也不能因域名当前解析到内网
    # 就让整份任务列表加载失败）；只拦明确不安全的 scheme/账号/IP 字面量。
    webhook_error = validate_webhook_url(webhook_url, resolve=False)
    if webhook_error:
        raise ValueError(f"任务 {task_id} 的 webhook_url 不安全：{webhook_error}。")
    return AutomationSpec(
        id=task_id,
        name=name,
        dataset_path=dataset,
        metrics_path=str(payload.get("metrics_path") or "").strip(),
        interval_minutes=interval,
        daily_at=daily_at,
        metric_names=names,
        zscore_threshold=zscore,
        pct_change_threshold=pct,
        history_limit=history_limit,
        min_samples=min_samples,
        webhook_url=webhook_url,
        enabled=bool(payload.get("enabled", True)),
    )


def automation_root(runs_dir: str | Path) -> Path:
    return Path(runs_dir).expanduser() / AUTOMATION_DIRNAME


def specs_path(runs_dir: str | Path) -> Path:
    return automation_root(runs_dir) / SPECS_FILENAME


def task_dir(runs_dir: str | Path, task_id: str) -> Path:
    return automation_root(runs_dir) / task_id


def load_specs(runs_dir: str | Path) -> list[AutomationSpec]:
    path = specs_path(runs_dir)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("automations")
    if not isinstance(payload, list):
        raise ValueError(f"{path} 必须是数组或包含 automations 数组。")
    return [parse_spec(item) for item in payload]


def save_specs(runs_dir: str | Path, specs: list[AutomationSpec]) -> None:
    path = specs_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"automations": [spec.to_dict() for spec in specs]}, ensure_ascii=False, indent=2
    )
    _atomic_write_text(path, payload + "\n")


def add_spec(runs_dir: str | Path, spec: AutomationSpec) -> list[AutomationSpec]:
    specs = load_specs(runs_dir)
    merged: list[AutomationSpec] = []
    replaced = False
    for item in specs:
        if item.id == spec.id:
            merged.append(spec)
            replaced = True
        else:
            merged.append(item)
    if not replaced:
        merged.append(spec)
    save_specs(runs_dir, merged)
    return merged


def remove_spec(runs_dir: str | Path, task_id: str) -> bool:
    specs = load_specs(runs_dir)
    remaining = [item for item in specs if item.id != task_id]
    if len(remaining) == len(specs):
        return False
    save_specs(runs_dir, remaining)
    return True


def _read_state(runs_dir: str | Path, task_id: str) -> dict[str, Any]:
    path = task_dir(runs_dir, task_id) / STATE_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(runs_dir: str | Path, task_id: str, state: dict[str, Any]) -> None:
    path = task_dir(runs_dir, task_id) / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _state_corrupt(runs_dir: str | Path, task_id: str) -> bool:
    """state.json 是否存在但无法解析（读侧按「从未运行」降级，但要留痕）。"""
    path = task_dir(runs_dir, task_id) / STATE_FILENAME
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(payload, dict)


def _append_jsonl(path: Path, payload: str, *, rotate_bytes: int = JSONL_ROTATE_BYTES) -> None:
    """追加一行 JSONL；文件超过阈值时先轮转成 ``*.1``（只保留一代）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file() and path.stat().st_size > rotate_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError as exc:
        _log.warning("could not rotate %s: %s", path.name, exc)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload + "\n")


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_due(
    spec: AutomationSpec,
    last_run_at: str,
    now: datetime,
    *,
    next_retry_at: str = "",
) -> bool:
    """判断任务此刻是否到期；坏状态按"从未运行"处理，不阻断整批。"""
    if not spec.enabled:
        return False
    retry = _parse_time(next_retry_at) if next_retry_at else None
    if retry is not None and now < retry:
        return False
    last = _parse_time(last_run_at) if last_run_at else None
    if spec.interval_minutes:
        if last is None:
            return True
        return now - last >= timedelta(minutes=spec.interval_minutes)
    hour, minute = (int(part) for part in spec.daily_at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return False
    if last is None:
        return True
    return last < target


def read_history(runs_dir: str | Path, task_id: str, limit: int) -> list[dict[str, Any]]:
    path = task_dir(runs_dir, task_id) / HISTORY_FILENAME
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
    if skipped:
        # 坏行静默丢弃会让「历史上限」看起来莫名变小；留 warning 便于发现截断。
        _log.warning(
            "history for task %s contains %d unparsable line(s); they were ignored",
            task_id,
            skipped,
        )
    return rows[-limit:] if limit > 0 else rows


def _lock_handle(handle: Any) -> bool:  # pragma: no cover - 平台分支
    """尝试对打开的文件加非阻塞排他锁（Windows / POSIX 两套 API）。"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_handle(handle: Any) -> None:  # pragma: no cover - 平台分支
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _task_lock(runs_dir: str | Path, task_id: str):
    """任务级跨进程非阻塞锁；拿不到锁时 yield False。"""
    directory = task_dir(runs_dir, task_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_FILENAME
    handle = path.open("a+")
    acquired = _lock_handle(handle)
    try:
        yield acquired
    finally:
        if acquired:
            _unlock_handle(handle)
        handle.close()


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------


def detect_anomalies(
    current: dict[str, float],
    history: list[dict[str, Any]],
    spec: AutomationSpec,
) -> list[dict[str, Any]]:
    """在当前值与历史窗口上检测异常（z-score / 环比 / 零基线起跳）。"""
    alerts: list[dict[str, Any]] = []
    for metric, value in current.items():
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric_value):
            continue
        previous = [
            float(row["metrics"][metric])
            for row in history
            if isinstance(row.get("metrics"), dict)
            and isinstance(row["metrics"].get(metric), (int, float))
            and math.isfinite(float(row["metrics"][metric]))
        ]
        if len(previous) < spec.min_samples:
            continue
        mean = statistics.fmean(previous)
        stdev = statistics.pstdev(previous)
        zscore = (numeric_value - mean) / stdev if stdev > 0 else None
        last = previous[-1]
        pct_change = (numeric_value - last) / abs(last) if last != 0 else None
        reasons: list[str] = []
        if zscore is not None and abs(zscore) >= spec.zscore_threshold:
            reasons.append(f"z-score {zscore:+.2f} ≥ {spec.zscore_threshold:g}")
        if pct_change is not None and abs(pct_change) >= spec.pct_change_threshold:
            reasons.append(f"环比 {pct_change:+.1%} ≥ {spec.pct_change_threshold:.0%}")
        if last == 0 and abs(numeric_value) > 1e-9:
            # 零基线：方差为 0、环比无定义，但从 0 起跳本身就是需要关注的变化。
            reasons.append("基线为 0，当前值非零")
        if reasons:
            alerts.append(
                {
                    "metric": metric,
                    "value": numeric_value,
                    "baseline_mean": mean,
                    "baseline_stdev": stdev,
                    "zscore": zscore,
                    "pct_change": pct_change,
                    "reasons": reasons,
                }
            )
    return alerts


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


def _metrics_snapshot(metrics_path: Path) -> tuple[str, list[Any]]:
    """读取指标定义并返回 ``(sha256, definitions)``，两者来自同一份字节。"""
    raw = metrics_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    return digest, parse_metric_definitions(payload)


def _make_workspace(
    spec: AutomationSpec, runs_dir: str | Path, dry_run: bool
) -> tuple[DataWorkspace, Path | None]:
    if dry_run:
        base = Path(tempfile.mkdtemp(prefix="data-agent-automation-dry-"))
        return DataWorkspace(base, session_id=spec.id), base
    return (
        DataWorkspace(Path(runs_dir).expanduser() / "automation_workspaces", session_id=spec.id),
        None,
    )


def run_automation(
    spec: AutomationSpec,
    runs_dir: str | Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
) -> dict[str, Any]:
    """执行一次任务：加载数据 → 计算登记指标 → 检测异常 → 落历史/告警/推送。"""
    moment = now or datetime.now().astimezone()
    metrics_path = Path(spec.metrics_path).expanduser() if spec.metrics_path else None
    if metrics_path is None:
        candidate = Path(spec.dataset_path).expanduser().parent / "metrics.json"
        metrics_path = candidate if candidate.is_file() else None
    if metrics_path is None:
        raise ValueError(
            f"任务 {spec.id} 没有可用的指标定义（metrics_path 或数据集同目录 metrics.json）。"
        )
    metrics_sha256, definitions = _metrics_snapshot(metrics_path)
    if spec.metric_names:
        allowed = {name.casefold() for name in spec.metric_names}
        definitions = [item for item in definitions if item.name.casefold() in allowed]
        if not definitions:
            raise ValueError(f"任务 {spec.id} 指定的指标在定义文件中不存在。")

    if dry_run:
        workspace, temp_root = _make_workspace(spec, runs_dir, True)
        try:
            return _run_with_workspace(
                workspace,
                spec,
                runs_dir,
                moment,
                metrics_path,
                metrics_sha256,
                definitions,
                dry_run=True,
                notify=False,
            )
        finally:
            if temp_root is not None:
                shutil.rmtree(temp_root, ignore_errors=True)

    with _task_lock(runs_dir, spec.id) as acquired:
        if not acquired:
            return {"id": spec.id, "name": spec.name, "skipped": "locked"}
        workspace, _ = _make_workspace(spec, runs_dir, False)
        return _run_with_workspace(
            workspace,
            spec,
            runs_dir,
            moment,
            metrics_path,
            metrics_sha256,
            definitions,
            dry_run=False,
            notify=notify,
        )


def _run_with_workspace(
    workspace: DataWorkspace,
    spec: AutomationSpec,
    runs_dir: str | Path,
    moment: datetime,
    metrics_path: Path,
    metrics_sha256: str,
    definitions: list[Any],
    *,
    dry_run: bool,
    notify: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    workspace.load(spec.dataset_path)
    computed = compute_metrics_on_frame(workspace.dataframe, definitions)
    current = {
        item["name"]: float(item["sum"])
        for item in computed
        if item.get("status") == "ok"
        and item.get("sum") is not None
        and int(item.get("non_null") or 0) > 0
    }
    errors = {
        item["name"]: str(item.get("error") or "无有效数值")
        for item in computed
        if item.get("status") != "ok"
        or item.get("sum") is None
        or int(item.get("non_null") or 0) == 0
    }
    history = [
        row
        for row in read_history(runs_dir, spec.id, spec.history_limit)
        # 只与相同口径版本的历史比较；旧行没有 hash 时按同版本兼容。
        if row.get("metrics_sha256") in (None, metrics_sha256)
    ]
    alerts = detect_anomalies(current, history, spec)
    ran_at = moment.isoformat(timespec="seconds")
    state_corrupt = _state_corrupt(runs_dir, spec.id)
    if state_corrupt:
        _log.warning(
            "state.json for task %s is corrupt; treating it as never-run (last_run_at lost)",
            spec.id,
        )
    result: dict[str, Any] = {
        "id": spec.id,
        "name": spec.name,
        "ran_at": ran_at,
        "metrics": computed,
        "metric_errors": errors,
        "alerts": alerts,
        "notified": False,
        "webhook_error": None,
        "webhook_status": None,
        "webhook_duration_ms": None,
        "dry_run": dry_run,
        "history_samples": len(history),
        "metrics_file": str(metrics_path),
        "metrics_sha256": metrics_sha256,
        "state_corrupt": state_corrupt,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if dry_run:
        return result

    directory = task_dir(runs_dir, spec.id)
    directory.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        directory / HISTORY_FILENAME,
        json.dumps(
            {
                "at": ran_at,
                "metrics": current,
                "errors": errors,
                "metrics_sha256": metrics_sha256,
                "duration_ms": result["duration_ms"],
            },
            ensure_ascii=False,
        ),
    )
    if alerts:
        _append_jsonl(
            directory / ALERTS_FILENAME,
            json.dumps({"at": ran_at, "task_id": spec.id, "alerts": alerts}, ensure_ascii=False),
        )
    state = _read_state(runs_dir, spec.id)
    state.update(
        {
            "last_run_at": ran_at,
            "last_attempt_at": ran_at,
            "last_status": "partial" if errors else "ok",
            "last_alerts": len(alerts),
            "metrics_sha256": metrics_sha256,
        }
    )
    state.pop("next_retry_at", None)
    if errors:
        state["last_errors"] = errors
    else:
        state.pop("last_errors", None)
    _write_state(runs_dir, spec.id, state)

    webhook_error: str | None = None
    webhook_status: int | None = None
    webhook_duration_ms: int | None = None
    if alerts and notify and spec.webhook_url:
        webhook_error, webhook_status, webhook_duration_ms = _notify_result(
            spec.webhook_url,
            {
                "task": {"id": spec.id, "name": spec.name},
                "ran_at": ran_at,
                "alerts": alerts,
            },
        )
    result["notified"] = bool(alerts and notify and spec.webhook_url and not webhook_error)
    result["webhook_error"] = webhook_error
    result["webhook_status"] = webhook_status
    result["webhook_duration_ms"] = webhook_duration_ms
    if webhook_error:
        state["last_webhook_error"] = webhook_error
    else:
        state.pop("last_webhook_error", None)
    if webhook_duration_ms is not None:
        # 只有真的尝试过推送才落这两个字段：区分「没配 webhook」与「推失败」。
        state["last_webhook_status"] = webhook_status
        state["last_webhook_duration_ms"] = webhook_duration_ms
    state["last_notified"] = result["notified"]
    _write_state(runs_dir, spec.id, state)
    return result


def _record_failure(runs_dir: str | Path, task_id: str, error: str, now: datetime) -> None:
    state = _read_state(runs_dir, task_id)
    state.update(
        {
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "last_status": "error",
            "last_error": error[:500],
            "next_retry_at": (now + timedelta(minutes=RETRY_BACKOFF_MINUTES)).isoformat(
                timespec="seconds"
            ),
        }
    )
    _write_state(runs_dir, task_id, state)


def run_due(
    runs_dir: str | Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
) -> list[dict[str, Any]]:
    """运行所有到期任务；单任务失败只影响自己，错误写进结果与状态。"""
    moment = now or datetime.now().astimezone()
    results: list[dict[str, Any]] = []
    for spec in load_specs(runs_dir):
        try:
            state = _read_state(runs_dir, spec.id)
            if not is_due(
                spec,
                str(state.get("last_run_at") or ""),
                moment,
                next_retry_at=str(state.get("next_retry_at") or ""),
            ):
                continue
            results.append(run_automation(spec, runs_dir, dry_run=dry_run, notify=notify))
        except Exception as exc:  # noqa: BLE001 — 单任务失败不阻断批次
            message = str(exc)[:500]
            if not dry_run:
                _record_failure(runs_dir, spec.id, message, moment)
            results.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "ran_at": moment.isoformat(timespec="seconds"),
                    "error": message,
                }
            )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _serve_loop(args: argparse.Namespace, runs_dir: Path) -> None:  # pragma: no cover
    """前台轮询：按 --poll-seconds 周期跑到期任务，Ctrl+C 退出。"""
    while True:
        results = run_due(runs_dir, notify=not args.no_notify)
        if results:
            sys.stdout.write(json.dumps(results, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        time.sleep(max(1, args.poll_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data-agent-automation", description="定时分析任务")
    parser.add_argument(
        "--runs-dir",
        default=str(Path(os.getenv("DATA_AGENT_RUNS_DIR", "runs"))),
        help="运行根目录（默认为 DATA_AGENT_RUNS_DIR 或 ./runs；必须写在子命令之前）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="新增/更新一条任务（--spec 指向 JSON 文件）")
    add.add_argument("--spec", required=True, help="任务定义 JSON 文件路径")

    sub.add_parser("list", help="列出全部任务")

    remove = sub.add_parser("remove", help="删除任务")
    remove.add_argument("--id", required=True)

    run = sub.add_parser("run", help="立即运行指定任务")
    run.add_argument("--id", required=True)
    run.add_argument("--dry-run", action="store_true", help="只计算与检测，不写历史/告警、不推送")
    run.add_argument("--no-notify", action="store_true")

    due = sub.add_parser("run-due", help="运行所有到期任务（供 cron / 任务计划调用）")
    due.add_argument("--dry-run", action="store_true")
    due.add_argument("--no-notify", action="store_true", help="不发送 webhook")

    loop = sub.add_parser("loop", help="前台轮询模式（Ctrl+C 退出）")
    loop.add_argument("--poll-seconds", type=int, default=60)
    loop.add_argument("--no-notify", action="store_true")

    args = parser.parse_args(argv)
    runs_dir = Path(args.runs_dir)
    try:
        if args.command == "add":
            payload = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            if isinstance(payload, dict) and "automations" in payload:
                specs = [parse_spec(item) for item in payload["automations"]]
                for spec in specs:
                    add_spec(runs_dir, spec)
                output: Any = {"added": [spec.id for spec in specs]}
            else:
                spec = parse_spec(payload)
                add_spec(runs_dir, spec)
                output = {"added": [spec.id]}
        elif args.command == "list":
            output = [spec.to_dict() for spec in load_specs(runs_dir)]
        elif args.command == "remove":
            output = {"removed": remove_spec(runs_dir, args.id)}
        elif args.command == "run":
            specs = {spec.id: spec for spec in load_specs(runs_dir)}
            if args.id not in specs:
                raise ValueError(f"任务不存在：{args.id}")
            output = run_automation(
                specs[args.id],
                runs_dir,
                dry_run=args.dry_run,
                notify=not args.no_notify,
            )
        elif args.command == "run-due":
            output = run_due(runs_dir, dry_run=args.dry_run, notify=not args.no_notify)
        else:
            _serve_loop(args, runs_dir)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
