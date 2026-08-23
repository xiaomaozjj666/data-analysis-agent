from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import zipfile
from collections import deque
from pathlib import Path

import pytest
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
    # _effective_settings() re-reads env at runtime; provide a dummy key so
    # tests behave the same with or without a local .env (CI has none).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
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
    # 速率限制器使用全局 request_buckets 字典累积请求，跨测试不清空会导致
    # 后续使用默认 rate_limit_per_minute=30 的测试文件（如 test_dashboard）
    # 被前序测试的残余请求阻塞，返回 429。每个测试开始时清空桶以确保隔离。
    api.request_buckets.clear()


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

        def stream(self, query, history=None, resume_from=None, plan_only=False):
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
            self._last_usage = None
            self._last_reasoning = ""

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


def test_delete_session_removes_workspace_and_history_entry(tmp_path, monkeypatch):
    """DELETE /api/sessions/{id} 应清理工作区目录、从历史列表消失。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()
    session_id = uploaded["id"]

    # 删除前：会话存在于历史列表，工作区目录存在
    history_before = client.get("/api/sessions?limit=30").json()["sessions"]
    assert any(s["id"] == session_id for s in history_before)
    session_dir = tmp_path / "runs" / session_id
    assert session_dir.is_dir()

    # 删除
    delete_response = client.delete(f"/api/sessions/{session_id}")
    assert delete_response.status_code == 204

    # 删除后：工作区目录已清理，历史列表不再包含该会话
    assert not session_dir.exists()
    history_after = client.get("/api/sessions?limit=30").json()["sessions"]
    assert not any(s["id"] == session_id for s in history_after)

    # 再次访问该会话应 404
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_delete_session_rejects_invalid_session_id(tmp_path, monkeypatch):
    """非法 session_id（格式不符）应返回 404 而非删除任意目录。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    assert client.delete("/api/sessions/!invalid!").status_code == 404


def test_sample_endpoint_creates_session_with_sales_data(tmp_path, monkeypatch):
    """POST /api/sessions/sample 应创建内置示例会话，可正常加载数据。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.post("/api/sessions/sample")
    assert response.status_code == 201
    payload = response.json()
    assert payload["id"].startswith("api_")
    assert payload["filename"] == "sample_sales.csv"
    # 示例数据含 20 行订单，列含 region/sales 等字段
    assert isinstance(payload["profile"], dict)
    history = client.get("/api/sessions?limit=30").json()["sessions"]
    assert any(s["id"] == payload["id"] for s in history)


def test_rename_session_persists_title_and_falls_back_on_empty(tmp_path, monkeypatch):
    """PATCH /api/sessions/{id} 应持久化自定义标题，空串回退到 filename。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()
    session_id = uploaded["id"]

    # 设置自定义标题
    rename_resp = client.patch(
        f"/api/sessions/{session_id}", json={"title": "我的销售分析"}
    )
    assert rename_resp.status_code == 200
    assert rename_resp.json()["title"] == "我的销售分析"

    # 历史列表与详情都应反映新标题
    history = client.get("/api/sessions?limit=30").json()["sessions"]
    matched = [s for s in history if s["id"] == session_id][0]
    assert matched["title"] == "我的销售分析"
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["title"] == "我的销售分析"

    # 空串清除自定义标题，回退 filename
    clear_resp = client.patch(f"/api/sessions/{session_id}", json={"title": "   "})
    assert clear_resp.status_code == 200
    assert clear_resp.json()["title"] == ""
    detail_after = client.get(f"/api/sessions/{session_id}").json()
    assert detail_after["title"] in (None, "")


def test_rename_session_rejects_unknown_session(tmp_path, monkeypatch):
    """PATCH 不存在的会话应返回 404。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    assert client.patch("/api/sessions/api_nonexistent", json={"title": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# 错误分级与 SSE 进度事件（routers/analysis.py + callbacks.py）
# ---------------------------------------------------------------------------


def test_classify_analysis_error_maps_known_failures():
    """关键词分级应把 openai/LangChain 风格异常归到对应错误码。"""
    from data_agent.routers.analysis import _classify_analysis_error

    cases = [
        (TimeoutError("Request timed out."), "model_timeout"),
        (Exception("Error code: 402 - Insufficient Balance"), "quota_exhausted"),
        (Exception("Error code: 429 - Too Many Requests"), "rate_limited"),
        (Exception("AuthenticationError: invalid api key provided"), "auth_failed"),
        (Exception("APIConnectionError: Connection error."), "connection_failed"),
    ]
    for exc, expected_code in cases:
        code, hint = _classify_analysis_error(exc)
        assert code == expected_code, f"{exc!r} 应归类为 {expected_code}，实际 {code}"
        assert hint, f"{expected_code} 应携带非空友好提示"


def test_classify_analysis_error_falls_back_to_generic():
    """未命中任何关键词时回退 analysis_failed + 空 hint（保持旧版行为）。"""
    from data_agent.routers.analysis import _classify_analysis_error

    assert _classify_analysis_error(ValueError("列 region 不存在")) == ("analysis_failed", "")


def test_error_payload_prepends_hint_and_keeps_code():
    """命中分级时 message = hint（原始文案），并保留独立 code/hint 字段。"""
    from data_agent.routers.analysis import _error_payload

    payload = _error_payload(Exception("Error code: 402 - Insufficient Balance"))
    assert payload["code"] == "quota_exhausted"
    assert payload["message"].startswith(payload["hint"])
    assert "Error code: 402 - Insufficient Balance" in payload["message"]


def test_error_payload_fallback_uses_prefix():
    """未分级异常沿用 prefix + 原始文案，hint 为空。"""
    from data_agent.routers.analysis import _error_payload

    payload = _error_payload(ValueError("列 region 不存在"), prefix="分析失败：")
    assert payload == {
        "message": "分析失败：列 region 不存在",
        "code": "analysis_failed",
        "hint": "",
    }


def test_tool_trace_callback_emits_step_progress_with_context():
    """每次 on_tool_start 应推送 step_progress，携带步骤序号与封顶 90% 的进度。"""
    from data_agent.callbacks import ToolTraceCallback

    events: list[tuple[str, dict]] = []
    cb = ToolTraceCallback(lambda event, payload: events.append((event, payload)), step_index=2, total_steps=4)

    cb.on_tool_start({"name": "run_python"}, "print(1)", run_id="r1")
    progress = [payload for event, payload in events if event == "step_progress"]
    assert progress[-1] == {
        "progress": 20,
        "tool_calls": 1,
        "message": "第 1 次工具调用",
        "step_index": 2,
        "total_steps": 4,
    }
    # tool_call 事件与 step_progress 成对推送
    assert any(event == "tool_call" and payload["name"] == "run_python" for event, payload in events)

    # 连续调用：进度按 20%/次 递增并封顶 90%
    for i in range(6):
        cb.on_tool_start({"name": "run_python"}, "x", run_id=f"r{i + 2}")
    progress = [payload for event, payload in events if event == "step_progress"]
    assert progress[-1]["progress"] == 90
    assert progress[-1]["tool_calls"] == 7

    # reset() 在步骤边界清零计数
    cb.reset()
    cb.on_tool_start({"name": "run_python"}, "x", run_id="r-next")
    progress = [payload for event, payload in events if event == "step_progress"]
    assert progress[-1]["tool_calls"] == 1
    assert progress[-1]["progress"] == 20


def test_tool_trace_callback_swallows_event_callback_errors():
    """事件回调抛错不能中断 ReAct 循环（工具调用照常执行）。"""
    from data_agent.callbacks import ToolTraceCallback

    def broken_callback(event: str, payload: dict) -> None:
        raise RuntimeError("sink 崩溃")

    cb = ToolTraceCallback(broken_callback)
    cb.on_tool_start({"name": "run_python"}, "x", run_id="r1")  # 不应抛出
    cb.on_tool_end("ok", run_id="r1")  # 不应抛出


# ---------------------------------------------------------------------------
# 路由层覆盖补齐：sessions / settings / artifacts
# ---------------------------------------------------------------------------


def _upload_csv_session(client, name="sales.csv", content=b"region,sales\nEast,100\nWest,200\n"):
    """上传 CSV 创建会话，返回上传响应 JSON（含 id 等字段）。"""
    response = client.post(
        "/api/sessions",
        files={"file": (name, content, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_chart_session(client):
    """上传 CSV 并用 build_tools 生成一个柱状图产物，返回 session_id。"""
    uploaded = _upload_csv_session(client)
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}
    tools["create_visualization"].invoke(
        {"chart_type": "bar", "x": "region", "y": "sales", "title": "sales_by_region"}
    )
    return uploaded["id"]


def _first_chart_artifact(client, session_id):
    """从会话详情中取出第一个 visualization 产物。"""
    artifacts = client.get(f"/api/sessions/{session_id}").json()["artifacts"]
    return next(item for item in artifacts if item["kind"] == "visualization")


# --- sessions 路由 ---


def test_list_sessions_respects_limit_bounds(tmp_path, monkeypatch):
    """GET /api/sessions 的 limit 参数应在 [1, 100] 区间内截断。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    _upload_csv_session(client)
    _upload_csv_session(client)

    # limit 超过上限应截断到 100，不报错
    big = client.get("/api/sessions?limit=9999").json()
    assert len(big["sessions"]) == 2

    # limit 小于 1 应截断到 1
    small = client.get("/api/sessions?limit=0").json()
    assert len(small["sessions"]) == 1


def test_get_session_detail_returns_full_payload(tmp_path, monkeypatch):
    """GET /api/sessions/{id} 应返回完整的会话详情载荷。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)

    detail = client.get(f"/api/sessions/{uploaded['id']}").json()
    assert detail["id"] == uploaded["id"]
    assert detail["filename"] == "sales.csv"
    assert detail["profile"]["rows"] == 2
    assert detail["profile"]["columns"] == 2
    assert detail["analysis_status"] == "idle"
    assert isinstance(detail["artifacts"], list)
    assert isinstance(detail["chat"], list)


def test_rename_session_handles_null_title(tmp_path, monkeypatch):
    """PATCH /api/sessions/{id} 传入 title=null 应视为清除自定义标题（与空串一致）。

    修复前 str(None) 会把 null 变成字面量 "None" 存进标题；修复后 null 与
    空串语义统一：清除标题、回退显示 filename。
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)

    # 先设置一个自定义标题，再传 null 清除
    renamed = client.patch(f"/api/sessions/{uploaded['id']}", json={"title": "临时标题"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "临时标题"

    response = client.patch(f"/api/sessions/{uploaded['id']}", json={"title": None})
    assert response.status_code == 200
    assert response.json()["title"] == ""
    detail = client.get(f"/api/sessions/{uploaded['id']}").json()
    assert detail["title"] in (None, "")


def test_delete_running_session_returns_409(tmp_path, monkeypatch):
    """DELETE 运行中的会话（run_lock 持有）应返回 409。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    record = api.registry.get(uploaded["id"])
    record.run_lock.acquire()
    try:
        response = client.delete(f"/api/sessions/{uploaded['id']}")
        assert response.status_code == 409
        assert "运行" in response.json()["detail"]
    finally:
        record.run_lock.release()


def test_export_session_returns_zip_archive(tmp_path, monkeypatch):
    """GET /api/sessions/{id}/export 应返回包含会话文件的 ZIP 流。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)

    response = client.get(f"/api/sessions/{uploaded['id']}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]

    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    names = bundle.namelist()
    # 应包含原始数据文件与会话清单
    assert any(n.startswith("input/") for n in names)
    assert "session.json" in names


def test_import_session_restores_from_exported_zip(tmp_path, monkeypatch):
    """POST /api/sessions/import 应从 export 导出的 ZIP 恢复完整会话。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)

    export = client.get(f"/api/sessions/{uploaded['id']}/export")
    assert export.status_code == 200

    imported = client.post(
        "/api/sessions/import",
        files={"file": ("session.zip", export.content, "application/zip")},
    )
    assert imported.status_code == 201, imported.text
    payload = imported.json()
    assert payload["id"].startswith("api_")
    assert payload["id"] != uploaded["id"]
    assert payload["filename"] == "sales.csv"
    assert payload["profile"]["rows"] == 2

    # 导入的会话应出现在历史列表
    history = client.get("/api/sessions?limit=30").json()["sessions"]
    assert any(s["id"] == payload["id"] for s in history)


def test_import_session_works_with_relative_runs_dir(tmp_path, monkeypatch):
    """runs_dir 为相对路径（.env 默认 ./runs）时，导入不得误报"不安全路径"。

    回归保护：校验代码用 ``(root / member.filename).resolve()``（绝对路径）
    做 parents 包含判断，若 root 保持相对形式（runs/api_xxx），绝对路径的
    parents 里永远找不到相对 root，导入一律 400。真实部署（DATA_AGENT_
    RUNS_DIR=./runs）即相对路径，此测试模拟该场景。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    runs_dir = Path("runs")  # 相对路径，相对 cwd（已被 chdir 到 tmp_path）解析
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
    api.request_buckets.clear()

    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    export = client.get(f"/api/sessions/{uploaded['id']}/export")
    assert export.status_code == 200
    imported = client.post(
        "/api/sessions/import",
        files={"file": ("session.zip", export.content, "application/zip")},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["profile"]["rows"] == 2


def test_import_session_rejects_invalid_zip(tmp_path, monkeypatch):
    """POST /api/sessions/import 收到非 ZIP 内容应返回 400。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)

    response = client.post(
        "/api/sessions/import",
        files={"file": ("not_a_zip.txt", b"this is not a zip file", "text/plain")},
    )
    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]


def test_import_session_rejects_path_traversal(tmp_path, monkeypatch):
    """POST /api/sessions/import 含 ../ 路径的 ZIP 应返回 400（路径遍历防护）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("../escape.txt", "malicious")
        bundle.writestr("session.json", "{}")
    buffer.seek(0)

    response = client.post(
        "/api/sessions/import",
        files={"file": ("evil.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "不安全路径" in response.json()["detail"]


def test_import_session_rejects_oversized_archive(tmp_path, monkeypatch):
    """POST /api/sessions/import 超过 max_upload_bytes 的归档应返回 413。"""
    _isolate_runtime(tmp_path, monkeypatch)
    # 将上传上限调到很小，便于测试触发 413
    monkeypatch.setattr(api.bootstrap_settings, "max_upload_bytes", 64)
    client = TestClient(api.app)

    response = client.post(
        "/api/sessions/import",
        files={"file": ("big.zip", b"\x00" * 200, "application/zip")},
    )
    assert response.status_code == 413
    assert "归档过大" in response.json()["detail"]


def test_get_unknown_session_returns_404(tmp_path, monkeypatch):
    """GET 不存在的会话应返回 404。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.get("/api/sessions/api_nonexistent")
    assert response.status_code == 404


# --- settings 路由 ---


def test_version_endpoint(tmp_path, monkeypatch):
    """GET /api/version 应返回 API 版本与最低客户端版本。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.get("/api/version")
    assert response.status_code == 200
    payload = response.json()
    assert "version" in payload
    assert "min_client" in payload
    assert payload["version"] == api.API_VERSION


def test_auth_status_without_token(tmp_path, monkeypatch):
    """无 APP_ACCESS_TOKEN 时 GET /api/auth 应返回 required:false。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.get("/api/auth")
    assert response.status_code == 200
    assert response.json() == {"required": False, "authenticated": True}


def test_storage_health_endpoint(tmp_path, monkeypatch):
    """GET /api/storage/health 应返回存储后端健康状态。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.get("/api/storage/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "local"
    assert payload["persistent"] is False
    assert payload["status"] == "local_only"


def test_get_settings_returns_runtime_config(tmp_path, monkeypatch):
    """GET /api/settings 应返回当前运行时配置。"""
    _isolate_runtime(tmp_path, monkeypatch)
    # 屏蔽 .env 加载，防止项目根目录的 APP_ACCESS_TOKEN / DEEPSEEK_API_KEY 污染测试环境
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *a, **k: False)
    # 删除可能的 API key 环境变量，确保 configured 字段稳定
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(api.app)
    response = client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "deepseek"
    assert "model" in payload
    assert "thinking_enabled" in payload
    assert "reasoning_effort" in payload
    assert "max_upload_bytes" in payload
    assert "storage_backend" in payload


def test_update_settings_persists_thinking_and_reasoning(tmp_path, monkeypatch):
    """PUT /api/settings 应持久化 thinking_enabled 与 reasoning_effort。"""
    _isolate_runtime(tmp_path, monkeypatch)
    # 屏蔽 .env 加载，避免 _effective_settings 内部的 load_dotenv 污染 os.environ
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 预设 runtime_settings 以避免污染其他测试
    monkeypatch.setitem(api.runtime_settings, "thinking_enabled", None)
    monkeypatch.setitem(api.runtime_settings, "reasoning_effort", None)
    monkeypatch.setitem(api.runtime_settings, "api_key", "")
    client = TestClient(api.app)

    response = client.put(
        "/api/settings",
        json={"thinking_enabled": False, "reasoning_effort": "max"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["thinking_enabled"] is False
    assert payload["reasoning_effort"] == "max"

    # GET 应反映更新后的值
    refreshed = client.get("/api/settings").json()
    assert refreshed["thinking_enabled"] is False
    assert refreshed["reasoning_effort"] == "max"


def test_update_settings_rejects_empty_api_key(tmp_path, monkeypatch):
    """PUT /api/settings 传入空白 API Key 应返回 422。"""
    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setitem(api.runtime_settings, "api_key", "")
    client = TestClient(api.app)

    response = client.put(
        "/api/settings",
        json={"api_key": "   "},
    )
    assert response.status_code == 422
    assert "API Key" in response.json()["detail"]


def test_delete_api_key_clears_runtime_config(tmp_path, monkeypatch):
    """DELETE /api/settings/key 应清除运行时 API Key 并返回 configured 状态。"""
    _isolate_runtime(tmp_path, monkeypatch)
    # 屏蔽 .env 加载，避免 _effective_settings 内部的 load_dotenv 污染 os.environ
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # 先在内存中放一个 key，验证删除后 configured 变为 False
    monkeypatch.setitem(api.runtime_settings, "api_key", "sk-test-temp")
    client = TestClient(api.app)

    response = client.delete("/api/settings/key")
    assert response.status_code == 200
    payload = response.json()
    assert "configured" in payload
    # env 无 key 且内存 key 已清除，configured 应为 False
    assert payload["configured"] is False


# --- artifacts 路由 ---


def test_list_artifacts_via_session_detail(tmp_path, monkeypatch):
    """会话详情的 artifacts 字段应列出已生成的图表与数据集产物。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    # 额外导出一个 CSV 数据集产物
    record = api.registry.get(session_id)
    tools = {item.name: item for item in build_tools(record.workspace)}
    tools["export_data"].invoke({"format": "csv", "filename": "cleaned_final"})

    detail = client.get(f"/api/sessions/{session_id}").json()
    kinds = [item["kind"] for item in detail["artifacts"]]
    assert "visualization" in kinds
    assert "dataset" in kinds
    chart = _first_chart_artifact(client, session_id)
    assert chart["previewable"] is True
    assert chart["preview_url"].endswith("/preview")
    assert chart["thumbnail_url"].endswith("/thumbnail")


def test_preview_artifact_supports_etag_304(tmp_path, monkeypatch):
    """GET preview 应支持条件请求：If-None-Match 命中时返回 304。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    first = client.get(chart["preview_url"])
    assert first.status_code == 200
    etag = first.headers["etag"]

    # 带 If-None-Match 请求应返回 304
    conditional = client.get(chart["preview_url"], headers={"if-none-match": etag})
    assert conditional.status_code == 304
    assert conditional.headers["etag"] == etag


def test_preview_artifact_emits_csp_headers(tmp_path, monkeypatch):
    """GET preview 的 HTML 应内联 CSP 头并禁用外部连接。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    response = client.get(chart["preview_url"])
    assert response.status_code == 200
    assert "Content-Security-Policy" in response.text
    assert "connect-src 'none'" in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "etag" in response.headers
    assert "last-modified" in response.headers


def test_download_html_artifact_is_selfcontained(tmp_path, monkeypatch):
    """GET 下载 HTML 产物应为自包含文档（含 CSP、attachment 头）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    response = client.get(chart["download_url"])
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "Content-Security-Policy" in response.text
    # plotly bundle 应被内联，不保留相对脚本引用
    assert "<script src='plotly.min.js'" not in response.text


def test_download_non_html_artifact_returns_file(tmp_path, monkeypatch):
    """GET 下载非 HTML 产物（CSV）应以 FileResponse 返回原始文件内容。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}
    tools["export_data"].invoke({"format": "csv", "filename": "cleaned_final"})

    dataset = next(
        item for item in client.get(f"/api/sessions/{uploaded['id']}").json()["artifacts"]
        if item["kind"] == "dataset"
    )
    response = client.get(dataset["download_url"])
    assert response.status_code == 200
    assert "region" in response.content.decode("utf-8", errors="ignore")


def test_thumbnail_returns_cached_png(tmp_path, monkeypatch):
    """GET thumbnail 在缓存命中时应直接返回已有的 PNG。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    # 手动放置一个缩略图 PNG（带 PNG 签名），模拟已缓存的产物
    record = api.registry.get(session_id)
    chart_name = chart["name"]
    stem = chart_name[: -len(".html")] if chart_name.endswith(".html") else chart_name
    thumb_path = record.workspace.artifacts_dir / f"{stem}_thumb.png"
    png_signature = b"\x89PNG\r\n\x1a\n"
    thumb_path.write_bytes(png_signature + b"\x00" * 32)

    response = client.get(chart["thumbnail_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(png_signature)


def test_thumbnail_returns_404_for_missing_chart(tmp_path, monkeypatch):
    """GET thumbnail 对不存在的图表应返回 404。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)

    response = client.get(f"/api/sessions/{uploaded['id']}/artifacts/nonexistent.html/thumbnail")
    assert response.status_code == 404
    assert "图表数据文件不存在" in response.json()["detail"]


def test_edit_chart_regenerates_html(tmp_path, monkeypatch):
    """PUT edit 应基于 .plotly.json 重新生成 HTML 并更新标题。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"title": "新标题"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    # 预览应反映新标题（<title> 标签中含新标题文本）
    preview = client.get(chart["preview_url"])
    assert preview.status_code == 200
    assert "新标题" in preview.text

    # 产物描述必须同步：卡片/模态标题读 description，只改 HTML 会让
    # UI 停留在旧标题；且应持久化到 manifest，重启后不回退。
    session = client.get(f"/api/sessions/{session_id}").json()
    edited = next(a for a in session["artifacts"] if a["name"] == chart["name"])
    assert edited["description"] == "新标题"
    manifest = api.registry._manifest_path(api.registry.get(session_id))
    assert json.loads(manifest.read_text(encoding="utf-8"))["artifacts"][0]["description"] == "新标题"


def test_edit_chart_description_sync_survives_persist_failure(tmp_path, monkeypatch):
    """持久化 manifest 失败不应影响图表编辑结果（描述同步仍生效）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(api.registry, "_persist_locked", _boom)
    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"title": "持久化失败也更新"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # 内存中的 description 已同步（本次编辑结果有效）
    session = client.get(f"/api/sessions/{session_id}").json()
    edited = next(a for a in session["artifacts"] if a["name"] == chart["name"])
    assert edited["description"] == "持久化失败也更新"


def test_edit_chart_returns_409_when_run_lock_held(tmp_path, monkeypatch):
    """PUT edit 在 run_lock 持有时应返回 409。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    record = api.registry.get(session_id)
    record.run_lock.acquire()
    try:
        response = client.put(
            f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
            json={"title": "x"},
        )
        assert response.status_code == 409
        assert "运行" in response.json()["detail"]
    finally:
        record.run_lock.release()


def test_artifact_endpoints_return_404_for_missing_file(tmp_path, monkeypatch):
    """GET 预览/下载不存在的产物应返回 404。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)

    preview = client.get(f"/api/sessions/{uploaded['id']}/artifacts/missing.html/preview")
    assert preview.status_code == 404
    download = client.get(f"/api/sessions/{uploaded['id']}/artifacts/missing.html")
    assert download.status_code == 404


# --- 补充覆盖：settings / artifacts 额外分支 ---


def test_auth_status_authenticated_with_token(monkeypatch):
    """携带正确 token 时 GET /api/auth 应返回 authenticated:True。"""
    monkeypatch.setenv("APP_ACCESS_TOKEN", "test-access-token")
    client = TestClient(api.app)
    response = client.get("/api/auth", headers={"X-App-Token": "test-access-token"})
    assert response.status_code == 200
    assert response.json() == {"required": True, "authenticated": True}


def test_preview_non_html_returns_415(tmp_path, monkeypatch):
    """GET preview 非 HTML 产物应返回 415。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}
    tools["export_data"].invoke({"format": "csv", "filename": "cleaned_final"})
    dataset = next(
        item for item in client.get(f"/api/sessions/{uploaded['id']}").json()["artifacts"]
        if item["kind"] == "dataset"
    )
    response = client.get(f"/api/sessions/{uploaded['id']}/artifacts/{dataset['name']}/preview")
    assert response.status_code == 415


def test_edit_chart_returns_404_for_missing_chart(tmp_path, monkeypatch):
    """PUT edit 不存在的图表应返回 404。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    response = client.put(
        f"/api/sessions/{uploaded['id']}/artifacts/nonexistent.html/edit",
        json={"title": "x"},
    )
    assert response.status_code == 404


def test_edit_chart_applies_color(tmp_path, monkeypatch):
    """PUT edit 应支持修改配色（color 字段应用到所有 trace）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"color": "#245C55"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_export_returns_html(tmp_path, monkeypatch):
    """GET /api/sessions/{id}/dashboard 应返回数据画像仪表盘 HTML。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    response = client.get(f"/api/sessions/{uploaded['id']}/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]


def test_preview_handles_non_utf8_html(tmp_path, monkeypatch):
    """GET preview 应能容错读取非 UTF-8（GB18030）编码的 HTML 产物。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    record = api.registry.get(uploaded["id"])
    # 手动放置一个 GBK 编码的 HTML 产物，覆盖 _read_utf8_robust 的回退分支
    html_path = record.workspace.artifacts_dir / "gbk_chart.html"
    html_path.write_bytes("<html><head></head><body>中文图表</body></html>".encode("gb18030"))
    record.workspace.register_artifact(html_path, "visualization", "GBK 图表")
    response = client.get(f"/api/sessions/{uploaded['id']}/artifacts/gbk_chart.html/preview")
    assert response.status_code == 200
    assert "中文图表" in response.text


@pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="Kaleido 无头渲染在 CI 上偶发挂起且无法被信号超时中断（本地已验证）",
)
def test_thumbnail_renders_png_when_no_cache(tmp_path, monkeypatch):
    """GET thumbnail 无缓存时应从 .plotly.json 渲染 PNG（kaleido）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    # 不预置缓存缩略图，强制走 kaleido 渲染分支
    response = client.get(chart["thumbnail_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_thumbnail_returns_503_when_render_import_fails(tmp_path, monkeypatch):
    """GET thumbnail 在 kaleido 渲染器不可用（ImportError）时应返回 503。"""
    import plotly.graph_objects as go

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    # write_image 内部会尝试加载 kaleido：模拟渲染器缺失触发 ImportError -> 503
    def _raise_import_error(self, *args, **kwargs):
        raise ImportError("模拟 kaleido 未安装")

    monkeypatch.setattr(go.Figure, "write_image", _raise_import_error)
    response = client.get(chart["thumbnail_url"])
    assert response.status_code == 503
    assert "kaleido" in response.json()["detail"]


def test_thumbnail_returns_500_on_render_failure(tmp_path, monkeypatch):
    """GET thumbnail 在渲染抛非 ImportError 异常时应返回 500。"""
    import plotly.graph_objects as go

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    def _raise_runtime_error(self, *args, **kwargs):
        raise RuntimeError("渲染引擎内部错误")

    monkeypatch.setattr(go.Figure, "write_image", _raise_runtime_error)
    response = client.get(chart["thumbnail_url"])
    assert response.status_code == 500
    assert "缩略图生成失败" in response.json()["detail"]


def test_create_session_rejects_empty_file(tmp_path, monkeypatch):
    """POST /api/sessions 上传空文件应返回 422。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.post(
        "/api/sessions",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert response.status_code == 422
    assert "为空" in response.json()["detail"]


def test_create_session_returns_500_on_unexpected_exception(tmp_path, monkeypatch):
    """POST /api/sessions 非 ValueError/OSError 的异常应返回 500（不暴露细节）。"""
    from data_agent.workspace import DataWorkspace

    _isolate_runtime(tmp_path, monkeypatch)

    # 模拟 load 阶段抛出非 ValueError/OSError 的异常（如 pyarrow.ArrowInvalid
    # 在某些版本下不继承 ValueError），覆盖通用 500 兜底分支
    def failing_load(self, path):
        raise RuntimeError("意外的内部解析错误")

    monkeypatch.setattr(DataWorkspace, "load", failing_load)
    client = TestClient(api.app)
    response = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\n", "text/csv")},
    )
    assert response.status_code == 500
    assert "解析失败" in response.json()["detail"]


def test_create_sample_session_handles_init_failure(tmp_path, monkeypatch):
    """POST /api/sessions/sample 示例数据初始化失败应返回 500。"""
    from data_agent.workspace import DataWorkspace

    _isolate_runtime(tmp_path, monkeypatch)

    def failing_save(self, name, stream, max_bytes):
        raise RuntimeError("模拟磁盘故障")

    monkeypatch.setattr(DataWorkspace, "save_upload_stream", failing_save)
    client = TestClient(api.app)
    response = client.post("/api/sessions/sample")
    assert response.status_code == 500
    assert "示例数据" in response.json()["detail"]


def test_import_session_rejects_invalid_manifest(tmp_path, monkeypatch):
    """POST /api/sessions/import 有效 ZIP 但 manifest 缺失应返回 400。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        # 只写一个无关文件，没有 session.json 也没有 input/
        bundle.writestr("random.txt", "no manifest here")
    buffer.seek(0)

    response = client.post(
        "/api/sessions/import",
        files={"file": ("no_manifest.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "无效" in response.json()["detail"]


def test_update_settings_persist_key_keyring_unavailable(tmp_path, monkeypatch):
    """PUT /api/settings persist_key=True 但 keyring 不可用时应返回 warning。"""
    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 模拟系统凭据存储不可用：save_api_key 返回 False
    monkeypatch.setattr("data_agent.registry.save_api_key", lambda value: False)
    monkeypatch.setitem(api.runtime_settings, "api_key", "")
    client = TestClient(api.app)

    response = client.put(
        "/api/settings",
        json={"api_key": "sk-test-persist", "persist_key": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert "warning" in payload
    assert "凭据存储不可用" in payload["warning"]


def test_harden_preview_document_fallback_branches():
    """_harden_preview_document 应处理缺 head、含 html 标签、body 片段三种情况。"""
    from data_agent.routers.artifacts import _harden_preview_document

    # 1. 含 <head>：在 head 后注入 meta，不新增 head 标签
    with_head = "<html><head><title>x</title></head><body></body></html>"
    result = _harden_preview_document(with_head)
    assert "Content-Security-Policy" in result
    assert result.count("<head>") == 1

    # 2. 含 <html> 但无 <head>：注入 <head>meta</head>
    no_head = "<html><body>chart</body></html>"
    result = _harden_preview_document(no_head)
    assert "<head>" in result
    assert "Content-Security-Policy" in result

    # 3. body 片段：包一层完整文档
    fragment = "<div>plot</div>"
    result = _harden_preview_document(fragment)
    assert result.startswith("<!doctype html>")
    assert "Content-Security-Policy" in result
    assert "<div>plot</div>" in result

    # 4. 已带 doctype 的片段：不重复声明 doctype
    doctype_frag = "<!doctype html><div>plot</div>"
    result = _harden_preview_document(doctype_frag)
    assert result.count("<!doctype") == 1


def test_repair_unterminated_plotly_script_fixes_legacy_bug():
    """旧版生成器把 to_html 脚本块的闭合 </script> 也转义成 <\\/script>，
    导致 script 无法闭合、预览空白。修复函数应还原最后一个 <\\/script>，
    且不触碰数据中真正的转义。"""
    from data_agent.routers.artifacts import _repair_unterminated_plotly_script

    # 1. 旧版坏文件：3 个开标签、2 个闭标签（to_html 闭合被转义）
    broken = (
        "<script src='plotly.min.js'></script>"
        "<script>window.PLOTLYENV={};Plotly.newPlot('g',{},{});<\\/script>"
        "<div class='plotly-interpretation'>解读</div>"
        "<script>(function(){})();</script>"
    )
    assert broken.count("<script") == 3 and broken.count("</script>") == 2
    repaired = _repair_unterminated_plotly_script(broken)
    assert repaired.count("<script") == repaired.count("</script>") == 3
    # 被还原的闭合标签位于 newPlot 之后、暗色脚本之前
    newplot_idx = repaired.find("Plotly.newPlot")
    assert repaired.find("</script>", newplot_idx) < repaired.find("<script>", newplot_idx)

    # 2. 数据本身也含 </script>（已转义）：只还原最后一个（闭合标签），
    #    数据中的转义必须原样保留，不重新引入 XSS。
    broken_with_data = (
        "<script src='plotly.min.js'></script>"
        "<script>var x='<\\/script><script>alert(1)<\\/script>';"
        "Plotly.newPlot('g',{},{});<\\/script>"
        "<script>(function(){})();</script>"
    )
    repaired2 = _repair_unterminated_plotly_script(broken_with_data)
    # bundle 1 个 + 还原的 to_html 闭合 1 个 + 暗色脚本 1 个
    assert repaired2.count("</script>") == 3
    # 数据中的两个 </script> 转义必须原样保留（未被还原）
    assert repaired2.count("<\\/script>") == 2
    # 还原的闭合标签在 newPlot 之后、暗色脚本之前
    newplot_idx = repaired2.find("Plotly.newPlot")
    tail2 = repaired2[newplot_idx:]
    assert tail2.find("</script>") != -1 and (
        tail2.find("<script") == -1 or tail2.find("</script>") < tail2.find("<script")
    )

    # 3. 结构正确的文件（开闭数量相等）：原样返回
    healthy = (
        "<script src='plotly.min.js'></script>"
        "<script>window.PLOTLYENV={};Plotly.newPlot('g',{},{});</script>"
        "<script>(function(){})();</script>"
    )
    assert _repair_unterminated_plotly_script(healthy) == healthy

    # 4. 含数据转义但结构正确的文件（如 ECharts option JSON）：原样返回
    healthy_with_escaped = (
        "<script>echarts.init();var o='<\\/script>';</script>"
        "<script>(function(){})();</script>"
    )
    assert _repair_unterminated_plotly_script(healthy_with_escaped) == healthy_with_escaped

    # 5. 含转义但之后没有原始闭合标签的异常片段：原样返回，不做猜测性修改
    odd_fragment = "<script>var x='<\\/script>';"
    assert _repair_unterminated_plotly_script(odd_fragment) == odd_fragment


def test_repair_legacy_plotly_theme_keys_removes_layout_prefix():
    """旧版暗色脚本的 relayout 键带 'layout.' 前缀，Plotly v3 会静默忽略，
    导致深色主题下图表保持浅色。修复应替换为合法的根路径键且幂等。"""
    from data_agent.routers.artifacts import _repair_legacy_plotly_theme_keys

    legacy = (
        "<script>"
        "var update = {"
        "'layout.paper_bgcolor': '#1c2433',"
        "'layout.plot_bgcolor': '#1c2433',"
        "'layout.font.color': '#e6eaf0',"
        "'layout.xaxis.gridcolor': '#2a3445',"
        "'layout.yaxis.zerolinecolor': '#3a4458'"
        "};"
        "Plotly.relayout(plotEl, update);"
        "</script>"
    )
    repaired = _repair_legacy_plotly_theme_keys(legacy)
    assert "'layout.paper_bgcolor'" not in repaired
    assert "'layout.plot_bgcolor'" not in repaired
    assert "'layout.font.color'" not in repaired
    assert "'layout.xaxis.gridcolor'" not in repaired
    assert "'layout.yaxis.zerolinecolor'" not in repaired
    assert "'paper_bgcolor'" in repaired
    assert "'font.color'" in repaired
    assert "'xaxis.gridcolor'" in repaired
    assert "'yaxis.zerolinecolor'" in repaired

    # 幂等：修复后再修一次结果不变
    assert _repair_legacy_plotly_theme_keys(repaired) == repaired

    # 新版文件（无 'layout.' 前缀键）原样返回
    modern = "<script>var update = {'paper_bgcolor': '#1c2433'};</script>"
    assert _repair_legacy_plotly_theme_keys(modern) == modern


def test_inject_legend_anchor_fix_is_idempotent_and_scoped():
    """历史 Plotly 图表注入图例锚定修正脚本：幂等、只作用于 Plotly 文档。"""
    from data_agent.routers.artifacts import _inject_legend_anchor_fix

    # 1. Plotly 文档：注入脚本
    plotly_html = "<html><head><title>t</title></head><body><div class='plotly-graph-div'></div></body></html>"
    injected = _inject_legend_anchor_fix(plotly_html)
    assert "legend-anchor-fix" in injected
    assert "legend.orientation" in injected
    # 注入位置在 <head> 之后
    head_end = injected.find("</head>")
    assert "legend-anchor-fix" in injected[:head_end]

    # 2. 幂等：二次注入不重复
    assert _inject_legend_anchor_fix(injected) == injected

    # 3. 非 Plotly 文档（ECharts）：跳过
    echarts_html = "<html><head></head><body><div id='chart'></div></body></html>"
    assert _inject_legend_anchor_fix(echarts_html) == echarts_html

    # 4. 无 <head> 的文档：原样返回（不注入到错误位置）
    no_head = "<html><body><div class='plotly-graph-div'></div></body></html>"
    assert _inject_legend_anchor_fix(no_head) == no_head


def test_inject_modebar_i18n_is_idempotent_and_scoped():
    """modebar 按钮提示中文本地化脚本：幂等、只作用于 Plotly 文档。"""
    from data_agent.routers.artifacts import _inject_modebar_i18n

    plotly_html = "<html><head><title>t</title></head><body><div class='plotly-graph-div'></div></body></html>"
    injected = _inject_modebar_i18n(plotly_html)
    assert "modebar-i18n" in injected
    assert "下载为 PNG 图片" in injected
    # 幂等
    assert _inject_modebar_i18n(injected) == injected
    # 非 Plotly 文档跳过
    echarts_html = "<html><head></head><body><div id='chart'></div></body></html>"
    assert _inject_modebar_i18n(echarts_html) == echarts_html
    # 无 <head> 的 Plotly 文档：原样返回
    no_head = "<html><body><div class='plotly-graph-div'></div></body></html>"
    assert _inject_modebar_i18n(no_head) == no_head


def test_echarts_json_endpoint_returns_option(tmp_path, monkeypatch):
    """GET /artifacts/{name}/echarts-json 应返回 ECharts option JSON。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    # 手工放置 echarts.json 模拟 ECharts 产物
    record = api.registry.get(session_id)
    stem = "柱状图_1"
    (record.workspace.artifacts_dir / f"{stem}.echarts.json").write_text(
        json.dumps({"series": [{"type": "bar", "data": [1, 2, 3]}], "xAxis": {"data": ["A", "B", "C"]}}),
        encoding="utf-8",
    )
    response = client.get(f"/api/sessions/{session_id}/artifacts/{stem}.html/echarts-json")
    assert response.status_code == 200
    assert response.json()["series"][0]["type"] == "bar"
    assert response.headers["content-type"].startswith("application/json")


def test_echarts_json_endpoint_500_on_corrupt_file(tmp_path, monkeypatch):
    """损坏的 .echarts.json（非法 JSON）应返回 500。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    record = api.registry.get(session_id)
    (record.workspace.artifacts_dir / "柱状图_1.echarts.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/柱状图_1.html/echarts-json"
    )
    assert response.status_code == 500


def test_echarts_json_endpoint_404_without_data_file(tmp_path, monkeypatch):
    """无 .echarts.json 的图表（如 Plotly 图）应返回 404，前端据此回退占位。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/echarts-json"
    )
    assert response.status_code == 404


def test_echarts_json_endpoint_rejects_path_traversal(tmp_path, monkeypatch):
    """文件名中的路径穿越应被拒绝（基名校验）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/..%2F..%2Fetc%2Fpasswd/echarts-json"
    )
    assert response.status_code in (404, 422, 400)


def test_plotly_json_endpoint_returns_figure(tmp_path, monkeypatch):
    """GET /artifacts/{name}/plotly-json 应返回 Plotly figure JSON。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    # 手工放置 plotly.json 模拟 Plotly 产物
    record = api.registry.get(session_id)
    stem = "柱状图_1"
    (record.workspace.artifacts_dir / f"{stem}.plotly.json").write_text(
        json.dumps(
            {"data": [{"type": "scatter", "x": [1, 2, 3], "y": [2, 3, 4]}],
             "layout": {"title": {"text": "sales_by_region"}}}
        ),
        encoding="utf-8",
    )
    response = client.get(f"/api/sessions/{session_id}/artifacts/{stem}.html/plotly-json")
    assert response.status_code == 200
    assert response.json()["data"][0]["type"] == "scatter"
    assert response.headers["content-type"].startswith("application/json")


def test_plotly_json_endpoint_500_on_corrupt_file(tmp_path, monkeypatch):
    """损坏的 .plotly.json（非法 JSON）应返回 500。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    record = api.registry.get(session_id)
    (record.workspace.artifacts_dir / "柱状图_1.plotly.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/柱状图_1.html/plotly-json"
    )
    assert response.status_code == 500


def test_plotly_json_endpoint_404_without_data_file(tmp_path, monkeypatch):
    """无 .plotly.json 的图表（如 ECharts 图）应返回 404，前端据此回退 PNG。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    record = api.registry.get(uploaded["id"])
    tools = {item.name: item for item in build_tools(record.workspace)}
    tools["create_visualization"].invoke(
        {"chart_type": "bar", "x": "region", "y": "sales",
         "title": "r", "chart_engine": "echarts"}
    )
    chart = _first_chart_artifact(client, uploaded["id"])
    response = client.get(
        f"/api/sessions/{uploaded['id']}/artifacts/{chart['name']}/plotly-json"
    )
    assert response.status_code == 404


def test_plotly_json_endpoint_rejects_path_traversal(tmp_path, monkeypatch):
    """文件名中的路径穿越应被拒绝（基名校验）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/..%2F..%2Fetc%2Fpasswd/plotly-json"
    )
    assert response.status_code in (404, 422, 400)


def test_plotly_json_endpoint_decodes_typed_arrays(tmp_path, monkeypatch):
    """plotly.py 把 numpy 数组写成 {dtype, bdata} typed-array，端点必须
    先解码为标准列表再抽样，否则按点数据无法统计与等距抽样。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    record = api.registry.get(session_id)

    import numpy as np
    import plotly.graph_objects as go

    arr = np.linspace(0, 1_000_000, 50_000, dtype="float32")
    fig = go.Figure(go.Scatter(x=arr, y=arr))
    (record.workspace.artifacts_dir / "柱状图_1.plotly.json").write_text(
        fig.to_json(), encoding="utf-8"
    )
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/柱状图_1.html/plotly-json"
    )
    assert response.status_code == 200
    trace = response.json()["data"][0]
    assert isinstance(trace["x"], list)      # 已解码为普通数组
    assert len(trace["x"]) == 2500           # 且已抽样
    assert abs(trace["x"][0]) < 1e-3         # 数值正确（从 0 起）
    assert isinstance(trace["y"], list)
    assert len(trace["y"]) == 2500


# === 迷你图大数据兜底：散点按等距抽样到 _THUMB_MAX_POINTS ===


def test_echarts_json_supports_big_datasets_by_sampling(tmp_path, monkeypatch):
    """30 万行散点的 option 十几 MB，端点必须抽样后再返回，且不破坏
    热力图/柱状图等非散点系列。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    record = api.registry.get(session_id)
    (record.workspace.artifacts_dir / "柱状图_1.echarts.json").write_text(
        json.dumps({
            "series": [
                {"type": "scatter", "data": [[i, i * 2] for i in range(300_000)]},
                {"type": "heatmap", "data": [[0, 0, i] for i in range(60)]},
                {"type": "bar", "data": [1] * 3000},
            ],
        }),
        encoding="utf-8",
    )
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/柱状图_1.html/echarts-json"
    )
    assert response.status_code == 200
    option = response.json()
    series = option["series"]
    assert len(series[0]["data"]) == 2500
    assert series[0]["data"][0] == [0, 0]          # 等距保留首端
    assert len(series[1]["data"]) == 60            # 热力图不抽样（缺格破图）
    assert len(series[2]["data"]) == 3000          # bar 不抽样


def test_plotly_json_supports_big_datasets_by_sampling(tmp_path, monkeypatch):
    """Plotly 散点/箱线的按点数组按同一下标规则抽样（x/y/颜色不错位），
    矩阵型（heatmap z）保持不变。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    record = api.registry.get(session_id)
    (record.workspace.artifacts_dir / "柱状图_1.plotly.json").write_text(
        json.dumps({
            "data": [
                {"type": "scatter", "x": list(range(5000)), "y": list(range(5000)),
                 "marker": {"color": ["#4E79A7"] * 5000}},
                {"type": "box", "x": ["A"] * 8000, "y": list(range(8000))},
                {"type": "heatmap", "z": [[1.0] * 30 for _ in range(30)]},
            ],
        }),
        encoding="utf-8",
    )
    response = client.get(
        f"/api/sessions/{session_id}/artifacts/柱状图_1.html/plotly-json"
    )
    assert response.status_code == 200
    traces = response.json()["data"]
    assert len(traces[0]["x"]) == 2500
    assert len(traces[0]["y"]) == 2500
    assert len(traces[0]["marker"]["color"]) == 2500
    assert traces[0]["x"][:3] == [0, 2, 4]         # 下标同步（等距）
    assert len(traces[1]["y"]) == 2000             # 8000 点 → step=4 → 2000
    assert len(traces[1]["x"]) == 2000
    assert len(traces[2]["z"]) == 30               # 矩阵型不动


def test_thumb_sampling_helpers_edge_cases():
    """抽样助手对非散点/非按点结构必须原样放行。"""
    from data_agent.routers.artifacts import (
        _decode_plotly_typed_arrays,
        _sample_echarts_option_for_thumb,
        _sample_plotly_figure_for_thumb,
    )

    opt = {"series": [{"type": "scatter", "data": [1, 2, 3]}, "junk"]}
    _sample_echarts_option_for_thumb(opt)
    assert opt["series"][0]["data"] == [1, 2, 3]   # 小数据不动
    _sample_echarts_option_for_thumb({})            # 无 series
    _sample_echarts_option_for_thumb({"series": None})
    _sample_echarts_option_for_thumb("not-a-dict")  # type: ignore[arg-type]

    fig = {"data": [{"type": "scatter", "x": [1, 2], "y": [3]}, "junk"]}
    _sample_plotly_figure_for_thumb(fig)
    assert fig["data"][0]["x"] == [1, 2]           # 未超上限不动
    _sample_plotly_figure_for_thumb({})
    _sample_plotly_figure_for_thumb({"data": None})
    _sample_plotly_figure_for_thumb("not-a-dict")  # type: ignore[arg-type]

    # typed-array 解码：合法 i4 数组 → [-1]；非法 dtype 原样返回（不崩溃）
    assert _decode_plotly_typed_arrays({"dtype": "i4", "bdata": "/////w=="}) == [-1]
    bad = {"dtype": "bogus", "bdata": "AAAA"}
    assert _decode_plotly_typed_arrays(bad) == bad
    # 非 typed-array 递归穿过
    assert _decode_plotly_typed_arrays({"a": [1, {"b": [2, 3]}]}) == {"a": [1, {"b": [2, 3]}]}
    assert _decode_plotly_typed_arrays("plain") == "plain"


def test_dashboard_returns_404_when_no_data_loaded(tmp_path, monkeypatch):
    """GET /api/sessions/{id}/dashboard 在未加载数据集时应返回 404。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client)
    # 清空工作区 dataframe 模拟未加载状态，触发 build_dashboard_html 抛 RuntimeError
    record = api.registry.get(uploaded["id"])
    record.workspace._df = None

    response = client.get(f"/api/sessions/{uploaded['id']}/dashboard")
    assert response.status_code == 404
    assert "尚未加载数据集" in response.json()["detail"]


def test_edit_chart_returns_500_on_corrupt_plotly_json(tmp_path, monkeypatch):
    """PUT edit 在 .plotly.json 损坏时应返回 500。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    record = api.registry.get(session_id)
    chart_name = chart["name"]
    stem = chart_name[: -len(".html")] if chart_name.endswith(".html") else chart_name
    json_path = record.workspace.artifacts_dir / f"{stem}.plotly.json"
    # 写入非法 JSON 触发 ValueError 分支
    json_path.write_text("{not valid json", encoding="utf-8")

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"title": "x"},
    )
    assert response.status_code == 500
    assert "图表数据读取失败" in response.json()["detail"]


def test_edit_chart_color_applies_to_trace_without_marker(tmp_path, monkeypatch):
    """PUT edit 对无 marker 的 trace 应自动创建 marker.color。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    record = api.registry.get(session_id)
    chart_name = chart["name"]
    stem = chart_name[: -len(".html")] if chart_name.endswith(".html") else chart_name
    json_path = record.workspace.artifacts_dir / f"{stem}.plotly.json"
    # 改造 .plotly.json：移除 trace 的 marker 字段，覆盖无 marker 的 else 分支
    fig_dict = json.loads(json_path.read_text(encoding="utf-8"))
    for trace in fig_dict.get("data", []):
        trace.pop("marker", None)
    json_path.write_text(json.dumps(fig_dict), encoding="utf-8")

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"color": "#123456"},
    )
    assert response.status_code == 200
    # 验证 marker 已被创建并写入期望颜色
    updated = json.loads(json_path.read_text(encoding="utf-8"))
    for trace in updated.get("data", []):
        assert trace.get("marker", {}).get("color") == "#123456"


def test_edit_chart_falls_back_to_inline_plotlyjs(tmp_path, monkeypatch):
    """PUT edit 在 ensure_plotly_bundle 返回 None 时应回退到 fig.write_html。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)
    record = api.registry.get(session_id)
    # monkeypatch ensure_plotly_bundle 返回 None，触发 write_html 回退分支
    monkeypatch.setattr(record.workspace, "ensure_plotly_bundle", lambda: None)

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"title": "回退标题"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # 预览应含新标题（write_html 回退路径生成的完整 HTML）
    preview = client.get(chart["preview_url"])
    assert preview.status_code == 200


# ---------------------------------------------------------------------------
# C1 修复测试：SSE 首帧断开后锁释放
# ---------------------------------------------------------------------------


def test_analyze_stream_releases_lock_on_first_frame_disconnect(tmp_path, monkeypatch):
    """SSE 首帧 started 后客户端断开，run_lock 和 analysis_slots 必须被释放。

    C1 修复场景：客户端在 worker.start() 执行前断开连接，generate() 的 finally
    块检测到 worker_started=False，手动释放 run_lock 和 analysis_slots。
    若此修复缺失，锁将永久泄漏，max_concurrent_analyses=2 时泄漏 2 次后
    整个服务无法启动新分析。使用 TestClient stream 模式模拟 SSE 首帧后断开。
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    # 快速完成式 Agent：stream() 立即返回 finalize，worker 的 finally 会释放锁。
    # 即使客户端在首帧后断开，worker 也已快速完成或被 C1 修复路径兜底释放。
    class FastAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            yield {"node": "finalize", "data": {
                "response": "done",
                "trace": [],
                "artifacts": list(self.workspace.artifacts),
                "dataset_profile": self.workspace.profile(),
                "plan": [],
                "completed_steps": [],
            }}

        def run(self, query, history=None, resume_from=None):
            return AnalysisResult(
                response="done", trace=[], artifacts=[],
                dataset_profile=self.workspace.profile(), plan=[], completed_steps=[],
            )

    monkeypatch.setattr(api, "DataAnalysisAgent", FastAgent)

    # 发起流式分析，收到首帧 started 后立即断开（不读取后续事件）
    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/analyze/stream",
        json={"task": "检查数据"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event: started"):
                break  # 首帧后断开

    # 验证 run_lock 已释放（可以成功 acquire）
    record = api.registry.get(session_id)
    assert record.run_lock.acquire(blocking=False), "run_lock 未释放——C1 修复路径未生效"
    record.run_lock.release()
    # 验证 analysis_slots 已释放（可以成功 acquire）
    assert api.analysis_slots.acquire(blocking=False), "analysis_slots 未释放——C1 修复路径未生效"
    api.analysis_slots.release()


def test_chat_stream_releases_lock_on_first_frame_disconnect(tmp_path, monkeypatch):
    """chat_stream 首帧 started 后客户端断开，run_lock 必须被释放。

    chat_stream 的 C1 修复与 analyze_stream 类似：客户端在 worker.start() 前
    断开时，generate() 的 finally 块手动释放 run_lock。chat_stream 不占用
    analysis_slots，因此只需验证 run_lock。
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    # 预设已完成的首轮分析，使追问不被 409 拦截
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [
        {"role": "user", "content": "检查数据"},
        {"role": "assistant", "content": "分析完成。"},
    ]
    api.registry.persist(session_id, record)

    # 快速完成式追问 Agent
    class FastChatAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self._last_usage = None
            self._last_reasoning = ""

        def chat(self, query, history=None):
            return "回答内容", []

    monkeypatch.setattr(api, "DataAnalysisAgent", FastChatAgent)

    # 发起追问流，收到首帧 started 后立即断开
    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/chat/stream",
        json={"task": "追问详情"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event: started"):
                break  # 首帧后断开

    # 验证 run_lock 已释放
    record = api.registry.get(session_id)
    assert record.run_lock.acquire(blocking=False), "run_lock 未释放——chat_stream C1 修复路径未生效"
    record.run_lock.release()


def test_lock_release_allows_subsequent_analysis_after_disconnect(tmp_path, monkeypatch):
    """锁释放后可以再次发起分析，不返回 409。

    验证 C1 修复的端到端效果：首次流式分析首帧断开后锁被释放，
    第二次同步分析应正常执行而非被 409 拦截。
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    # 首次分析用的快速 Agent
    class FirstFastAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            yield {"node": "finalize", "data": {
                "response": "done",
                "trace": [],
                "artifacts": list(self.workspace.artifacts),
                "dataset_profile": self.workspace.profile(),
                "plan": [],
                "completed_steps": [],
            }}

        def run(self, query, history=None, resume_from=None):
            return AnalysisResult(
                response="done", trace=[], artifacts=[],
                dataset_profile=self.workspace.profile(), plan=[], completed_steps=[],
            )

    monkeypatch.setattr(api, "DataAnalysisAgent", FirstFastAgent)

    # 第一次：发起流式分析并在首帧后断开
    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/analyze/stream",
        json={"task": "第一次分析"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event: started"):
                break

    # 确认锁已释放
    record = api.registry.get(session_id)
    assert record.run_lock.acquire(blocking=False), "首次断开后 run_lock 未释放"
    record.run_lock.release()

    # 第二次：用新的 FastAgent 发起同步分析，验证不返回 409
    class SecondFastAgent:
        def __init__(self, workspace, settings, cancel_event=None, **kwargs):
            self.workspace = workspace

        def run(self, query, history=None, resume_from=None):
            return AnalysisResult(
                response="第二次分析完成",
                trace=[],
                artifacts=[],
                dataset_profile=self.workspace.profile(),
                plan=[],
                completed_steps=[],
            )

    monkeypatch.setattr(api, "DataAnalysisAgent", SecondFastAgent)

    response = client.post(
        f"/api/sessions/{session_id}/analyze",
        json={"task": "第二次分析"},
    )
    assert response.status_code == 200, (
        f"锁未正确释放，第二次分析被拦截：{response.status_code} {response.text}"
    )
    assert response.json()["response"] == "第二次分析完成"


def test_analyze_stream_c1_fix_releases_lock_when_worker_start_fails(tmp_path, monkeypatch):
    """worker.start() 失败时 C1 修复路径释放 run_lock 和 analysis_slots。

    直接测试 C1 修复代码路径：monkeypatch threading.Thread.start 使 analysis
    worker 线程启动失败，generate() 的 finally 块检测到 worker_started=False
    并手动释放锁。这是对 C1 修复逻辑的确定性测试，不依赖断开时序。
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]

    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )

    class DummyAgent:
        def __init__(self, workspace, settings, cancel_event=None, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "finalize", "data": {
                "response": "done", "trace": [], "artifacts": [],
                "dataset_profile": {}, "plan": [], "completed_steps": [],
            }}

    monkeypatch.setattr(api, "DataAnalysisAgent", DummyAgent)

    # 使 analysis worker 线程启动失败，触发 worker_started=False 的 C1 修复路径
    original_start = threading.Thread.start

    def failing_start(self):
        if self.name.startswith("analysis-"):
            raise RuntimeError("模拟线程启动失败")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    # 发起流式分析：首帧 started 正常发送，worker.start() 抛异常后
    # generate() 的 finally 块走 C1 修复路径释放锁
    try:
        with client.stream(
            "POST",
            f"/api/sessions/{session_id}/analyze/stream",
            json={"task": "检查数据"},
        ) as response:
            for line in response.iter_lines():
                if line.startswith("event: started"):
                    break
    except Exception:
        # 流式响应可能因异常中断，关键是验证锁是否释放
        pass

    # 验证 C1 修复路径已释放 run_lock
    record = api.registry.get(session_id)
    assert record.run_lock.acquire(blocking=False), "run_lock 未释放——C1 修复路径未生效"
    record.run_lock.release()
    # 验证 C1 修复路径已释放 analysis_slots
    assert api.analysis_slots.acquire(blocking=False), "analysis_slots 未释放——C1 修复路径未生效"
    api.analysis_slots.release()


# ---------------------------------------------------------------------------
# 流式端点剩余分支：event_callback 透传 / persist 失败降级 / CancelledError 路径
# ---------------------------------------------------------------------------


def _finalize_data(workspace):
    return {
        "response": "done",
        "trace": [],
        "artifacts": list(workspace.artifacts),
        "dataset_profile": workspace.profile(),
        "plan": [],
        "completed_steps": [],
    }


def test_analyze_stream_forwards_event_callback_events(tmp_path, monkeypatch):
    """agent.event_callback 推送的细粒度事件（tool_call 等）应透传到 SSE。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class EmittingAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self.event_callback = event_callback

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            if self.event_callback:
                self.event_callback("tool_call", {"name": "inspect_data", "detail": "{}"})
                self.event_callback("report_chunk", {"chunk": "报告片段"})
            yield {"node": "finalize", "data": _finalize_data(self.workspace)}

    monkeypatch.setattr(api, "DataAnalysisAgent", EmittingAgent)
    with client.stream(
        "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: tool_call" in text
    assert "inspect_data" in text
    assert "event: report_chunk" in text


def test_analyze_stream_complete_still_emitted_when_persist_fails(tmp_path, monkeypatch, caplog):
    """complete 分支的 persist 失败应记录日志且不影响 complete 事件推送。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class StubAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self._last_usage = {}
            self._last_reasoning = ""

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            yield {"node": "finalize", "data": _finalize_data(self.workspace)}

    monkeypatch.setattr(api, "DataAnalysisAgent", StubAgent)

    def failing_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", failing_persist)

    with caplog.at_level(logging.ERROR, logger="data_agent.routers.analysis"):
        with client.stream(
            "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
        ) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: complete" in text
    assert "Failed to persist completed state" in caplog.text


def test_analyze_stream_cancelled_emitted_when_persist_fails(tmp_path, monkeypatch, caplog):
    """cancelled 分支的 persist 失败应记录日志且不影响 cancelled 事件推送。"""
    from data_agent.models import AnalysisCancelled

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class CancellingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            raise AnalysisCancelled("aborted")
            yield

    monkeypatch.setattr(api, "DataAnalysisAgent", CancellingAgent)

    def failing_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", failing_persist)

    with caplog.at_level(logging.ERROR, logger="data_agent.routers.analysis"):
        with client.stream(
            "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
        ) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: cancelled" in text
    assert "Failed to persist cancelled state" in caplog.text


def test_analyze_stream_error_emitted_when_persist_fails(tmp_path, monkeypatch, caplog):
    """error 分支的 persist 失败应记录日志且不影响 error 事件推送。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class FailingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            raise RuntimeError("boom")
            yield

    monkeypatch.setattr(api, "DataAnalysisAgent", FailingAgent)

    def failing_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", failing_persist)

    with caplog.at_level(logging.ERROR, logger="data_agent.routers.analysis"):
        with client.stream(
            "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
        ) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: error" in text
    assert "Failed to persist failed state" in caplog.text


def test_analyze_stream_cancel_error_path(tmp_path, monkeypatch):
    """generate() 的 except CancelledError 分支：set cancel_event、CAS 写 cancelling、
    等待 worker 退出（_await_worker_exit 轮询）。

    TestClient 断开连接抛的是 GeneratorExit 而非 CancelledError，因此用
    monkeypatch 让 queue.get 的 wait_for 抛 CancelledError 确定性触发该路径。
    """
    import time

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class SlowAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self.cancel_event = cancel_event

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            from data_agent.models import AnalysisCancelled

            # 若 CancelledError 路径 set 了 cancel_event，worker 应感知并取消
            time.sleep(0.8)
            if self.cancel_event and self.cancel_event.is_set():
                raise AnalysisCancelled("用户取消")
            yield {"node": "finalize", "data": _finalize_data(self.workspace)}

    monkeypatch.setattr(api, "DataAnalysisAgent", SlowAgent)

    def cancel_first(awaitable, timeout=None):
        awaitable.close()  # 关闭未 await 的协程，避免 RuntimeWarning
        raise asyncio.CancelledError()

    monkeypatch.setattr(api.asyncio, "wait_for", cancel_first)
    session_id = uploaded["id"]

    try:
        with client.stream("POST", f"/api/sessions/{session_id}/analyze/stream", json={"task": "x"}) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: started"):
                    break
    except Exception:
        pass  # CancelledError 会传播出 generate

    record = api.registry.get(session_id)
    deadline = time.time() + 8
    while record.run_lock.locked() and time.time() < deadline:
        time.sleep(0.1)
    # worker 感知取消并写入 cancelled 终态，证明 CancelledError 分支 set 了 event
    assert record.analysis_status == "cancelled", "CancelledError 路径应 set cancel_event 并取消 worker"
    assert not record.run_lock.locked(), "worker 退出后 run_lock 应被释放"
    assert api.analysis_slots.acquire(blocking=False)
    api.analysis_slots.release()


def test_analyze_stream_first_frame_disconnect_persist_failure(tmp_path, monkeypatch, caplog):
    """首帧断开兜底路径中 persist 失败应记录日志（408-409 分支）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class DummyAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "finalize", "data": {"response": "x", "dataset_profile": {}}}

    monkeypatch.setattr(api, "DataAnalysisAgent", DummyAgent)

    original_start = threading.Thread.start

    def failing_start(self):
        if self.name.startswith("analysis-"):
            raise RuntimeError("模拟线程启动失败")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    def failing_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", failing_persist)

    with caplog.at_level(logging.ERROR, logger="data_agent.routers.analysis"):
        try:
            with client.stream(
                "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
            ) as resp:
                for line in resp.iter_lines():
                    if line.startswith("event: started"):
                        break
        except Exception:
            pass
    assert "Failed to persist abort state" in caplog.text
    # 兜底路径已释放锁
    record = api.registry.get(uploaded["id"])
    assert record.run_lock.acquire(blocking=False)
    record.run_lock.release()


def test_chat_stream_emits_heartbeat_when_idle(tmp_path, monkeypatch):
    """chat_stream 长时间无事件时应推送 heartbeat（513-515 分支）。"""
    import time

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [{"role": "user", "content": "检查"}, {"role": "assistant", "content": "完成"}]
    api.registry.persist(session_id, record)

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class SlowChatAgent:
        def __init__(self, *args, **kwargs):
            self._last_usage = {}
            self._last_reasoning = ""

        def chat(self, query, history=None):
            time.sleep(0.3)
            return "回答", []

    monkeypatch.setattr(api, "DataAnalysisAgent", SlowChatAgent)
    # 缩短 wait_for 超时强制心跳
    monkeypatch.setattr("data_agent.api.asyncio.wait_for", _short_wait_for)

    with client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json={"task": "追问"}) as resp:
        events = []
        for line in resp.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):])
    assert "heartbeat" in events
    assert "chat_done" in events


def test_chat_stream_cancel_error_path(tmp_path, monkeypatch):
    """chat_stream 的 CancelledError 分支：应 set cancel_event（520-522）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [{"role": "user", "content": "检查"}, {"role": "assistant", "content": "完成"}]
    api.registry.persist(session_id, record)

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class ChatAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self._last_usage = {}
            self._last_reasoning = ""
            self.cancel_event = cancel_event

        def chat(self, query, history=None):
            # 若 CancelledError 分支 set 了 cancel_event，worker 应感知并抛取消
            import time

            time.sleep(0.8)
            if self.cancel_event and self.cancel_event.is_set():
                from data_agent.models import AnalysisCancelled

                raise AnalysisCancelled("追问取消")
            return "回答", []

    monkeypatch.setattr(api, "DataAnalysisAgent", ChatAgent)

    def cancel_first(awaitable, timeout=None):
        awaitable.close()  # 关闭未 await 的协程，避免 RuntimeWarning
        raise asyncio.CancelledError()

    monkeypatch.setattr(api.asyncio, "wait_for", cancel_first)

    try:
        with client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json={"task": "追问"}) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: started"):
                    break
    except Exception:
        pass

    record = api.registry.get(session_id)
    # worker 感知取消 → cancelled 事件已推送；锁由 worker finally 释放
    import time

    deadline = time.time() + 8
    while record.run_lock.locked() and time.time() < deadline:
        time.sleep(0.1)
    assert not record.run_lock.locked(), "worker 退出后 run_lock 应被释放"


def test_chat_stream_first_frame_disconnect_releases_lock_when_start_fails(tmp_path, monkeypatch):
    """chat_stream 的 worker.start() 失败时兜底释放 run_lock（534-537 分支）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [{"role": "user", "content": "检查"}, {"role": "assistant", "content": "完成"}]
    api.registry.persist(session_id, record)

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class DummyChatAgent:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, query, history=None):
            return "回答", []

    monkeypatch.setattr(api, "DataAnalysisAgent", DummyChatAgent)

    original_start = threading.Thread.start

    def failing_start(self):
        if self.name.startswith("chat-"):
            raise RuntimeError("模拟线程启动失败")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    try:
        with client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json={"task": "追问"}) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: started"):
                    break
    except Exception:
        pass

    record = api.registry.get(session_id)
    assert record.run_lock.acquire(blocking=False), "chat 兜底路径应释放 run_lock"
    record.run_lock.release()


def test_edit_chart_returns_500_on_render_failure(tmp_path, monkeypatch):
    """PUT edit 渲染阶段抛异常应返回 500（图表重新生成失败分支）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    from data_agent.routers import artifacts as artifacts_router

    def raise_render(*args, **kwargs):
        raise RuntimeError("render engine broken")

    monkeypatch.setattr(artifacts_router, "_render_plotly_html", raise_render)
    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"title": "x"},
    )
    assert response.status_code == 500
    assert "图表重新生成失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 流式端点兜底释放的异常吞掉分支 + chat persist 失败降级 + worker 未退出警告
# ---------------------------------------------------------------------------


def test_analyze_stream_first_frame_disconnect_swallows_release_errors(tmp_path, monkeypatch):
    """worker.start() 失败时兜底路径的 slots/lock 释放异常应被吞掉（400-405 分支）。

    客户端保持连接完整读取：generate 在 worker.start() 处抛错，finally 的
    兜底路径（worker_started=False）执行释放；释放异常被吞掉后错误继续传播。
    """
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class DummyAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "finalize", "data": {"response": "x", "dataset_profile": {}}}

    monkeypatch.setattr(api, "DataAnalysisAgent", DummyAgent)

    class BrokenSlots:
        def acquire(self, *args, **kwargs):
            return True

        def release(self):
            raise ValueError("already released")

    monkeypatch.setattr(api, "analysis_slots", BrokenSlots())

    original_start = threading.Thread.start

    def failing_start(self):
        if self.name.startswith("analysis-"):
            raise RuntimeError("模拟线程启动失败")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    # 完整读取：worker.start() 抛错 → 兜底释放（异常被吞）→ 错误继续传播
    try:
        with client.stream(
            "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
        ) as resp:
            for _line in resp.iter_lines():
                pass
    except Exception:
        pass  # 兜底释放异常已被吞掉，错误最终传播到客户端

    # 兜底路径必须已释放真实 run_lock（worker 从未启动，只有兜底能释放它）
    record = api.registry.get(uploaded["id"])
    assert record.run_lock.acquire(blocking=False), "兜底路径应释放 run_lock"
    record.run_lock.release()


def test_analyze_stream_first_frame_disconnect_swallows_lock_release_error(tmp_path, monkeypatch):
    """兜底路径的 run_lock 释放抛 RuntimeError 应被吞掉（404-405 分支）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class DummyAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "finalize", "data": {"response": "x", "dataset_profile": {}}}

    monkeypatch.setattr(api, "DataAnalysisAgent", DummyAgent)

    class BrokenLock:
        def acquire(self, *args, **kwargs):
            return True

        def release(self):
            raise RuntimeError("not held")

        def locked(self):
            return False

    record = api.registry.get(uploaded["id"])
    record.run_lock = BrokenLock()

    original_start = threading.Thread.start

    def failing_start(self):
        if self.name.startswith("analysis-"):
            raise RuntimeError("模拟线程启动失败")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    try:
        with client.stream(
            "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
        ) as resp:
            for _line in resp.iter_lines():
                pass
    except Exception:
        pass  # 兜底释放异常已被吞掉

    # slots 未被替换 → 兜底正常释放，可重新 acquire
    assert api.analysis_slots.acquire(blocking=False), "兜底路径应释放 analysis_slots"
    api.analysis_slots.release()


def test_chat_stream_persist_failure_logged(tmp_path, monkeypatch, caplog):
    """chat_done 分支的 persist 失败应记录日志且不影响 chat_done 推送。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [{"role": "user", "content": "检查"}, {"role": "assistant", "content": "完成"}]
    api.registry.persist(session_id, record)

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class ChatAgent:
        def __init__(self, *args, **kwargs):
            self._last_usage = {}
            self._last_reasoning = ""

        def chat(self, query, history=None):
            return "回答", []

    monkeypatch.setattr(api, "DataAnalysisAgent", ChatAgent)

    def failing_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", failing_persist)

    with caplog.at_level(logging.ERROR, logger="data_agent.routers.analysis"):
        with client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json={"task": "追问"}) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: chat_done" in text
    assert "Failed to persist chat state" in caplog.text


def test_chat_stream_cancelled_persist_failure_logged(tmp_path, monkeypatch, caplog):
    """chat cancelled 分支的 persist 失败应记录日志且不影响 cancelled 推送。"""
    from data_agent.models import AnalysisCancelled

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [{"role": "user", "content": "检查"}, {"role": "assistant", "content": "完成"}]
    api.registry.persist(session_id, record)

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class CancellingChatAgent:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, query, history=None):
            raise AnalysisCancelled("追问取消")

    monkeypatch.setattr(api, "DataAnalysisAgent", CancellingChatAgent)

    def failing_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", failing_persist)

    with caplog.at_level(logging.ERROR, logger="data_agent.routers.analysis"):
        with client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json={"task": "追问"}) as resp:
            text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: cancelled" in text
    assert "Failed to persist cancelled chat state" in caplog.text


def test_chat_stream_first_frame_disconnect_swallows_release_error(tmp_path, monkeypatch):
    """chat 兜底释放 run_lock 抛 RuntimeError 时应被吞掉（536-537 分支）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")
    session_id = uploaded["id"]
    record = api.registry.get(session_id)
    record.analysis_status = "completed"
    record.chat = [{"role": "user", "content": "检查"}, {"role": "assistant", "content": "完成"}]
    api.registry.persist(session_id, record)

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class DummyChatAgent:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, query, history=None):
            return "回答", []

    monkeypatch.setattr(api, "DataAnalysisAgent", DummyChatAgent)

    class BrokenLock:
        released = {"n": 0}

        def acquire(self, *args, **kwargs):
            return True

        def release(self):
            BrokenLock.released["n"] += 1
            raise RuntimeError("not held")

        def locked(self):
            return False

    record.run_lock = BrokenLock()

    original_start = threading.Thread.start
    calls = {"n": 0}

    def failing_start(self):
        calls["n"] += 1
        if self.name.startswith("chat-"):
            raise RuntimeError("模拟线程启动失败")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    # 完整读取：worker.start() 抛错 → 兜底释放（异常被吞）→ 错误继续传播
    try:
        with client.stream("POST", f"/api/sessions/{session_id}/chat/stream", json={"task": "追问"}) as resp:
            for _line in resp.iter_lines():
                pass
    except Exception:
        pass  # 兜底释放异常已被吞掉

    # 探针：chat worker 的 start 必须被调用过，且兜底路径必须调用过 release
    assert calls["n"] >= 1, "请求未执行（chat worker.start 未被调用）"
    assert BrokenLock.released["n"] >= 1, "兜底路径未执行 run_lock.release"

def test_analyze_stream_cancel_warns_when_worker_still_alive(tmp_path, monkeypatch, caplog):
    """取消后 worker 5s 内未退出应记录 warning（376-380 分支）。"""
    import time

    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = _upload_csv_session(client, content=b"region,sales\nEast,100\n")

    monkeypatch.setattr(
        api, "_effective_settings", lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs")
    )

    class VerySlowAgent:
        def __init__(self, workspace, settings, cancel_event=None, progress_callback=None, event_callback=None):
            self.workspace = workspace
            self.cancel_event = cancel_event

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            # 长于 _await_worker_exit 的 5s 等待窗口，worker 无法及时退出
            time.sleep(8)
            if self.cancel_event and self.cancel_event.is_set():
                from data_agent.models import AnalysisCancelled

                raise AnalysisCancelled("用户取消")
            yield {"node": "finalize", "data": _finalize_data(self.workspace)}

    monkeypatch.setattr(api, "DataAnalysisAgent", VerySlowAgent)
    # 缩短等待窗口以加速测试：直接改模块常量不现实（5.0 硬编码），
    # 用 wait_for 抛 CancelledError 触发取消路径，等待窗口为真实的 5s。
    def cancel_first(awaitable, timeout=None):
        awaitable.close()
        raise asyncio.CancelledError()

    monkeypatch.setattr(api.asyncio, "wait_for", cancel_first)

    with caplog.at_level(logging.WARNING, logger="data_agent.routers.analysis"):
        try:
            with client.stream(
                "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "x"}
            ) as resp:
                for line in resp.iter_lines():
                    if line.startswith("event: started"):
                        break
        except Exception:
            pass
    assert "did not exit within 5s" in caplog.text


