from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from data_agent import api
from data_agent.agent import AnalysisResult
from data_agent.config import AgentSettings
from data_agent.storage import LocalSessionStorage
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _isolate_runtime(tmp_path: Path, monkeypatch) -> None:
    """Replace the module-level registry, storage and analysis slots.

    The API module initializes these objects at import time using whatever
    environment was active then. Tests need a fresh, isolated runs directory
    per test, so we swap the globals explicitly instead of relying on
    ``monkeypatch.setenv`` which has no effect after import.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(
        api_key="not-used",
        provider="deepseek",
        runs_dir=runs_dir,
        max_active_sessions=100,
        session_ttl_hours=24,
        rate_limit_per_minute=1000,
        max_concurrent_analyses=2,
    )
    registry = api.SessionRegistry(
        runs_dir,
        settings.max_active_sessions,
        settings.session_ttl_hours,
        storage=LocalSessionStorage(),
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "registry", registry)
    monkeypatch.setattr(api, "session_storage", LocalSessionStorage())
    monkeypatch.setattr(api, "analysis_slots", threading.BoundedSemaphore(settings.max_concurrent_analyses))


def test_health_and_upload_session(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["architecture"] == "plan-and-execute-react"

    response = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["filename"] == "sales.csv"
    assert payload["profile"]["rows"] == 2
    assert payload["profile"]["columns"] == 2
    assert payload["preview"][0]["region"] == "East"


def test_analyze_endpoint_returns_plan(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    class FakeAgent:
        def __init__(self, workspace, settings, **kwargs):
            self.workspace = workspace

        def run(self, task, history=None):
            return AnalysisResult(
                response="分析完成",
                trace=[{"type": "tool_call", "name": "inspect_data", "detail": "{}"}],
                artifacts=[],
                dataset_profile=self.workspace.profile(),
                plan=[
                    {
                        "id": "inspect",
                        "title": "检查数据",
                        "instruction": "检查",
                        "success_criteria": "完成概况",
                    }
                ],
                completed_steps=[{"id": "inspect", "title": "检查数据", "summary": "完成"}],
            )

    monkeypatch.setattr(api, "DataAnalysisAgent", FakeAgent)
    response = client.post(
        f"/api/sessions/{uploaded['id']}/analyze",
        json={"task": "检查销售数据"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "分析完成"
    assert payload["plan"][0]["id"] == "inspect"
    assert payload["completed_steps"][0]["summary"] == "完成"


def test_access_token_protects_api(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_TOKEN", "test-access-token")
    client = TestClient(api.app)

    status = client.get("/api/auth")
    assert status.json() == {"required": True, "authenticated": False}
    assert client.get("/api/settings").status_code == 401

    authorized = client.get("/api/settings", headers={"X-App-Token": "test-access-token"})
    assert authorized.status_code == 200


def test_access_token_protects_analysis_stream(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_TOKEN", "test-access-token")
    client = TestClient(api.app)

    response = client.post(
        "/api/sessions/api_missing/analyze/stream",
        json={"task": "检查数据"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "需要有效的应用访问令牌。"


def test_upload_stream_enforces_dataset_limits(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("DATA_AGENT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("DATA_AGENT_MAX_ROWS", "1")
    client = TestClient(api.app)

    response = client.post(
        "/api/sessions",
        files={"file": ("too_many.csv", b"a,b\n1,2\n3,4\n", "text/csv")},
    )
    assert response.status_code == 422
    assert "数据规模超过限制" in response.json()["detail"]


def test_session_manifest_can_restore_persistent_workspace(tmp_path):
    workspace = DataWorkspace(tmp_path / "runs", session_id="api_restore")
    source = workspace.save_upload("sales.csv", b"region,sales\nEast,100\n")
    workspace.load(source)
    first = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    session_id, record = first.create(workspace)
    record.chat.append({"role": "user", "content": "检查数据"})
    first.persist(session_id, record)

    restored = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24).get(session_id)
    assert restored.workspace.dataframe.shape == (1, 2)
    assert restored.chat == [{"role": "user", "content": "检查数据"}]


def test_artifact_payload_is_curated_and_chart_preview_is_standalone(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}
    for title in ("区域销售（n=2）", "区域销售（样本=2）"):
        tools["create_visualization"].invoke(
            {"chart_type": "bar", "x": "region", "y": "sales", "title": title}
        )
    tools["export_data"].invoke({"format": "csv", "filename": "cleaned_data_final"})

    artifacts = client.get(f"/api/sessions/{uploaded['id']}").json()["artifacts"]
    assert [item["kind"] for item in artifacts] == ["visualization", "dataset"]
    chart = artifacts[0]
    assert chart["previewable"] is True

    preview = client.get(chart["preview_url"])
    assert preview.status_code == 200
    assert "<script src='plotly.min.js'" not in preview.text
    assert "Plotly" in preview.text

    download = client.get(chart["download_url"])
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "<script src='plotly.min.js'" not in download.text


def test_running_analysis_can_be_cancelled(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    record.run_lock.acquire()
    record.analysis_status = "running"
    try:
        response = client.post(f"/api/sessions/{uploaded['id']}/cancel")
        assert response.json() == {"status": "cancelling"}
        assert record.cancel_event.is_set()
    finally:
        record.run_lock.release()
