"""automation CLI 与参数校验边界（补齐覆盖率与回归防线）。"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_agent import automation


def _spec_payload(tmp_path: Path, **overrides) -> dict:
    dataset = tmp_path / "sales.csv"
    metrics = tmp_path / "metrics.json"
    if not dataset.exists():
        dataset.write_text("region,sales\nEast,100\n", encoding="utf-8")
    if not metrics.exists():
        metrics.write_text(
            json.dumps(
                {"metrics": [{"name": "销售额", "description": "合计", "expression": "sales"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    payload = {
        "id": "cli-task",
        "name": "CLI 任务",
        "dataset_path": str(dataset),
        "metrics_path": str(metrics),
        "interval_minutes": 60,
    }
    payload.update(overrides)
    return payload


def _write_spec(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"name": ""}, "缺少 name"),
        ({"dataset_path": ""}, "缺少 dataset_path"),
        ({"interval_minutes": "abc"}, "interval_minutes 必须是整数"),
        ({"interval_minutes": 0}, "必须且只能设置"),
        ({"interval_minutes": 60, "daily_at": "09:00"}, "必须且只能设置"),
        ({"zscore_threshold": "abc"}, "阈值/窗口参数必须是数字"),
        ({"interval_minutes": 0, "daily_at": "9:0"}, "HH:MM"),
        ({"zscore_threshold": 0}, "zscore_threshold"),
        ({"pct_change_threshold": -1}, "pct_change_threshold"),
        ({"history_limit": 0}, "history_limit / min_samples"),
        ({"min_samples": 0}, "history_limit / min_samples"),
        ({"min_samples": 5, "history_limit": 3}, "不能大于 history_limit"),
        ({"metric_names": "销售额"}, "metric_names 必须是字符串数组"),
        ({"webhook_url": "http:///path"}, "缺少主机名"),
        ({"webhook_url": "ftp://example.com/x"}, "只支持 http/https"),
        ({"webhook_url": "http://user:pass@example.com/x"}, "不能包含账号密码"),
    ],
)
def test_parse_rejects_invalid_fields(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        automation.parse_spec(_spec_payload(tmp_path, **overrides))


def test_parse_rejects_non_object_payload():
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        automation.parse_spec(["not-a-dict"])


def test_host_reason_edges(monkeypatch):
    assert automation._unsafe_host_reason("") == "主机为空"
    assert automation._unsafe_host_reason("2001:db8::1") == "目标是内网/环回/保留地址"

    monkeypatch.setattr(
        automation.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("192.168.1.9", 0))]
    )
    assert automation._unsafe_host_reason("internal.test") == "域名解析到内网/环回/保留地址"

    monkeypatch.setattr(
        automation.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("not-an-ip", 0)), (2, 1, 6, "", ("8.8.8.8", 0))],
    )
    assert automation._unsafe_host_reason("public.test") is None


def test_notify_short_circuits_without_url():
    assert automation._notify("", {"a": 1}) is None


def test_post_request_pins_validated_ip_and_keeps_host_header(monkeypatch):
    """IP 钉扎：连接用校验过的 IP，Host 头/超时/请求体原样透传。"""
    import ipaddress

    captured: dict = {}

    class _Resp:
        status = 200

        def __init__(self, headers=None):
            self.headers = headers or {}

        def read(self):
            return b""

    class _Conn:
        instances: list[_Conn] = []

        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            _Conn.instances.append(self)

        def request(self, method, path, body=None, headers=None):
            captured.update(method=method, path=path, body=body, headers=headers)

        def getresponse(self):
            return _Resp()

        def close(self):
            pass

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = "93.184.216.34" if host == "example.test" else host
        return [(automation.socket.AF_INET, 0, 0, "", (ip, port))]

    monkeypatch.setattr(automation.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(automation.http_client, "HTTPConnection", _Conn)

    request = automation.urllib.request.Request(
        "http://example.test/hook", data=b"{}", headers={"Content-Type": "application/json"}
    )
    response = automation._post_request(request)
    assert response.status == 200
    conn = _Conn.instances[0]
    assert conn.host == "93.184.216.34" and conn.port == 80
    assert conn.timeout == automation.WEBHOOK_TIMEOUT_SECONDS
    assert captured["method"] == "POST"
    assert captured["path"] == "/hook"
    assert captured["body"] == b"{}"
    assert captured["headers"]["host"] == "example.test"
    assert captured["headers"]["content-type"] == "application/json"
    assert ipaddress.ip_address(conn.host).is_global


def test_post_request_follows_redirect_with_revalidation(monkeypatch):
    captured_requests: list[dict] = []

    class _Resp:
        def __init__(self, status, headers=None):
            self.status = status
            self.headers = headers or {}

        def read(self):
            return b""

    class _Conn:
        def __init__(self, host, port, timeout=None):
            captured_requests.append({"host": host, "path": None})

        def request(self, method, path, body=None, headers=None):
            captured_requests[-1].update(method=method, path=path)

        def getresponse(self):
            if len(captured_requests) == 1:
                return _Resp(302, {"Location": "http://example.test/next"})
            return _Resp(200)

        def close(self):
            pass

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(automation.socket.AF_INET, 0, 0, "", ("93.184.216.34", port))]

    monkeypatch.setattr(automation.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(automation.http_client, "HTTPConnection", _Conn)
    request = automation.urllib.request.Request("http://example.test/hook", data=b"{}")
    assert automation._post_request(request).status == 200
    assert [item["path"] for item in captured_requests] == ["/hook", "/next"]
    assert captured_requests[1]["method"] == "GET"  # 302 改写为 GET 且丢弃请求体


def test_load_specs_rejects_non_list_payload(tmp_path):
    runs = tmp_path / "runs"
    path = automation.specs_path(runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"automations": "nope"}', encoding="utf-8")
    with pytest.raises(ValueError, match="必须是数组"):
        automation.load_specs(runs)


def test_read_state_tolerates_bad_json(tmp_path):
    runs = tmp_path / "runs"
    path = automation.task_dir(runs, "x") / automation.STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert automation._read_state(runs, "x") == {}


def test_read_history_skips_bad_lines(tmp_path):
    runs = tmp_path / "runs"
    path = automation.task_dir(runs, "x") / automation.HISTORY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"metrics": {"m": 1}}\nnot-json\n\n{"metrics": {"m": 2}}\n', encoding="utf-8")
    rows = automation.read_history(runs, "x", 10)
    assert [row["metrics"]["m"] for row in rows] == [1, 2]


def test_detect_ignores_non_numeric_and_nan(tmp_path):
    spec = automation.parse_spec(_spec_payload(tmp_path))
    alerts = automation.detect_anomalies(
        {"a": "x", "b": math.nan, "c": 1.0},  # type: ignore[dict-item]
        [{"metrics": {"c": 1.0}}] * 3,
        spec,
    )
    assert alerts == []


def test_run_automation_rejects_unknown_metric_filter(tmp_path):
    spec = automation.parse_spec(_spec_payload(tmp_path, metric_names=["不存在"]))
    with pytest.raises(ValueError, match="指定的指标在定义文件中不存在"):
        automation.run_automation(spec, tmp_path / "runs")


def test_cli_add_list_run_remove(tmp_path, capsys):
    runs = tmp_path / "runs"
    spec_path = _write_spec(tmp_path, _spec_payload(tmp_path))

    assert automation.main(["--runs-dir", str(runs), "add", "--spec", str(spec_path)]) == 0
    assert "cli-task" in capsys.readouterr().out

    assert automation.main(["--runs-dir", str(runs), "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == "cli-task"

    assert automation.main(["--runs-dir", str(runs), "run", "--id", "cli-task", "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["alerts"] == []

    assert automation.main(["--runs-dir", str(runs), "run-due", "--dry-run", "--no-notify"]) == 0
    json.loads(capsys.readouterr().out)

    assert automation.main(["--runs-dir", str(runs), "remove", "--id", "cli-task"]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] is True


def test_cli_add_bulk_payload(tmp_path, capsys):
    runs = tmp_path / "runs"
    payload = _spec_payload(tmp_path)
    other = dict(payload, id="cli-two", name="第二个")
    path = tmp_path / "bulk.json"
    path.write_text(json.dumps({"automations": [payload, other]}, ensure_ascii=False), "utf-8")
    assert automation.main(["--runs-dir", str(runs), "add", "--spec", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == ["cli-task", "cli-two"]


def test_cli_reports_config_errors_without_traceback(tmp_path, capsys):
    runs = tmp_path / "runs"
    missing = tmp_path / "absent.json"
    assert automation.main(["--runs-dir", str(runs), "add", "--spec", str(missing)]) == 2
    assert "错误" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert automation.main(["--runs-dir", str(runs), "add", "--spec", str(bad)]) == 2

    assert automation.main(["--runs-dir", str(runs), "run", "--id", "nope"]) == 2


def test_cli_run_due_skips_non_due(tmp_path, capsys):
    runs = tmp_path / "runs"
    spec = automation.parse_spec(_spec_payload(tmp_path))
    automation.save_specs(runs, [spec])
    automation._write_state(runs, spec.id, {"last_run_at": datetime.now(UTC).isoformat()})
    assert automation.main(["--runs-dir", str(runs), "run-due", "--no-notify"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_status_and_fail_on_alert(tmp_path, capsys):
    runs = tmp_path / "runs"
    spec_path = _write_spec(tmp_path, _spec_payload(tmp_path))
    assert automation.main(["--runs-dir", str(runs), "add", "--spec", str(spec_path)]) == 0
    capsys.readouterr()

    assert automation.main(["--runs-dir", str(runs), "status", "--fail-on-alert"]) == 1
    overview = json.loads(capsys.readouterr().out)
    assert overview["summary"]["due"] == 1
    assert overview["tasks"][0]["schedule"] == {"interval_minutes": 60}

    assert automation.main(["--runs-dir", str(runs), "run", "--id", "cli-task"]) == 0
    capsys.readouterr()
    assert automation.main(["--runs-dir", str(runs), "status", "--fail-on-alert"]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert overview["summary"]["due"] == 0
    assert overview["tasks"][0]["last_status"] == "ok"

    # 上次运行留下告警时，即使未到期也要让监控看到（alerts 计入失败判据）
    dataset = tmp_path / "sales.csv"
    for value in (100, 110, 90):
        dataset.write_text(f"region,sales\nEast,{value}\n", encoding="utf-8")
        assert automation.main(["--runs-dir", str(runs), "run", "--id", "cli-task"]) == 0
        capsys.readouterr()
    dataset.write_text("region,sales\nEast,1000\n", encoding="utf-8")
    assert automation.main(["--runs-dir", str(runs), "run", "--id", "cli-task"]) == 0
    capsys.readouterr()
    assert automation.main(["--runs-dir", str(runs), "status", "--fail-on-alert"]) == 1
    overview = json.loads(capsys.readouterr().out)
    assert overview["summary"]["alerts"] >= 1


def test_dashboard_html_is_escaped_and_refreshes(tmp_path, capsys):
    runs = tmp_path / "runs"
    payload = _spec_payload(tmp_path, name='<script>alert("x")</script>')
    spec_path = _write_spec(tmp_path, payload)
    assert automation.main(["--runs-dir", str(runs), "add", "--spec", str(spec_path)]) == 0
    capsys.readouterr()

    target = tmp_path / "dashboard.html"
    assert (
        automation.main(
            [
                "--runs-dir",
                str(runs),
                "dashboard",
                "--out",
                str(target),
                "--refresh-seconds",
                "30",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["path"] == str(target)
    html = target.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert 'http-equiv="refresh" content="30"' in html
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html
    assert "定时任务看板" in html
    assert "任务总数" in html


def test_dashboard_stdout_mode(tmp_path, capsys):
    runs = tmp_path / "runs"
    assert automation.main(["--runs-dir", str(runs), "dashboard"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("<!doctype html>")
    assert "暂无任务" in out
