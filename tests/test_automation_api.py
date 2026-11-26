"""定时任务路由：列表 / 历史 / 启停 / 立即运行 / 错误口径。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from data_agent import api, automation
from data_agent.config import AgentSettings
from data_agent.storage import LocalSessionStorage


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(
        api_key="not-used",
        provider="deepseek",
        runs_dir=runs_dir,
        rate_limit_per_minute=1000,
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "session_storage", LocalSessionStorage())
    api.request_buckets.clear()
    return runs_dir


def _write_task_files(base: Path) -> automation.AutomationSpec:
    dataset = base / "sales.csv"
    dataset.write_text("region,sales\nEast,100\n", encoding="utf-8")
    metrics = base / "metrics.json"
    metrics.write_text(
        json.dumps(
            {"metrics": [{"name": "销售额", "description": "合计", "expression": "sales"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return automation.parse_spec(
        {
            "id": "daily-sales",
            "name": "每日销售",
            "dataset_path": str(dataset),
            "metrics_path": str(metrics),
            "interval_minutes": 60,
        }
    )


def test_list_history_toggle_and_run(tmp_path, monkeypatch):
    runs_dir = _isolate(tmp_path, monkeypatch)
    spec = _write_task_files(tmp_path)
    automation.save_specs(runs_dir, [spec])
    client = TestClient(api.app)

    overview = client.get("/api/automation/tasks")
    assert overview.status_code == 200
    body = overview.json()
    assert body["summary"]["total"] == 1
    task = body["tasks"][0]
    assert task["id"] == "daily-sales"
    assert task["enabled"] is True
    assert task["schedule"] == {"interval_minutes": 60}

    # 尚未运行过：历史为空。
    history = client.get("/api/automation/tasks/daily-sales/history")
    assert history.status_code == 200
    assert history.json() == {"task_id": "daily-sales", "history": []}

    # 试运行不落历史/状态。
    dry = client.post("/api/automation/tasks/daily-sales/run?dry_run=true")
    assert dry.status_code == 200
    assert dry.json()["id"] == "daily-sales"
    assert client.get("/api/automation/tasks/daily-sales/history").json()["history"] == []

    # 正式运行写入历史。
    run = client.post("/api/automation/tasks/daily-sales/run")
    assert run.status_code == 200
    assert run.json()["id"] == "daily-sales"
    assert len(client.get("/api/automation/tasks/daily-sales/history").json()["history"]) == 1

    # 启停开关返回最新概览。
    toggled = client.patch("/api/automation/tasks/daily-sales", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["tasks"][0]["enabled"] is False
    assert toggled.json()["summary"]["enabled"] == 0


def test_unknown_task_returns_404(tmp_path, monkeypatch):
    runs_dir = _isolate(tmp_path, monkeypatch)
    automation.save_specs(runs_dir, [_write_task_files(tmp_path)])
    client = TestClient(api.app)

    assert client.get("/api/automation/tasks/missing/history").status_code == 404
    assert (
        client.patch("/api/automation/tasks/missing", json={"enabled": False}).status_code
        == 404
    )
    assert client.post("/api/automation/tasks/missing/run").status_code == 404


def test_corrupt_specs_returns_422(tmp_path, monkeypatch):
    runs_dir = _isolate(tmp_path, monkeypatch)
    specs = automation.specs_path(runs_dir)
    specs.parent.mkdir(parents=True, exist_ok=True)
    specs.write_text("{not-json", encoding="utf-8")
    client = TestClient(api.app)

    assert client.get("/api/automation/tasks").status_code == 422
    # PATCH 走 _specs() 的同一错误分支，确保配置损坏不会 500。
    assert (
        client.patch("/api/automation/tasks/daily-sales", json={"enabled": False}).status_code
        == 422
    )


def test_run_reports_lock_and_configuration_errors(tmp_path, monkeypatch):
    runs_dir = _isolate(tmp_path, monkeypatch)
    spec = _write_task_files(tmp_path)
    automation.save_specs(runs_dir, [spec])
    client = TestClient(api.app)

    monkeypatch.setattr(
        automation, "run_automation", lambda *args, **kwargs: {"id": "daily-sales", "skipped": "locked"}
    )
    locked = client.post("/api/automation/tasks/daily-sales/run")
    assert locked.status_code == 409

    def _boom(*_args, **_kwargs):
        raise ValueError("没有可用的指标定义")

    monkeypatch.setattr(automation, "run_automation", _boom)
    failed = client.post("/api/automation/tasks/daily-sales/run")
    assert failed.status_code == 422
    assert "指标定义" in failed.json()["detail"]


def test_history_and_specs_io_errors_return_422(tmp_path, monkeypatch):
    runs_dir = _isolate(tmp_path, monkeypatch)
    automation.save_specs(runs_dir, [_write_task_files(tmp_path)])
    client = TestClient(api.app)

    def _raise_os(*_args, **_kwargs):
        raise OSError("磁盘不可用")

    monkeypatch.setattr(automation, "read_history", _raise_os)
    assert client.get("/api/automation/tasks/daily-sales/history").status_code == 422

    monkeypatch.setattr(automation, "save_specs", _raise_os)
    assert (
        client.patch("/api/automation/tasks/daily-sales", json={"enabled": False}).status_code
        == 422
    )


def test_concurrent_run_lock_is_reported(tmp_path, monkeypatch):
    """真实锁文件被占用时，run 返回 409（不依赖 monkeypatch 实现）。"""
    runs_dir = _isolate(tmp_path, monkeypatch)
    automation.save_specs(runs_dir, [_write_task_files(tmp_path)])
    client = TestClient(api.app)

    holder_ready = threading.Event()
    release = threading.Event()

    def _hold():
        with automation._task_lock(runs_dir, "daily-sales"):
            holder_ready.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    assert holder_ready.wait(timeout=5)
    try:
        assert client.post("/api/automation/tasks/daily-sales/run").status_code == 409
    finally:
        release.set()
        thread.join(timeout=5)
