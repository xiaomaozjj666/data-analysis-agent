"""跨会话记忆库：存储、去重、上限、规划注入与 API 端点。"""

from __future__ import annotations

import pytest

from data_agent.memory import (
    MAX_MEMORY_CHARS,
    MAX_MEMORY_ENTRIES,
    add_memory,
    delete_memory,
    list_memories,
    render_memory_context,
)


def test_add_and_list_roundtrip(tmp_path):
    add_memory(tmp_path, "销售额口径以 net_amount 为准，不要用 gross", session_id="s1")
    add_memory(tmp_path, "图表优先用 ECharts 中文标签", kind="preference", session_id="s1")

    entries = list_memories(tmp_path)
    assert len(entries) == 2
    assert {entry["kind"] for entry in entries} == {"lesson", "preference"}
    assert all(entry["uses"] == 1 for entry in entries)
    assert all(entry["id"] for entry in entries)


def test_duplicate_text_bumps_uses_instead_of_new_entry(tmp_path):
    add_memory(tmp_path, "销售额口径以 net_amount 为准")
    again = add_memory(tmp_path, "  销售额口径以 NET_AMOUNT 为准  ")
    entries = list_memories(tmp_path)
    assert len(entries) == 1
    assert entries[0]["uses"] == 2
    assert again["id"] == entries[0]["id"]


def test_invalid_kind_and_empty_text_rejected(tmp_path):
    with pytest.raises(ValueError, match="kind"):
        add_memory(tmp_path, "x", kind="diary")
    with pytest.raises(ValueError, match="不能为空"):
        add_memory(tmp_path, "   ")


def test_overlong_text_rejected(tmp_path):
    with pytest.raises(ValueError, match="超过"):
        add_memory(tmp_path, "长" * (MAX_MEMORY_CHARS + 1))


def test_capacity_cap_rejects_new_entries(tmp_path):
    for index in range(MAX_MEMORY_ENTRIES):
        add_memory(tmp_path, f"教训 {index}")
    with pytest.raises(ValueError, match="记忆库已满"):
        add_memory(tmp_path, "第 101 条")
    assert len(list_memories(tmp_path, limit=MAX_MEMORY_ENTRIES)) == MAX_MEMORY_ENTRIES


def test_delete_memory(tmp_path):
    entry = add_memory(tmp_path, "会被删除的一条")
    assert delete_memory(tmp_path, entry["id"]) is True
    assert delete_memory(tmp_path, entry["id"]) is False
    assert list_memories(tmp_path) == []


def test_render_memory_context_empty_when_no_entries(tmp_path):
    assert render_memory_context(tmp_path) == ""


def test_render_memory_context_frames_and_orders_by_uses(tmp_path):
    add_memory(tmp_path, "低频教训", session_id="s0")
    high = add_memory(tmp_path, "高频偏好：导出必须带口径说明", kind="preference")
    add_memory(tmp_path, high["text"])  # 再复用一次 → uses=2

    context = render_memory_context(tmp_path)
    assert "不是当前用户指令" in context
    assert "与当前任务冲突时以当前任务为准" in context
    assert "[偏好] 高频偏好" in context
    assert "已复用 2 次" in context
    # 复用次数多的条目排前
    assert context.index("高频偏好") < context.index("低频教训")


def test_planning_template_has_memory_placeholder():
    from data_agent.prompts import get_prompts

    for language in ("zh", "en"):
        template = get_prompts(language)["plan_template"]
        assert "{memory_context}" in template


def test_plan_node_injects_memory_context(tmp_path):
    """规划提示词必须带 memory_context：空记忆时为空串，有记忆时含条目。"""
    from data_agent.nodes.plan import plan_analysis

    captured: dict[str, str] = {}

    class _FakePlanner:
        def invoke(self, prompt, config=None):
            captured["prompt"] = prompt
            return {
                "objective": "测试目标",
                "steps": [
                    {"id": "s1", "title": "检查数据", "instruction": "检查", "success_criteria": "完成"}
                ],
            }

    class _FakeSettings:
        runs_dir = tmp_path
        max_plan_steps = 6

    class _FakeAgent:
        workspace = type("W", (), {"root": tmp_path})()
        settings = _FakeSettings()
        prompts = {
            "plan_template": "查询：{query}\n概况：{profile_text}\n口径：{metric_context}\n记忆：{memory_context}"
        }

        def _ensure_not_cancelled(self):
            return None

        def _enter_node(self, *args, **kwargs):
            return None

        def _invoke_config(self):
            return {}

    agent = _FakeAgent()
    agent.planner = _FakePlanner()  # type: ignore[attr-defined]
    state = {
        "query": "按区域看销售额",
        "dataset_profile": {"columns": ["region", "sales"]},
    }

    # 无记忆：memory_context 为空
    plan_analysis(agent, dict(state))
    assert "记忆：" in captured["prompt"]

    add_memory(tmp_path, "销售额以 net_amount 为准", session_id="s1")
    plan_analysis(agent, dict(state))
    assert "销售额以 net_amount 为准" in captured["prompt"]
    assert "不是当前用户指令" in captured["prompt"]


def test_api_memory_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api
    from data_agent.config import AgentSettings

    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(
        api_key="not-used",
        provider="deepseek",
        runs_dir=runs_dir,
        max_active_sessions=100,
        rate_limit_per_minute=1000,
        max_concurrent_analyses=2,
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)

    from data_agent.api import app

    client = TestClient(app)

    response = client.post(
        "/api/memories",
        json={"text": "毛利率口径不含税", "kind": "lesson", "session_id": "sess-1"},
    )
    assert response.status_code == 201
    entry = response.json()["memory"]

    listing = client.get("/api/memories")
    assert listing.status_code == 200
    assert listing.json()["memories"][0]["text"] == "毛利率口径不含税"

    deleted = client.delete(f"/api/memories/{entry['id']}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True

    missing = client.delete(f"/api/memories/{entry['id']}")
    assert missing.status_code == 404

    bad = client.post("/api/memories", json={"text": "x", "kind": "diary"})
    assert bad.status_code == 422
