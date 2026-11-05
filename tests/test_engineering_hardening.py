"""请求隔离、全方法限流与 HTTP 可观测性回归测试。"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from data_agent import api
from data_agent.config import AgentSettings


def _configure_api(monkeypatch, *, write: int = 100, read: int = 100, health: int = 100) -> None:
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        api,
        "bootstrap_settings",
        AgentSettings(
            api_key="test-key",
            runs_dir=Path("runs"),
            rate_limit_per_minute=write,
            read_rate_limit_per_minute=read,
            health_rate_limit_per_minute=health,
        ),
    )
    api.request_buckets.clear()


def test_runtime_settings_are_isolated_between_concurrent_clients(monkeypatch) -> None:
    _configure_api(monkeypatch)
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *args, **kwargs: False)
    clients = (TestClient(api.app), TestClient(api.app))
    for client in clients:
        assert client.get("/api/settings").status_code == 200

    barrier = threading.Barrier(2)

    def update(client: TestClient, thinking: bool, effort: str, key: str):
        barrier.wait(timeout=5)
        return client.put(
            "/api/settings",
            json={
                "api_key": key,
                "persist_key": False,
                "thinking_enabled": thinking,
                "reasoning_effort": effort,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda args: update(*args),
                (
                    (clients[0], False, "max", "key-user-a"),
                    (clients[1], True, "high", "key-user-b"),
                ),
            )
        )
    assert all(response.status_code == 200 for response in responses)

    # 两个请求已并发写配置；随后每个客户端必须持续读回自己的值。
    first = clients[0].get("/api/settings").json()
    second = clients[1].get("/api/settings").json()
    assert (first["thinking_enabled"], first["reasoning_effort"]) == (False, "max")
    assert (second["thinking_enabled"], second["reasoning_effort"]) == (True, "high")


def test_request_id_is_propagated_and_written_to_structured_log(monkeypatch, caplog) -> None:
    _configure_api(monkeypatch)
    client = TestClient(api.app)
    with caplog.at_level(logging.INFO, logger="data_agent.middleware"):
        response = client.get("/api/health", headers={"X-Request-ID": "req-sales-001"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-sales-001"
    records = [json.loads(item.message) for item in caplog.records if item.message.startswith("{")]
    entry = next(item for item in records if item["request_id"] == "req-sales-001")
    assert entry["event"] == "http_request"
    assert entry["route"] == "/api/health"
    assert entry["status_code"] == 200
    assert entry["duration_ms"] >= 0


def test_invalid_request_id_is_replaced(monkeypatch) -> None:
    _configure_api(monkeypatch)
    response = TestClient(api.app).get("/api/health", headers={"X-Request-ID": "bad request id"})
    assert response.headers["X-Request-ID"] != "bad request id"
    assert len(response.headers["X-Request-ID"]) == 32


def test_metrics_endpoint_exposes_low_cardinality_metrics_without_secrets(monkeypatch) -> None:
    _configure_api(monkeypatch)
    client = TestClient(api.app)
    client.get("/api/health", headers={"X-Request-ID": "secret-request-id"})
    client.get("/api/sessions/sensitive-session-id?token=secret-query")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "data_agent_http_requests_total" in text
    assert "data_agent_http_request_duration_seconds" in text
    assert "data_agent_http_request_errors_total" in text
    assert 'route="/api/health"' in text
    assert "secret-request-id" not in text
    assert "sensitive-session-id" not in text
    assert "secret-query" not in text


def test_metrics_endpoint_can_be_disabled(monkeypatch) -> None:
    _configure_api(monkeypatch)
    monkeypatch.setenv("DATA_AGENT_METRICS_ENABLED", "false")
    assert TestClient(api.app).get("/metrics").status_code == 404


def test_metrics_endpoint_rejects_non_loopback_client(monkeypatch) -> None:
    from data_agent.routers.settings import prometheus_metrics

    class Client:
        host = "203.0.113.10"

    class Request:
        client = Client()

    monkeypatch.setenv("DATA_AGENT_METRICS_ENABLED", "true")
    with pytest.raises(HTTPException) as exc_info:
        prometheus_metrics(Request())
    assert exc_info.value.status_code == 404


def test_get_requests_use_independent_read_limit(monkeypatch) -> None:
    _configure_api(monkeypatch, write=1, read=2, health=10)
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *args, **kwargs: False)
    client = TestClient(api.app)

    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/settings").status_code == 200
    limited = client.get("/api/settings")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    # 读取桶耗尽不占用写请求配额。
    assert client.post("/api/sessions/missing/cancel").status_code == 404


def test_health_endpoint_is_also_rate_limited(monkeypatch) -> None:
    _configure_api(monkeypatch, write=10, read=10, health=2)
    client = TestClient(api.app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").status_code == 429
