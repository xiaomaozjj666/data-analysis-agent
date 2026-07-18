from __future__ import annotations

from fastapi.testclient import TestClient

from data_agent import api
from data_agent.agent import AnalysisResult
from data_agent.config import AgentSettings
from data_agent.workspace import DataWorkspace


def test_health_and_upload_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_RUNS_DIR", str(tmp_path / "runs"))
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
    monkeypatch.setenv("DATA_AGENT_RUNS_DIR", str(tmp_path / "runs"))
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
        def __init__(self, workspace, settings):
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


def test_upload_stream_enforces_dataset_limits(tmp_path, monkeypatch):
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
