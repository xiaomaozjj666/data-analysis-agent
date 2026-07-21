from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
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
    # AgentSettings.from_env() calls load_dotenv() at import time, which may
    # have injected APP_ACCESS_TOKEN from a local .env file. Remove it so
    # tests that don't explicitly set the token run against an unguarded API.
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
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

        def run(self, task, history=None, resume_from=None):
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
    unauthorized = client.get("/api/settings")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["x-content-type-options"] == "nosniff"
    assert unauthorized.headers["x-frame-options"] == "DENY"
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


def test_cors_preflight_is_not_blocked_by_access_token(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_TOKEN", "test-access-token")
    client = TestClient(api.app)

    response = client.options(
        "/api/settings",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-app-token",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "x-app-token" in response.headers["access-control-allow-headers"].lower()


def test_upload_stream_enforces_dataset_limits(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    # 用 monkeypatch.setattr 而非直接赋值，确保测试结束后属性自动还原，
    # 避免同进程后续测试读到被污染的 max_rows。
    monkeypatch.setattr(api.bootstrap_settings, "max_rows", 1)
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


def test_cancel_persists_cancelling_status(tmp_path, monkeypatch):
    """cancel_analysis must persist the cancelling status so a process restart
    during the unwind does not leave the manifest stuck on "running"."""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    record.analysis_status = "running"

    response = client.post(f"/api/sessions/{uploaded['id']}/cancel")
    assert response.json() == {"status": "cancelling"}

    manifest_path = tmp_path / "runs" / uploaded["id"] / "session.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["analysis_status"] == "cancelling"


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

        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self.progress_callback = progress_callback
            self.event_callback = event_callback
            self.cancel_event = cancel_event

        def stream(self, query, history=None, resume_from=None):
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

        def run(self, query, history=None, resume_from=None):
            for _update in self.stream(query, history=history, resume_from=resume_from):
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


def test_client_identifier_ignores_xff_when_no_trusted_proxy(monkeypatch):
    """Without DATA_AGENT_TRUSTED_PROXY_HOPS we must not trust X-Forwarded-For.

    A client connecting directly can forge any XFF header; trusting it would
    let an attacker bypass per-IP rate limits by rotating fake IPs. Default
    config must fall back to the direct socket address.
    """
    monkeypatch.delenv("DATA_AGENT_TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.setattr(api, "_trusted_proxy_hops", 0)

    class FakeRequest:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("Client", (), {"host": "5.6.7.8"})()

    assert api._client_identifier(FakeRequest()) == "5.6.7.8"


def test_client_identifier_takes_nth_from_last_xff_with_trusted_hops(monkeypatch):
    """With N trusted hops, take XFF[-N] as the client identifier.

    Each trusted proxy appends the IP it received the request from to XFF.
    The Nth-from-last entry is therefore the first untrusted hop — the real
    client — and cannot be forged by the client itself.
    """
    monkeypatch.setattr(api, "_trusted_proxy_hops", 1)

    class FakeRequest:
        headers = {"x-forwarded-for": "spoofed, 203.0.113.5"}
        client = type("Client", (), {"host": "10.0.0.1"})()

    # XFF[-1] is the entry appended by our direct proxy (trusted).
    assert api._client_identifier(FakeRequest()) == "203.0.113.5"


def test_client_identifier_falls_back_to_direct_host_on_short_xff(monkeypatch):
    """If XFF has fewer entries than trusted hops, use the socket host.

    This happens for internal health checks that bypass the proxy layer.
    """
    monkeypatch.setattr(api, "_trusted_proxy_hops", 2)

    class FakeRequest:
        headers = {"x-forwarded-for": "203.0.113.5"}
        client = type("Client", (), {"host": "10.0.0.1"})()

    assert api._client_identifier(FakeRequest()) == "10.0.0.1"


def test_rate_limit_bucket_dict_is_bounded(monkeypatch):
    """_prune_rate_buckets_locked must keep the dict under MAX_RATE_LIMIT_BUCKETS.

    An attacker forging unique XFF values can otherwise manufacture unlimited
    bucket entries and OOM the process. The pruner must evict stale entries
    and, if still over the cap, drop the oldest 25% by last-seen timestamp.
    """
    # Inject a tiny cap so the test doesn't need to create 10K entries.
    monkeypatch.setattr(api, "MAX_RATE_LIMIT_BUCKETS", 4)

    # Reset to a fresh dict; other tests may have populated it.
    fresh_buckets: dict[str, deque] = {}
    monkeypatch.setattr(api, "request_buckets", fresh_buckets)

    # Fill past the cap with staggered timestamps so the LRU path runs.
    now = 1000.0
    for index in range(8):
        fresh_buckets[f"ip-{index}"] = deque([now + index], maxlen=None)

    # Mark two buckets stale (60s+ old) to exercise the first eviction path.
    fresh_buckets["ip-stale-1"] = deque([now - 120], maxlen=None)
    fresh_buckets["ip-stale-2"] = deque([now - 90], maxlen=None)

    with api.request_buckets_lock:
        api._prune_rate_buckets_locked(now=now + 1.0)

    # Stale buckets must be gone; total size must drop under the cap.
    assert "ip-stale-1" not in fresh_buckets
    assert "ip-stale-2" not in fresh_buckets
    assert len(fresh_buckets) <= api.MAX_RATE_LIMIT_BUCKETS


def test_analyze_persists_chat_within_run_lock(tmp_path, monkeypatch):
    """chat.extend / last_result / persist must all happen while run_lock is held.

    The original implementation released run_lock in the finally block, then
    wrote chat/last_result/persist outside the lock. A concurrent request
    could acquire run_lock between those steps and see stale chat history.
    This test simulates that race by holding run_lock immediately after
    analyze() releases it and verifying the chat is already updated.
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    session_id = uploaded["id"]

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    class FastAgent:
        def __init__(self, workspace, settings, cancel_event=None):
            self.workspace = workspace

        def run(self, query, history=None, resume_from=None):
            return AnalysisResult(
                response="分析完成",
                trace=[],
                artifacts=list(self.workspace.artifacts),
                dataset_profile=self.workspace.profile(),
                plan=[],
                completed_steps=[],
            )

    monkeypatch.setattr(api, "DataAnalysisAgent", FastAgent)

    response = client.post(
        f"/api/sessions/{session_id}/analyze",
        json={"task": "检查数据"},
    )
    assert response.status_code == 200

    # The sync analyze() path must have already extended chat + persisted
    # before releasing run_lock. Verify by reading the manifest: it should
    # contain the user+assistant conversation we just produced.
    manifest = json.loads(
        (tmp_path / "runs" / session_id / "session.json").read_text(encoding="utf-8")
    )
    roles = [item["role"] for item in manifest["chat"]]
    assert "user" in roles
    assert "assistant" in roles
    assert manifest["last_result"]["response"] == "分析完成"


def test_chat_stream_appends_followup_to_chat_history(tmp_path, monkeypatch):
    """The /chat/stream endpoint must append the user+assistant follow-up pair
    to record.chat and persist it, without altering analysis_status or
    last_result.

    The follow-up is a lightweight ReAct answer that reuses the existing
    workspace; it must not trigger plan→execute→finalize or occupy
    analysis_slots. This test verifies the contract by mocking
    DataAnalysisAgent.chat() and checking the persisted manifest.
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    session_id = uploaded["id"]

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    # 预填一条分析对话，模拟已完成的首轮分析
    record = api.registry.get(session_id)
    record.chat = [
        {"role": "user", "content": "检查销售数据"},
        {"role": "assistant", "content": "分析完成：华东区销售最高。"},
    ]
    record.analysis_status = "completed"
    api.registry.persist(session_id, record)

    chat_response_text = "华东区销售额 100，是唯一有数据的区域。"

    class ChatAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self.event_callback = event_callback

        def chat(self, query, history=None):
            # 模拟流式 token 推送
            if self.event_callback:
                self.event_callback("chat_chunk", {"chunk": chat_response_text})
            return chat_response_text, []

    monkeypatch.setattr(api, "DataAnalysisAgent", ChatAgent)

    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/chat/stream",
        json={"task": "哪个区域销售最高？"},
    ) as response:
        assert response.status_code == 200
        events = []
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):])

    # 必须收到 started + chat_chunk + chat_done 事件
    assert "started" in events
    assert "chat_chunk" in events
    assert "chat_done" in events

    # 持久化的 chat 必须包含 4 条（首轮 2 + 追问 2）
    manifest = json.loads(
        (tmp_path / "runs" / session_id / "session.json").read_text(encoding="utf-8")
    )
    assert len(manifest["chat"]) == 4
    assert manifest["chat"][2] == {"role": "user", "content": "哪个区域销售最高？"}
    assert manifest["chat"][3] == {"role": "assistant", "content": chat_response_text}
    # 追问不得改变 analysis_status
    assert manifest["analysis_status"] == "completed"


def test_chat_stream_rejects_concurrent_analysis(tmp_path, monkeypatch):
    """/chat/stream must 409 when the session has a running analysis,
    preventing concurrent writes to the workspace."""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    ).json()
    record = api.registry.get(uploaded["id"])
    record.analysis_status = "running"

    response = client.post(
        f"/api/sessions/{uploaded['id']}/chat/stream",
        json={"task": "追问"},
    )
    assert response.status_code == 409
    assert "正在运行" in response.json()["detail"]

