"""会话级分析内核（SessionKernel）的单元与集成测试。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from data_agent import api
from data_agent._registry_payloads import _session_payload
from data_agent.config import AgentSettings
from data_agent.registry import SessionRegistry
from data_agent.session_kernel import KERNEL_TOOL_TRACE_LIMIT, SessionKernel
from data_agent.storage import LocalSessionStorage
from data_agent.workspace import DataWorkspace

CSV = b"region,sales\nEast,100\nWest,200\n"


# ---------------------------------------------------------------------------
# 纯数据结构
# ---------------------------------------------------------------------------


def test_kernel_to_dict_trims_tool_trace_and_from_dict_roundtrips():
    kernel = SessionKernel(objective="检查销售")
    for index in range(KERNEL_TOOL_TRACE_LIMIT + 5):
        kernel.append_tool({"call_id": f"c{index}", "name": "inspect_data", "status": "running"})
    payload = kernel.to_dict()
    assert len(payload["tool_trace"]) == KERNEL_TOOL_TRACE_LIMIT
    assert payload["tool_trace"][-1]["call_id"] == f"c{KERNEL_TOOL_TRACE_LIMIT + 4}"
    restored = SessionKernel.from_dict(payload)
    assert restored.objective == "检查销售"
    assert len(restored.tool_trace) == KERNEL_TOOL_TRACE_LIMIT
    assert restored.to_dict() == payload


def test_kernel_from_dict_tolerates_bad_types():
    kernel = SessionKernel.from_dict(
        {
            "objective": 123,
            "plan": "not-a-list",
            "completed_steps": [{"id": "a"}, "bad", None],
            "status": None,
            "tool_trace": ["bad"],
            "updated_at": "yesterday",
        }
    )
    assert kernel.objective == "123"
    assert kernel.plan == []
    assert kernel.completed_steps == [{"id": "a"}]
    assert kernel.status == "idle"
    assert kernel.tool_trace == []
    assert kernel.updated_at is None
    assert SessionKernel.from_dict(None).status == "idle"


def test_kernel_update_ignores_unknown_fields_and_merges_tools():
    kernel = SessionKernel()
    kernel.update(plan=[{"id": "p"}], unknown_field="ignored")
    assert kernel.plan == [{"id": "p"}]
    assert not hasattr(kernel, "unknown_field")
    assert kernel.updated_at is not None

    kernel.append_tool({"call_id": "c1", "name": "inspect_data", "status": "running"})
    kernel.append_tool({"call_id": "c1", "output_preview": "ok"})
    assert len(kernel.tool_trace) == 1
    assert kernel.tool_trace[0]["output_preview"] == "ok"
    assert kernel.tool_trace[0]["name"] == "inspect_data"

    kernel.update_tool("c1", status="done")
    kernel.update_tool("missing", status="done")
    kernel.update_tool("", status="done")
    assert kernel.tool_trace[0]["status"] == "done"


# ---------------------------------------------------------------------------
# Registry 持久化 + 会话载荷
# ---------------------------------------------------------------------------


def _registry_with_session(tmp_path: Path) -> tuple[SessionRegistry, str]:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    registry = SessionRegistry(runs_dir, 10, 24.0, storage=LocalSessionStorage())
    workspace = DataWorkspace(runs_dir, session_id="kernel_case")
    source = tmp_path / "sales.csv"
    source.write_bytes(CSV)
    workspace.load(source, copy_into_workspace=True)
    session_id, _ = registry.create(workspace)
    return registry, session_id


def test_registry_persists_and_restores_kernel(tmp_path):
    registry, session_id = _registry_with_session(tmp_path)
    record = registry.get(session_id)
    record.update_kernel(
        objective="检查销售",
        plan=[{"id": "inspect", "title": "检查数据"}],
        plan_source="model",
        completed_steps=[{"id": "inspect", "status": "ok"}],
        current_node="replan",
        status="running",
    )
    record.append_kernel_tool({"call_id": "c1", "name": "inspect_data", "status": "running"})
    payload = _session_payload(session_id, record)
    assert payload["kernel"]["objective"] == "检查销售"
    assert payload["kernel"]["tool_trace"][0]["call_id"] == "c1"

    registry.persist(session_id, record)
    registry._items.pop(session_id, None)  # noqa: SLF001 - 模拟进程重启
    restored = registry.get(session_id)
    kernel = restored.kernel_payload()
    assert kernel["objective"] == "检查销售"
    assert kernel["plan_source"] == "model"
    assert kernel["completed_steps"] == [{"id": "inspect", "status": "ok"}]
    assert kernel["current_node"] == "replan"
    assert kernel["status"] == "running"
    assert kernel["tool_trace"][0]["name"] == "inspect_data"


# ---------------------------------------------------------------------------
# 流式分析按节点/工具事件更新内核
# ---------------------------------------------------------------------------


def _isolate_runtime(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
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
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(
        api,
        "registry",
        SessionRegistry(
            runs_dir, settings.max_active_sessions, settings.session_ttl_hours,
            storage=LocalSessionStorage(),
        ),
    )
    monkeypatch.setattr(api, "session_storage", LocalSessionStorage())
    # 避免 _effective_settings() 重新读取 .env 把 APP_ACCESS_TOKEN 注入回来，
    # 导致后续 GET 被访问令牌拦截（与 test_api 的隔离策略一致）。
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.setattr(
        api, "analysis_slots", threading.BoundedSemaphore(settings.max_concurrent_analyses)
    )
    api.request_buckets.clear()
    return runs_dir


def test_stream_updates_kernel_per_node_and_tool_events(tmp_path, monkeypatch):
    runs_dir = _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions", files={"file": ("sales.csv", CSV, "text/csv")}
    ).json()
    # 指标定义文件存在时，分析启动前会固定其内容哈希（覆盖 _snapshot_metrics
    # 的读取分支，也让"口径防改写"在流式分析里真实生效）。
    (runs_dir / uploaded["id"] / "metrics.json").write_text(
        json.dumps({"metrics": []}), encoding="utf-8"
    )

    class StreamingAgent:
        def __init__(self, workspace, settings, event_callback=None, **_kwargs):
            self.workspace = workspace
            self.event_callback = event_callback

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            if self.event_callback:
                self.event_callback(
                    "tool_call",
                    {"call_id": "call-1", "name": "inspect_data", "input_preview": "{}"},
                )
                self.event_callback(
                    "tool_result",
                    {"call_id": "call-1", "output_preview": "rows=2", "duration_ms": 7},
                )
            plan = [
                {"id": "inspect", "title": "检查数据", "instruction": "x", "success_criteria": "y"}
            ]
            yield {
                "node": "plan_analysis",
                "data": {"objective": "检查销售", "plan": plan, "plan_source": "model"},
            }
            yield {
                "node": "replan",
                "data": {"completed_steps": [{"id": "inspect", "status": "ok"}]},
            }
            yield {
                "node": "finalize",
                "data": {
                    "response": "完成",
                    "artifacts": [],
                    "dataset_profile": self.workspace.profile(),
                    "plan": plan,
                    "completed_steps": [{"id": "inspect", "status": "ok"}],
                },
            }

    monkeypatch.setattr(api, "DataAnalysisAgent", StreamingAgent)
    with client.stream(
        "POST", f"/api/sessions/{uploaded['id']}/analyze/stream", json={"task": "检查销售"}
    ) as response:
        assert response.status_code == 200
        b"".join(response.iter_bytes())

    kernel = client.get(f"/api/sessions/{uploaded['id']}").json()["kernel"]
    assert kernel["objective"] == "检查销售"
    assert kernel["plan_source"] == "model"
    assert kernel["status"] == "completed"
    assert kernel["current_node"] == "finalize"
    assert kernel["completed_steps"] == [{"id": "inspect", "status": "ok"}]
    assert kernel["tool_trace"][0]["call_id"] == "call-1"
    assert kernel["tool_trace"][0]["status"] == "done"
    assert kernel["tool_trace"][0]["output_preview"] == "rows=2"


def test_stream_plan_only_marks_kernel_awaiting_approval(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    uploaded = client.post(
        "/api/sessions", files={"file": ("sales.csv", CSV, "text/csv")}
    ).json()

    plan = [{"id": "inspect", "title": "检查数据", "instruction": "x", "success_criteria": "y"}]

    class PlanOnlyAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def stream(self, query, history=None, resume_from=None, plan_only=False):
            yield {
                "node": "plan_analysis",
                "data": {"objective": query, "plan": plan, "plan_source": "fallback"},
            }

    monkeypatch.setattr(api, "DataAnalysisAgent", PlanOnlyAgent)
    with client.stream(
        "POST",
        f"/api/sessions/{uploaded['id']}/analyze/stream",
        json={"task": "检查销售", "plan_only": True},
    ) as response:
        b"".join(response.iter_bytes())

    kernel = client.get(f"/api/sessions/{uploaded['id']}").json()["kernel"]
    assert kernel["status"] == "awaiting_approval"
    assert kernel["plan_source"] == "fallback"
    assert kernel["plan"] == plan
