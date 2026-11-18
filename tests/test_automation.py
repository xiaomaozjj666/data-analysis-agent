"""定时分析任务：定义校验、调度到期、异常检测、运行与推送。"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from data_agent import automation


def _base_spec(**overrides) -> dict:
    payload = {
        "id": "daily-sales",
        "name": "每日销售",
        "dataset_path": "unused.csv",
        "metrics_path": "unused-metrics.json",
        "interval_minutes": 60,
    }
    payload.update(overrides)
    return payload


def _write_dataset(path: Path, value: int) -> None:
    path.write_text(f"region,sales\nEast,{value}\n", encoding="utf-8")


def _write_metrics(path: Path) -> None:
    path.write_text(
        json.dumps(
            {"metrics": [{"name": "销售额", "description": "销售额合计", "expression": "sales"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_spec(tmp_path: Path, **overrides) -> automation.AutomationSpec:
    dataset = tmp_path / "sales.csv"
    metrics = tmp_path / "metrics.json"
    if not dataset.exists():
        _write_dataset(dataset, 100)
    if not metrics.exists():
        _write_metrics(metrics)
    payload = _base_spec(dataset_path=str(dataset), metrics_path=str(metrics), **overrides)
    return automation.parse_spec(payload)


# ---------------------------------------------------------------------------
# 定义校验
# ---------------------------------------------------------------------------


def test_parse_rejects_bad_id():
    with pytest.raises(ValueError, match="id 只能包含"):
        automation.parse_spec(_base_spec(id="bad id!"))


def test_parse_requires_exactly_one_schedule():
    with pytest.raises(ValueError, match="必须且只能设置"):
        automation.parse_spec(_base_spec(interval_minutes=0))
    with pytest.raises(ValueError, match="必须且只能设置"):
        automation.parse_spec(_base_spec(daily_at="09:00"))


def test_parse_validates_daily_time_and_thresholds():
    with pytest.raises(ValueError, match="HH:MM"):
        automation.parse_spec(_base_spec(interval_minutes=0, daily_at="9:0"))
    with pytest.raises(ValueError, match="zscore_threshold"):
        automation.parse_spec(_base_spec(zscore_threshold=0))
    with pytest.raises(ValueError, match="pct_change_threshold"):
        automation.parse_spec(_base_spec(pct_change_threshold=float("nan")))
    with pytest.raises(ValueError, match="min_samples"):
        automation.parse_spec(_base_spec(min_samples=5, history_limit=3))


def test_parse_normalizes_metric_names():
    spec = automation.parse_spec(_base_spec(metric_names=[" 销售额 ", ""]))
    assert spec.metric_names == ("销售额",)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def test_store_add_list_remove(tmp_path):
    spec = _make_spec(tmp_path)
    automation.add_spec(tmp_path / "runs", spec)
    assert [item.id for item in automation.load_specs(tmp_path / "runs")] == ["daily-sales"]

    updated = _make_spec(tmp_path, name="改名后的任务")
    automation.add_spec(tmp_path / "runs", updated)
    specs = automation.load_specs(tmp_path / "runs")
    assert len(specs) == 1 and specs[0].name == "改名后的任务"

    assert automation.remove_spec(tmp_path / "runs", "daily-sales") is True
    assert automation.load_specs(tmp_path / "runs") == []
    assert automation.remove_spec(tmp_path / "runs", "absent") is False


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def test_is_due_interval(tmp_path):
    spec = _make_spec(tmp_path)
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert automation.is_due(spec, "", now) is True
    assert automation.is_due(spec, (now - timedelta(minutes=30)).isoformat(), now) is False
    assert automation.is_due(spec, (now - timedelta(minutes=61)).isoformat(), now) is True


def test_is_due_daily_at(tmp_path):
    spec = _make_spec(tmp_path, interval_minutes=0, daily_at="09:00")
    before = datetime(2026, 11, 17, 8, 59, tzinfo=UTC)
    after = datetime(2026, 11, 17, 9, 1, tzinfo=UTC)
    assert automation.is_due(spec, "", before) is False
    assert automation.is_due(spec, "", after) is True
    # 今天 09:00 之后已跑过，不再触发
    assert automation.is_due(spec, after.isoformat(), after + timedelta(hours=3)) is False
    # 昨天跑过，今天到点后再次触发
    assert automation.is_due(spec, (after - timedelta(days=1)).isoformat(), after) is True


def test_disabled_spec_is_never_due(tmp_path):
    spec = _make_spec(tmp_path, enabled=False)
    assert automation.is_due(spec, "", datetime(2026, 11, 17, tzinfo=UTC)) is False


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------


def _history(values: list[float]) -> list[dict]:
    return [{"metrics": {"销售额": value}} for value in values]


def test_detect_skips_when_samples_insufficient(tmp_path):
    spec = _make_spec(tmp_path)
    assert automation.detect_anomalies({"销售额": 1000.0}, _history([100.0, 100.0]), spec) == []


def test_detect_flags_pct_change(tmp_path):
    spec = _make_spec(tmp_path)
    alerts = automation.detect_anomalies({"销售额": 1000.0}, _history([100.0, 100.0, 100.0]), spec)
    assert len(alerts) == 1
    assert alerts[0]["metric"] == "销售额"
    assert alerts[0]["pct_change"] == pytest.approx(9.0)
    assert alerts[0]["zscore"] is None  # 基线方差为 0 时不做 z 判断


def test_detect_flags_zscore(tmp_path):
    spec = _make_spec(tmp_path)
    alerts = automation.detect_anomalies({"销售额": 1000.0}, _history([100.0, 110.0, 90.0]), spec)
    assert len(alerts) == 1
    assert alerts[0]["zscore"] is not None and alerts[0]["zscore"] > 3


def test_detect_ignores_stable_series(tmp_path):
    spec = _make_spec(tmp_path)
    assert (
        automation.detect_anomalies({"销售额": 105.0}, _history([100.0, 110.0, 90.0]), spec) == []
    )


# ---------------------------------------------------------------------------
# 运行与推送
# ---------------------------------------------------------------------------


def test_first_run_writes_history_without_alert(tmp_path):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    result = automation.run_automation(spec, runs, now=datetime(2026, 11, 17, tzinfo=UTC))
    assert result["alerts"] == []
    history = automation.read_history(runs, spec.id, 10)
    assert len(history) == 1
    assert history[0]["metrics"]["销售额"] == 100.0
    assert len(history[0]["metrics_sha256"]) == 64
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert state["last_run_at"]
    assert state["last_alerts"] == 0


def test_spike_produces_alert_and_webhook(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path, webhook_url="https://hooks.example.test/alert")
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)

    calls: list[object] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_post(request):
        calls.append(request)
        return _Resp()

    monkeypatch.setattr(automation, "_post_request", fake_post)
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))
    assert len(result["alerts"]) == 1
    assert result["notified"] is True
    assert result["webhook_error"] is None
    assert len(calls) == 1
    body = json.loads(calls[0].data.decode("utf-8"))
    assert body["task"]["id"] == spec.id
    assert body["alerts"][0]["metric"] == "销售额"
    alerts_path = automation.task_dir(runs, spec.id) / automation.ALERTS_FILENAME
    assert alerts_path.is_file()
    assert "销售额" in alerts_path.read_text(encoding="utf-8")


def test_webhook_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path, webhook_url="https://hooks.example.test/alert")
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)

    def failing_post(request):
        raise OSError("connection refused")

    monkeypatch.setattr(automation, "_post_request", failing_post)
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))
    assert result["alerts"]
    assert result["notified"] is False
    assert "connection refused" in result["webhook_error"]
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert "last_webhook_error" in state


def test_dry_run_does_not_write(tmp_path):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    result = automation.run_automation(spec, runs, dry_run=True)
    assert result["dry_run"] is True
    assert automation.read_history(runs, spec.id, 10) == []
    assert not automation.task_dir(runs, spec.id).exists()


def test_run_due_only_runs_due_specs_and_isolates_errors(tmp_path):
    good = _make_spec(tmp_path)
    bad = automation.parse_spec(
        _base_spec(
            id="missing-data",
            name="数据缺失",
            dataset_path=str(tmp_path / "absent.csv"),
            metrics_path=str(tmp_path / "metrics.json"),
        )
    )
    not_due = automation.parse_spec(
        _base_spec(
            id="later", name="稍后", dataset_path=good.dataset_path, metrics_path=good.metrics_path
        )
    )
    runs = tmp_path / "runs"
    automation.save_specs(runs, [bad, good, not_due])
    now = datetime(2026, 11, 17, tzinfo=UTC)
    automation._write_state(runs, "later", {"last_run_at": now.isoformat()})

    results = automation.run_due(runs, now=now, notify=False)
    by_id = {item["id"]: item for item in results}
    assert set(by_id) == {"missing-data", "daily-sales"}
    assert "error" in by_id["missing-data"]
    assert by_id["daily-sales"]["alerts"] == []


def test_missing_metrics_definition_raises(tmp_path):
    spec = automation.parse_spec(
        _base_spec(
            dataset_path=str(tmp_path / "sales.csv"),
            metrics_path="",
        )
    )
    with pytest.raises(ValueError, match="没有可用的指标定义"):
        automation.run_automation(spec, tmp_path / "runs")


# ---------------------------------------------------------------------------
# webhook 安全（SSRF）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/hook",
        "ftp://example.com/hook",
        "http://user:pass@example.com/hook",
    ],
)
def test_parse_rejects_unsafe_webhook(url):
    with pytest.raises(ValueError, match="webhook_url"):
        automation.parse_spec(_base_spec(webhook_url=url))


def test_notify_refuses_hostname_resolving_to_private(monkeypatch):
    calls: list[object] = []

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("192.168.1.10", 0))]

    def fake_post(request):
        calls.append(request)
        raise AssertionError("不应该发起请求")

    monkeypatch.setattr(automation.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(automation, "_post_request", fake_post)
    error = automation._notify("https://internal.example.com/hook", {"alerts": []})
    assert error is not None and "webhook 被拒绝" in error
    assert calls == []


def test_notify_rejects_private_ip_literal():
    error = automation._notify("http://10.1.2.3/hook", {"alerts": []})
    assert error is not None and "webhook 被拒绝" in error


# ---------------------------------------------------------------------------
# 评审修复回归：dry-run 副作用、坏状态、异常口径、并发与 webhook 重定向
# ---------------------------------------------------------------------------


def _prepare_baseline(tmp_path, spec, runs, values=(100, 110, 90)):
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for offset, value in enumerate(values):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base + timedelta(hours=offset))
    return dataset, base + timedelta(days=1)


def test_dry_run_with_webhook_has_no_side_effects(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path, webhook_url="https://hooks.example.test/alert")
    runs = tmp_path / "runs"
    dataset, moment = _prepare_baseline(tmp_path, spec, runs)
    _write_dataset(dataset, 1000)

    calls: list[object] = []

    def fake_post(request):
        calls.append(request)
        raise AssertionError("dry-run 不应发送 webhook")

    monkeypatch.setattr(automation, "_post_request", fake_post)
    before_history = automation.read_history(runs, spec.id, 10)
    result = automation.run_automation(spec, runs, now=moment, dry_run=True)
    assert result["alerts"]
    assert result["notified"] is False
    assert calls == []
    assert automation.read_history(runs, spec.id, 10) == before_history
    assert not (automation.task_dir(runs, spec.id) / automation.ALERTS_FILENAME).exists()


def test_bad_last_run_at_does_not_abort_batch(tmp_path):
    spec = _make_spec(tmp_path)
    other = automation.parse_spec(
        _base_spec(
            id="second",
            name="第二个",
            dataset_path=spec.dataset_path,
            metrics_path=spec.metrics_path,
        )
    )
    runs = tmp_path / "runs"
    automation.save_specs(runs, [spec, other])
    automation._write_state(runs, spec.id, {"last_run_at": "not-a-date"})
    now = datetime(2026, 11, 17, tzinfo=UTC)
    results = automation.run_due(runs, now=now, notify=False)
    assert {item["id"] for item in results} == {spec.id, other.id}
    assert all("error" not in item for item in results)


def test_failure_records_backoff_and_skips_immediate_retry(tmp_path):
    spec = automation.parse_spec(
        _base_spec(
            id="broken",
            dataset_path=str(tmp_path / "missing.csv"),
            metrics_path=str(tmp_path / "metrics.json"),
        )
    )
    runs = tmp_path / "runs"
    automation.save_specs(runs, [spec])
    now = datetime(2026, 11, 17, tzinfo=UTC)
    first = automation.run_due(runs, now=now, notify=False)
    assert first and "error" in first[0]
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert state["last_status"] == "error"
    assert state["next_retry_at"]
    second = automation.run_due(runs, now=now + timedelta(minutes=1), notify=False)
    assert second == []


def test_zero_baseline_triggers_alert(tmp_path):
    spec = _make_spec(tmp_path)
    alerts = automation.detect_anomalies(
        {"销售额": 100.0},
        [{"metrics": {"销售额": 0.0}}, {"metrics": {"销售额": 0.0}}, {"metrics": {"销售额": 0.0}}],
        spec,
    )
    assert len(alerts) == 1
    assert any("基线为 0" in reason for reason in alerts[0]["reasons"])


def test_all_null_metric_is_partial_not_zero(tmp_path):
    spec = _make_spec(tmp_path)
    Path(spec.dataset_path).write_text("region,sales\nEast,\nWest,\n", encoding="utf-8")
    result = automation.run_automation(spec, tmp_path / "runs")
    assert result["alerts"] == []
    assert "销售额" in result["metric_errors"]
    state = json.loads(
        (automation.task_dir(tmp_path / "runs", spec.id) / automation.STATE_FILENAME).read_text()
    )
    assert state["last_status"] == "partial"


def test_metrics_change_resets_baseline(tmp_path):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    dataset, moment = _prepare_baseline(tmp_path, spec, runs)
    metrics = Path(spec.metrics_path)
    metrics.write_text(
        json.dumps(
            {"metrics": [{"name": "销售额", "description": "翻倍", "expression": "sales * 2"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = automation.run_automation(spec, runs, now=moment)
    assert result["history_samples"] == 0
    assert result["alerts"] == []
    assert (
        result["metrics_sha256"] != automation.read_history(runs, spec.id, 10)[0]["metrics_sha256"]
    )


def test_task_lock_prevents_concurrent_run(tmp_path):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    with automation._task_lock(runs, spec.id) as acquired:
        assert acquired is True
        result = automation.run_automation(spec, runs)
    assert result.get("skipped") == "locked"


def test_redirect_to_private_host_is_blocked(monkeypatch):
    """DNS rebinding 窗口已关闭：钉扎用的解析结果本身就是校验对象。"""

    import urllib.error

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "public.test":
            ip = "93.184.216.34"
        else:
            ip = "169.254.169.254"
        return [(automation.socket.AF_INET, 0, 0, "", (ip, port))]

    monkeypatch.setattr(automation.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        automation._validated_sockaddrs("http://private.test/x")
    assert "redirect blocked" in str(excinfo.value) and "内网" in str(excinfo.value)

    class _Resp:
        def __init__(self, status, headers=None):
            self.status = status
            self.headers = headers or {}

        def read(self):
            return b""

    class _Conn:
        def __init__(self, host, port, timeout=None):
            pass

        def request(self, method, path, body=None, headers=None):
            pass

        def getresponse(self):
            # 公网入口 302 到内网元数据地址：钉扎路径在第二跳直接拒绝。
            return _Resp(302, {"Location": "http://private.test/steal"})

        def close(self):
            pass

    monkeypatch.setattr(automation.http_client, "HTTPConnection", _Conn)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        automation._pinned_send("POST", "http://public.test/hook", b"{}", {})
    assert "redirect blocked" in str(excinfo.value) and "内网" in str(excinfo.value)


def test_parse_spec_does_not_resolve_dns(monkeypatch):
    def boom(host, port):
        raise AssertionError("parse 阶段不应做 DNS 查询")

    monkeypatch.setattr(automation.socket, "getaddrinfo", boom)
    spec = automation.parse_spec(_base_spec(webhook_url="https://hooks.example.test/alert"))
    assert spec.webhook_url == "https://hooks.example.test/alert"


# ---------------------------------------------------------------------------
# 可观测性：坏状态留痕、坏历史行计数、JSONL 轮转、webhook 状态与耗时
# ---------------------------------------------------------------------------


def test_history_row_records_duration(tmp_path):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    result = automation.run_automation(spec, runs, now=datetime(2026, 11, 17, tzinfo=UTC))
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert result["state_corrupt"] is False
    row = automation.read_history(runs, spec.id, 10)[0]
    assert "duration_ms" in row and isinstance(row["duration_ms"], int)


def test_corrupt_state_is_flagged_and_logged(tmp_path, caplog):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    directory = automation.task_dir(runs, spec.id)
    directory.mkdir(parents=True)
    (directory / automation.STATE_FILENAME).write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="data_agent.automation"):
        result = automation.run_automation(spec, runs, now=datetime(2026, 11, 17, tzinfo=UTC))

    assert result["state_corrupt"] is True
    assert any("corrupt" in record.message for record in caplog.records)


def test_read_history_warns_about_corrupt_lines(tmp_path, caplog):
    spec = _make_spec(tmp_path)
    path = automation.task_dir(tmp_path / "runs", spec.id) / automation.HISTORY_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text('{"at": "x", "metrics": {}}\nnot-json\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="data_agent.automation"):
        rows = automation.read_history(tmp_path / "runs", spec.id, 10)

    assert len(rows) == 1
    assert any("unparsable" in record.message for record in caplog.records)


def test_jsonl_rotation_caps_growth(tmp_path):
    path = tmp_path / "alerts.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    automation._append_jsonl(path, '{"a": 1}', rotate_bytes=50)
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert path.with_name("alerts.jsonl.1").read_text(encoding="utf-8") == "x" * 100


def test_webhook_status_and_duration_recorded(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path, webhook_url="https://hooks.example.test/alert")
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)

    class _Resp:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(automation, "_post_request", lambda request: _Resp())
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))

    assert result["notified"] is True
    assert result["webhook_status"] == 202
    assert isinstance(result["webhook_duration_ms"], int)
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert state["last_webhook_status"] == 202
    assert isinstance(state["last_webhook_duration_ms"], int)


def test_webhook_http_error_status_is_recorded(tmp_path, monkeypatch):
    """4xx/5xx 走 HTTPError：状态码必须落盘（区分地址配错与对方故障）。"""
    import urllib.error

    spec = _make_spec(tmp_path, webhook_url="https://hooks.example.test/alert")
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)

    def failing_post(request):
        raise urllib.error.HTTPError(request.full_url, 422, "unprocessable", {}, None)

    monkeypatch.setattr(automation, "_post_request", failing_post)
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))

    assert result["notified"] is False
    assert result["webhook_status"] == 422
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert state["last_webhook_status"] == 422


# ---------------------------------------------------------------------------
# 归因摘要：排序、方向与落盘
# ---------------------------------------------------------------------------


def test_summarize_alerts_ranks_by_threshold_normalized_severity():
    alerts = [
        {
            "metric": "销售额",
            "value": 120.0,
            "baseline_mean": 100.0,
            "zscore": 4.0,
            "pct_change": 0.2,
            "direction": "up",
            "severity": 1.3333,
            "reasons": ["z"],
        },
        {
            "metric": "订单数",
            "value": 40.0,
            "baseline_mean": 100.0,
            "zscore": None,
            "pct_change": -0.6,
            "direction": "down",
            "severity": 3.0,
            "reasons": ["pct"],
        },
    ]
    summary = automation.summarize_alerts(alerts)
    assert summary["top_metric"] == "订单数"
    assert summary["max_severity"] == 3.0
    assert summary["same_direction"] is False
    assert summary["directions"] == {"up": 1, "down": 1, "flat": 0}
    assert "订单数" in summary["headline"] and "下降" in summary["headline"]
    assert [item["metric"] for item in summary["ranking"]] == ["订单数", "销售额"]


def test_summarize_alerts_same_direction_and_empty():
    assert automation.summarize_alerts([]) is None
    alerts = [
        {
            "metric": name,
            "value": 50.0,
            "baseline_mean": 100.0,
            "zscore": -5.0,
            "pct_change": -0.5,
            "direction": "down",
            "severity": 2.5,
            "reasons": ["z"],
        }
        for name in ("销售额", "订单数")
    ]
    summary = automation.summarize_alerts(alerts)
    assert summary["same_direction"] is True
    assert summary["directions"]["down"] == 2


def test_attribution_is_written_to_alerts_webhook_and_state(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path, webhook_url="https://hooks.example.test/alert")
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)

    calls: list[object] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        automation, "_post_request", lambda request: calls.append(request) or _Resp()
    )
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))

    assert result["attribution"]["top_metric"] == "销售额"
    assert "销售额" in result["attribution"]["headline"]
    body = json.loads(calls[0].data.decode("utf-8"))
    assert body["attribution"]["max_severity"] >= 3.0
    alert_rows = [
        json.loads(line)
        for line in (automation.task_dir(runs, spec.id) / automation.ALERTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert alert_rows[-1]["attribution"]["count"] == 1
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert "销售额" in state["last_alert_headline"]


def test_next_due_at_interval_and_daily_boundaries(tmp_path):
    now = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    interval_spec = _make_spec(tmp_path, interval_minutes=60)
    assert automation.next_due_at(interval_spec, "", now) == now
    last = (now - timedelta(minutes=30)).isoformat()
    assert automation.next_due_at(interval_spec, last, now) == now + timedelta(minutes=30)

    daily_payload = _base_spec()
    daily_payload.pop("interval_minutes")
    daily_payload["daily_at"] = "09:00"
    daily_payload.update(
        dataset_path=str(tmp_path / "sales.csv"), metrics_path=str(tmp_path / "metrics.json")
    )
    daily_spec = automation.parse_spec(daily_payload)
    assert automation.next_due_at(daily_spec, "", now) == now.replace(hour=9, minute=0)

    later = datetime(2026, 11, 18, 10, 30, tzinfo=UTC)
    ran_today = datetime(2026, 11, 18, 9, 5, tzinfo=UTC).isoformat()
    assert automation.next_due_at(daily_spec, ran_today, later) == datetime(
        2026, 11, 19, 9, 0, tzinfo=UTC
    )

    disabled = _make_spec(tmp_path, enabled=False)
    assert automation.next_due_at(disabled, "", now) is None


def test_status_overview_reports_due_counts(tmp_path):
    spec = _make_spec(tmp_path)
    runs = tmp_path / "runs"
    automation.add_spec(runs, spec)
    now = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)

    overview = automation.status_overview(runs, now=now)
    assert overview["summary"] == {"total": 1, "enabled": 1, "due": 1, "alerts": 0, "errors": 0}
    task = overview["tasks"][0]
    assert task["id"] == spec.id
    assert task["schedule"] == {"interval_minutes": 60}
    assert task["last_run_at"] is None
    assert task["next_due_at"] == now.isoformat(timespec="seconds")
    assert task["state_corrupt"] is False


# ---------------------------------------------------------------------------
# 群机器人渠道适配
# ---------------------------------------------------------------------------


def _webhook_payload() -> dict:
    return {
        "task": {"id": "daily", "name": "每日销售"},
        "ran_at": "2026-11-18T08:00:00+08:00",
        "alerts": [
            {"metric": "销售额", "value": 1000, "reasons": ["z-score +4.00 ≥ 3"]},
            {"metric": "订单数", "value": 10, "reasons": []},
        ],
        "attribution": {"headline": "销售额 上升，为本次偏离最大的指标"},
    }


def test_webhook_body_adapts_known_channels():
    payload = _webhook_payload()
    slack = automation._webhook_body("https://hooks.slack.com/services/demo", payload)
    assert set(slack) == {"text"}
    assert "每日销售" in slack["text"]
    assert "销售额 上升" in slack["text"]
    assert "z-score +4.00" in slack["text"]

    feishu = automation._webhook_body("https://open.feishu.cn/open-apis/bot/v2/hook/x", payload)
    assert feishu["msg_type"] == "text"
    assert "每日销售" in feishu["content"]["text"]

    dingtalk = automation._webhook_body(
        "https://oapi.dingtalk.com/robot/send?access_token=x", payload
    )
    assert dingtalk["msgtype"] == "text"
    assert "每日销售" in dingtalk["text"]["content"]


def test_webhook_body_truncates_and_keeps_generic_payload():
    payload = _webhook_payload()
    payload["alerts"] = [
        {"metric": f"指标{i}", "value": i, "reasons": ["原因" * 200]} for i in range(12)
    ]
    text = automation._alert_text(payload)
    assert len(text) <= 3500
    assert "另有 7 项告警" in text
    assert automation._webhook_body("https://example.test/hook", payload) is payload


def test_slack_webhook_sends_text_body(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path, webhook_url="https://hooks.slack.com/services/demo")
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)
    calls: list[object] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        automation, "_post_request", lambda request: calls.append(request) or _Resp()
    )
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))
    body = json.loads(calls[0].data.decode("utf-8"))
    assert result["notified"] is True
    assert set(body) == {"text"}
    assert "每日销售" in body["text"] or spec.name in body["text"]


# ---------------------------------------------------------------------------
# 告警严重度/方向与归因摘要的边界（2026-11 评审回归）
# ---------------------------------------------------------------------------


def test_detect_direction_follows_pct_change_sign(tmp_path):
    """P1 回归：环比下降但当前值高于均值时，方向必须是 down。

    否则摘要会输出「上升（环比 -50%）」这类自相矛盾的句子。
    """
    spec = _make_spec(tmp_path)
    history = [
        {"at": f"2026-11-{10 + i:02d}T00:00:00+00:00", "metrics": {"销售额": value}}
        for i, value in enumerate((10, 10, 10, 100))
    ]

    alerts = automation.detect_anomalies({"销售额": 50}, history, spec)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["pct_change"] == pytest.approx(-0.5)
    assert alert["direction"] == "down"
    # 环比 50% / 阈值 20% = 2.5，是主导严重度
    assert alert["severity"] == pytest.approx(2.5)


def test_detect_zero_baseline_severity_at_least_threshold(tmp_path):
    spec = _make_spec(tmp_path)
    history = [{"metrics": {"销售额": 0.0}} for _ in range(3)]

    alerts = automation.detect_anomalies({"销售额": 10.0}, history, spec)

    assert len(alerts) == 1
    assert alerts[0]["severity"] == pytest.approx(1.0)
    assert alerts[0]["direction"] == "up"


def test_summarize_alerts_flat_semantics_and_missing_value():
    """flat 不参与「同向」判定；缺 value/severity 不抛错。"""
    up_and_flat = automation.summarize_alerts(
        [
            {"metric": "a", "value": 1.0, "direction": "up", "severity": 2.0},
            {"metric": "b", "value": 1.0, "direction": "flat", "severity": 1.0},
        ]
    )
    assert up_and_flat["same_direction"] is False

    all_flat = automation.summarize_alerts(
        [{"metric": "a", "value": 1.0, "direction": "flat", "severity": 1.0}]
    )
    assert all_flat["same_direction"] is False

    sparse = automation.summarize_alerts([{"metric": "m", "severity": 2.0}])
    assert sparse is not None
    assert sparse["top_metric"] == "m"
    assert sparse["ranking"][0]["value"] is None


# ---------------------------------------------------------------------------
# 邮件 / 短信投递
# ---------------------------------------------------------------------------


def test_parse_recipients_validates_and_deduplicates(tmp_path):
    spec = _make_spec(
        tmp_path,
        email_to="a@example.com, b@example.com;a@example.com",
        sms_to=["+8613800000000"],
    )
    assert spec.email_to == ("a@example.com", "b@example.com")
    assert spec.sms_to == ("+8613800000000",)
    assert spec.to_dict()["email_to"] == ["a@example.com", "b@example.com"]

    with pytest.raises(ValueError, match="email_to"):
        automation.parse_spec(_base_spec(email_to=["not-an-email"]))
    with pytest.raises(ValueError, match="sms_to"):
        automation.parse_spec(_base_spec(sms_to=["123"]))
    with pytest.raises(ValueError, match="最多 5"):
        automation.parse_spec(_base_spec(sms_to=[f"+86138000000{i:02d}" for i in range(6)]))
    with pytest.raises(ValueError, match="必须是字符串数组"):
        automation.parse_spec(_base_spec(email_to={"a": 1}))


class _FakeSMTP:
    instances: list[_FakeSMTP] = []
    fail_login: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.messages: list[object] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        if _FakeSMTP.fail_login is not None:
            raise _FakeSMTP.fail_login
        self.logged_in = (username, password)

    def send_message(self, message):
        self.messages.append(message)


def test_email_delivery_success_and_config_errors(monkeypatch):
    _FakeSMTP.instances.clear()
    _FakeSMTP.fail_login = None
    monkeypatch.setattr(automation.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv(automation.SMTP_URL_ENV, "smtp://user:secret@smtp.example.test:587")
    monkeypatch.setenv(automation.SMTP_FROM_ENV, "alerts@example.test")

    error, status, duration = automation._send_email_result(("ops@example.test",), "告警", "正文")
    assert error is None and status == 250 and duration >= 0
    client = _FakeSMTP.instances[0]
    assert client.started_tls is True
    assert client.logged_in == ("user", "secret")
    assert client.messages[0]["To"] == "ops@example.test"
    assert client.messages[0]["From"] == "alerts@example.test"

    monkeypatch.delenv(automation.SMTP_URL_ENV)
    error, status, _ = automation._send_email_result(("ops@example.test",), "s", "b")
    assert automation.SMTP_URL_ENV in error and status is None

    monkeypatch.setenv(automation.SMTP_URL_ENV, "smtp://smtp.example.test:587")
    monkeypatch.delenv(automation.SMTP_FROM_ENV)
    error, _, _ = automation._send_email_result(("ops@example.test",), "s", "b")
    assert automation.SMTP_FROM_ENV in error


def test_email_subject_with_newline_is_sanitized(monkeypatch):
    """任务名可含换行（JSON 输入）：主题必须折叠，不能造成邮件头注入。"""
    _FakeSMTP.instances.clear()
    _FakeSMTP.fail_login = None
    monkeypatch.setattr(automation.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv(automation.SMTP_URL_ENV, "smtp://user:secret@smtp.example.test:587")
    monkeypatch.setenv(automation.SMTP_FROM_ENV, "alerts@example.test")

    error, status, _ = automation._send_email_result(
        ("ops@example.test",), "[data-agent] 任务\nBcc: evil@example.test", "body"
    )
    assert error is None and status == 250
    subject = str(_FakeSMTP.instances[0].messages[0]["Subject"])
    assert "\n" not in subject and "Bcc:" in subject


def test_email_failure_redacts_password(monkeypatch):
    _FakeSMTP.instances.clear()
    _FakeSMTP.fail_login = OSError("login failed with secret")
    monkeypatch.setattr(automation.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv(automation.SMTP_URL_ENV, "smtp://user:secret@smtp.example.test:587")
    monkeypatch.setenv(automation.SMTP_FROM_ENV, "alerts@example.test")
    try:
        error, status, _ = automation._send_email_result(("ops@example.test",), "s", "b")
    finally:
        _FakeSMTP.fail_login = None
    assert "secret" not in error
    assert "***" in error
    assert status is None


def test_sms_delivery_uses_twilio_form_and_redacts(monkeypatch):
    monkeypatch.setenv(automation.TWILIO_SID_ENV, "AC123")
    monkeypatch.setenv(automation.TWILIO_TOKEN_ENV, "token-secret")
    monkeypatch.setenv(automation.TWILIO_FROM_ENV, "+12025550123")
    calls: list[object] = []

    class _Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        automation, "_post_request", lambda request: calls.append(request) or _Resp()
    )
    error, status, _ = automation._send_sms_result(("+8613800000000", "+8613800000001"), "x" * 400)
    assert error is None and status == 201 and len(calls) == 2
    form = urllib.parse.parse_qs(calls[0].data.decode("utf-8"))
    assert form["To"] == ["+8613800000000"]
    assert len(form["Body"][0]) <= 301
    assert calls[0].headers["Authorization"].startswith("Basic ")

    monkeypatch.delenv(automation.TWILIO_TOKEN_ENV)
    error, status, _ = automation._send_sms_result(("+8613800000000",), "b")
    assert "Twilio 配置不完整" in error and status is None


def test_delivery_fields_recorded_in_run(tmp_path, monkeypatch):
    monkeypatch.setenv(automation.SMTP_URL_ENV, "smtp://user:secret@smtp.example.test:587")
    monkeypatch.setenv(automation.SMTP_FROM_ENV, "alerts@example.test")
    monkeypatch.setenv(automation.TWILIO_SID_ENV, "AC123")
    monkeypatch.setenv(automation.TWILIO_TOKEN_ENV, "token-secret")
    monkeypatch.setenv(automation.TWILIO_FROM_ENV, "+12025550123")

    class _Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(automation, "_post_request", lambda request: _Resp())
    monkeypatch.setattr(automation, "_send_email_result", lambda *args: (None, 250, 5))
    monkeypatch.setattr(automation, "_send_sms_result", lambda *args: (None, 201, 6))
    spec = _make_spec(tmp_path, email_to=["ops@example.test"], sms_to=["+8613800000000"])
    runs = tmp_path / "runs"
    dataset = Path(spec.dataset_path)
    base = datetime(2026, 11, 17, tzinfo=UTC)
    for value in (100, 110, 90):
        _write_dataset(dataset, value)
        automation.run_automation(spec, runs, now=base)
    _write_dataset(dataset, 1000)
    result = automation.run_automation(spec, runs, now=base + timedelta(days=1))

    assert result["email_sent"] is True and result["sms_sent"] is True
    assert result["email_status"] == 250 and result["sms_status"] == 201
    state = json.loads((automation.task_dir(runs, spec.id) / automation.STATE_FILENAME).read_text())
    assert state["last_email_status"] == 250
    assert state["last_sms_status"] == 201


class _RawSMTPServer(threading.Thread):
    """最小 SMTP 服务器（纯 socket）：验证真实 smtplib 握手与邮件内容。

    不使用替身类，因此能覆盖真实的 EHLO / MAIL FROM / RCPT TO / DATA 流程；
    不启用 STARTTLS 与登录（`?starttls=0` + 无用户名），避免测试依赖证书。
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = int(self._sock.getsockname()[1])
        self._sock.listen(1)
        self.messages: list[str] = []

    def run(self) -> None:  # pragma: no cover - 线程体由用例驱动
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            reader = conn.makefile("rb")

            def send(text: str) -> None:
                conn.sendall((text + "\r\n").encode("utf-8"))

            send("220 localhost SMTP ready")
            in_data = False
            buffer: list[str] = []
            while True:
                raw = reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if in_data:
                    if line == ".":
                        in_data = False
                        self.messages.append("\n".join(buffer))
                        buffer = []
                        send("250 queued")
                    else:
                        buffer.append(line)
                    continue
                upper = line.upper()
                if upper.startswith(("EHLO", "HELO")):
                    send("250 localhost")
                elif upper.startswith(("MAIL FROM", "RCPT TO", "RSET", "NOOP")):
                    send("250 ok")
                elif upper.startswith("DATA"):
                    in_data = True
                    send("354 end with .")
                elif upper.startswith("QUIT"):
                    send("221 bye")
                    break
                else:
                    send("250 ok")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def test_email_real_smtp_handshake(monkeypatch):
    """真实 smtplib 走完整 SMTP 对话（不使用 smtplib 替身）。"""
    server = _RawSMTPServer()
    server.start()
    monkeypatch.setenv(automation.SMTP_URL_ENV, f"smtp://127.0.0.1:{server.port}?starttls=0")
    monkeypatch.setenv(automation.SMTP_FROM_ENV, "alerts@example.test")
    try:
        error, status, duration = automation._send_email_result(
            ("ops@example.test",), "[data-agent] 巡检告警", "销售额异常：环比 +900%"
        )
    finally:
        server.close()
        server.join(timeout=3)

    assert error is None, error
    assert status == 250 and duration >= 0
    assert server.messages, "SMTP 服务器没有收到 DATA"
    wire = server.messages[0]
    assert "To: ops@example.test" in wire
    assert "Subject:" in wire
    assert "From: alerts@example.test" in wire
