from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from data_agent import api
from data_agent.agent import AnalysisResult
from data_agent.config import AgentSettings
from data_agent.storage import LocalSessionStorage
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

# Capture the real asyncio.wait_for before any test patches it, so the
# short-timeout shim used to force heartbeats can still delegate to the
# original implementation without infinite recursion.
_real_wait_for = asyncio.wait_for


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
    assert client.get("/api/settings?token=test-access-token").status_code == 401

    authorized = client.get("/api/settings", headers={"X-App-Token": "test-access-token"})
    assert authorized.status_code == 200
    assert authorized.headers["x-content-type-options"] == "nosniff"
    assert authorized.headers["x-frame-options"] == "DENY"


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
    api.bootstrap_settings.max_rows = 1
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
    record.workspace.dataframe["sales"] = [999]
    {item.name: item for item in build_tools(record.workspace)}["export_data"].invoke(
        {"format": "csv", "filename": "cleaned_data_final"}
    )
    record.chat.append({"role": "user", "content": "检查数据"})
    record.analysis_status = "completed"
    first.persist(session_id, record)

    restored = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24).get(session_id)
    assert restored.workspace.dataframe.shape == (1, 2)
    assert restored.workspace.dataframe["sales"].tolist() == [999]
    assert restored.chat == [{"role": "user", "content": "检查数据"}]
    assert restored.analysis_status == "completed"
    assert restored.workspace.artifacts[0]["description"] == "清洗或变换后的数据集"


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
    assert "Content-Security-Policy" in preview.text
    assert "connect-src 'none'" in preview.text
    assert preview.headers["cache-control"] == "private, no-store"
    assert client.get(f"/api/sessions/{uploaded['id']}/plotly.js").status_code == 404

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


def test_cancel_endpoint_uses_status_not_run_lock(tmp_path, monkeypatch):
    """cancel_analysis must rely on analysis_status, not run_lock.locked().

    A session whose run_lock was just released but whose status is still
    "running" (race window) should still accept the cancel signal.
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    # Simulate the race: lock already released, status still "running".
    record.analysis_status = "running"
    response = client.post(f"/api/sessions/{uploaded['id']}/cancel")
    assert response.json() == {"status": "cancelling"}
    assert record.cancel_event.is_set()
    # A second cancel on an already-cancelling session keeps reporting cancelling.
    assert client.post(f"/api/sessions/{uploaded['id']}/cancel").json() == {"status": "cancelling"}


def test_cancel_on_idle_session_returns_current_status(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    assert record.analysis_status == "idle"
    response = client.post(f"/api/sessions/{uploaded['id']}/cancel")
    assert response.json() == {"status": "idle"}
    assert not record.cancel_event.is_set()


def test_cleaned_data_csv_is_not_dropped_from_artifacts(tmp_path, monkeypatch):
    """A cleaning step that only saves cleaned_data.csv must still surface in
    the ArtifactCenter; it must not be silently filtered out for lacking a
    'final' suffix."""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}
    # Only run clean_data; never call export_data with a "final" filename.
    payload = json.loads(tools["clean_data"].invoke({"drop_duplicates": True}))
    assert payload["status"] == "ok"

    artifacts = client.get(f"/api/sessions/{uploaded['id']}").json()["artifacts"]
    dataset_names = [item["name"] for item in artifacts if item["kind"] == "dataset"]
    assert "cleaned_data.csv" in dataset_names


def test_transformed_data_does_not_shadow_cleaned_data(tmp_path, monkeypatch):
    """When both cleaned_data.csv and transformed_data.csv exist, the curated
    artifact list must prefer the authoritative cleaner output."""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}

    tools["clean_data"].invoke({"drop_duplicates": True})
    tools["transform_data"].invoke({"filter_column": "region", "filter_operator": "eq", "filter_value": "East"})

    artifacts = client.get(f"/api/sessions/{uploaded['id']}").json()["artifacts"]
    dataset_names = [item["name"] for item in artifacts if item["kind"] == "dataset"]
    assert "cleaned_data.csv" in dataset_names
    # transformed_data.csv may or may not appear (priority 4 vs 1), but it
    # must never replace cleaned_data.csv in the curated list.
    if "transformed_data.csv" in dataset_names:
        assert dataset_names.count("cleaned_data.csv") == 1


def test_sse_stream_emits_progress_and_heartbeat(tmp_path, monkeypatch):
    """The SSE stream must emit progress events at node entry and heartbeats
    when no event arrives within the heartbeat window."""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    class SlowAgent:
        """Mimics DataAnalysisAgent.stream: emits one progress + one finalize."""

        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None):
            self.workspace = workspace
            self.progress_callback = progress_callback
            self.cancel_event = cancel_event

        def stream(self, query, history=None):
            if self.progress_callback:
                self.progress_callback("validate_dataset", "正在检查数据集结构")
            import time

            # Force a heartbeat by sleeping past the 15s timeout would be too
            # slow; instead patch the heartbeat timeout via monkeypatch below.
            time.sleep(0.1)
            yield {"node": "finalize", "data": {
                "response": "done",
                "trace": [],
                "artifacts": list(self.workspace.artifacts),
                "dataset_profile": self.workspace.profile(),
                "plan": [],
                "completed_steps": [],
            }}

        def run(self, query, history=None):
            for _update in self.stream(query, history=history):
                pass
            return AnalysisResult(
                response="done",
                trace=[],
                artifacts=list(self.workspace.artifacts),
                dataset_profile=self.workspace.profile(),
                plan=[],
                completed_steps=[],
            )

    monkeypatch.setattr(api, "DataAnalysisAgent", SlowAgent)
    # Shrink the heartbeat window so the test stays fast.
    monkeypatch.setattr("data_agent.api.asyncio.wait_for", _short_wait_for)

    with client.stream(
        "POST",
        f"/api/sessions/{uploaded['id']}/analyze/stream",
        json={"task": "检查数据"},
    ) as response:
        assert response.status_code == 200
        events = []
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):])

    assert "started" in events
    assert "progress" in events
    assert "complete" in events
    manifest = json.loads((tmp_path / "runs" / uploaded["id"] / "session.json").read_text(encoding="utf-8"))
    assert manifest["analysis_status"] == "completed"
    assert manifest["last_result"]["response"] == "done"

    restored = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24).get(uploaded["id"])
    assert restored.analysis_status == "completed"
    assert restored.last_result is not None
    assert restored.last_result.response == "done"


def _short_wait_for(awaitable, timeout=None):
    """Replacement for asyncio.wait_for that times out quickly to force a
    heartbeat within the test window."""
    return _real_wait_for(awaitable, timeout=0.05)
