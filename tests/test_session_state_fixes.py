"""本轮集成层修复的回归测试。

覆盖四件事：
1. 产物磁盘 → 注册表同步（落盘未注册的文件不再显示为 0）。
2. 核验结果会话级持久化（复算后 GET last 可恢复，confirm 并入回执）。
3. 候选数据集采纳 API（join 不再静默接管主数据的 UI 侧闭环）。
4. 图表空数据兜底（Plotly/ECharts 双引擎，空序列拒绝产出空白 HTML）。
"""

from __future__ import annotations

import json

import pytest
from conftest import _skip_kaleido  # noqa: F401
from fastapi.testclient import TestClient

from data_agent import api
from data_agent.audit_state import load_last_audit
from data_agent.tools import build_tools
from data_agent.tools._charts.validate import (
    _ensure_echarts_series_has_data,
    _ensure_plotly_fig_has_data,
)
from data_agent.workspace import DataWorkspace
from tests.test_api import _isolate_runtime

# ---------------------------------------------------------------------------
# 产物磁盘同步
# ---------------------------------------------------------------------------


def test_artifact_dropped_on_disk_appears_in_session_payload(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()["id"]

    workspace = api.registry.get(session_id).workspace
    stray = workspace.artifacts_dir / "手工放置_1.html"
    stray.write_text("<html><body>chart</body></html>", encoding="utf-8")

    payload = client.get(f"/api/sessions/{session_id}").json()
    names = {item["name"] for item in payload["artifacts"]}
    assert "手工放置_1.html" in names


def test_artifacts_rescan_endpoint_reports_added_files(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()["id"]

    workspace = api.registry.get(session_id).workspace
    (workspace.artifacts_dir / "export_result.csv").write_text(
        "region,sales\nEast,100\n", encoding="utf-8"
    )
    body = client.post(f"/api/sessions/{session_id}/artifacts/rescan").json()
    assert body["added"] == 1
    assert any(item["name"] == "export_result.csv" for item in body["artifacts"])
    # 幂等：再扫一次不重复计数
    again = client.post(f"/api/sessions/{session_id}/artifacts/rescan").json()
    assert again["added"] == 0


# ---------------------------------------------------------------------------
# 核验结果持久化
# ---------------------------------------------------------------------------


def test_sales_audit_persists_and_last_endpoint_restores(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    sample = client.post("/api/sessions/sample?audit=true")
    assert sample.status_code == 201
    session_id = sample.json()["id"]

    draft = {"draft": "销售额合计：17224.00 元\n销量合计：462 件\n订单数：462 笔\n市场份额：37%"}
    result = client.post(f"/api/sessions/{session_id}/sales-audit", json=draft).json()
    assert result["summary"] == {"verified": 2, "conflict": 1, "unsupported": 1}

    last = client.get(f"/api/sessions/{session_id}/sales-audit/last")
    assert last.status_code == 200
    payload = last.json()
    assert payload["draft"] == draft["draft"]
    assert payload["result"]["summary"] == result["summary"]
    assert payload["result"]["audit_id"] == result["audit_id"]
    # 尚未确认发布：无回执
    assert payload.get("receipt") is None

    # 服务重启模拟：清掉内存缓存后仍可从磁盘恢复
    api.registry._items.pop(session_id, None)
    last_after_restart = client.get(f"/api/sessions/{session_id}/sales-audit/last")
    assert last_after_restart.status_code == 200
    assert last_after_restart.json()["result"]["audit_id"] == result["audit_id"]


def test_sales_audit_last_returns_404_before_first_audit(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    sample = client.post("/api/sessions/sample?audit=true")
    session_id = sample.json()["id"]
    response = client.get(f"/api/sessions/{session_id}/sales-audit/last")
    assert response.status_code == 404


def test_confirm_attaches_receipt_to_audit_state(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    sample = client.post("/api/sessions/sample?audit=true")
    session_id = sample.json()["id"]

    draft = {"draft": "销售额合计：17224.00 元\n销量合计：462 件"}
    audit = client.post(f"/api/sessions/{session_id}/sales-audit", json=draft).json()
    if audit.get("corrected_brief") is None:
        pytest.skip("该草稿没有可发布的核验结论")
    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft["draft"], "audit_id": audit["audit_id"]},
    )
    assert confirm.status_code == 200
    receipt = confirm.json()["receipt"]

    state = load_last_audit(api.registry.get(session_id).workspace.root)
    assert state is not None
    assert state["receipt"]["hash"] == receipt["hash"]
    assert state["confirmed_brief"]


def test_audit_state_tolerates_missing_and_corrupt_files(tmp_path):
    """核验状态文件缺失 / 损坏时只降级为 None，不阻塞主链路。"""
    from data_agent.audit_state import attach_receipt, save_last_audit

    root = tmp_path / "sess"
    # 文件不存在：attach 静默返回，load 返回 None
    attach_receipt(root, {"hash": "x"}, "brief")
    assert load_last_audit(root) is None

    path = root / "audit" / "last_audit.json"
    path.parent.mkdir(parents=True)

    path.write_text("{not json", encoding="utf-8")
    attach_receipt(root, {"hash": "x"}, "brief")
    assert load_last_audit(root) is None

    # JSON 合法但不是对象
    path.write_text("[]", encoding="utf-8")
    attach_receipt(root, {"hash": "x"}, "brief")
    assert load_last_audit(root) is None

    # 对象但 result 不是字典
    path.write_text('{"result": "not-a-dict"}', encoding="utf-8")
    assert load_last_audit(root) is None

    # 正常写入后仍可读取
    save_last_audit(root, {"summary": {"ok": True}}, "draft")
    assert load_last_audit(root)["result"] == {"summary": {"ok": True}}


def test_audit_state_receipt_write_failure_is_swallowed(tmp_path, monkeypatch):
    """回执落盘失败只记日志：核验/确认接口本身不能因此失败。"""
    from data_agent import audit_state

    root = tmp_path / "sess"
    audit_state.save_last_audit(root, {"summary": {"ok": True}}, "draft")

    def broken_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(audit_state.os, "replace", broken_replace)
    audit_state.attach_receipt(root, {"hash": "h"}, "brief")  # 不抛异常

    loaded = audit_state.load_last_audit(root)
    assert loaded is not None
    # 旧结果保留，回执没有写进去
    assert "receipt" not in loaded


# ---------------------------------------------------------------------------
# 候选数据集采纳 API
# ---------------------------------------------------------------------------


def test_dataset_adopt_endpoint_replaces_active_dataset(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()["id"]
    workspace = api.registry.get(session_id).workspace
    tools = {tool.name: tool for tool in build_tools(workspace)}
    workspace.save_upload("extra.csv", b"region,manager\nEast,A\nWest,B\n")
    join_payload = json.loads(
        tools["join_datasets"].invoke(
            {"right_source": "extra.csv", "left_on": ["region"], "how": "left"}
        )
    )
    assert join_payload["adopted"] is False

    payload = client.get(f"/api/sessions/{session_id}").json()
    assert {item["name"] for item in payload["candidate_datasets"]} == {"joined_data.csv"}

    adopted = client.post(
        f"/api/sessions/{session_id}/datasets/adopt", json={"name": "joined_data.csv"}
    )
    assert adopted.status_code == 200
    assert adopted.json()["after_rows"] == 2

    refreshed = client.get(f"/api/sessions/{session_id}").json()
    assert refreshed["candidate_datasets"] == []
    assert "manager" in [row for row in refreshed["preview"]][0]

    # 采纳状态跨重启：候选清空、主数据保持合并结果
    api.registry._items.pop(session_id, None)
    after_restart = client.get(f"/api/sessions/{session_id}").json()
    assert after_restart["candidate_datasets"] == []
    assert len(after_restart["preview"][0]) == 3  # region, sales, manager


def test_dataset_adopt_endpoint_discards_candidate(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()["id"]
    workspace = api.registry.get(session_id).workspace
    tools = {tool.name: tool for tool in build_tools(workspace)}
    workspace.save_upload("extra.csv", b"region,manager\nEast,A\nWest,B\n")
    tools["join_datasets"].invoke(
        {"right_source": "extra.csv", "left_on": ["region"], "how": "left"}
    )
    response = client.post(
        f"/api/sessions/{session_id}/datasets/adopt",
        json={"name": "joined_data.csv", "discard": True},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    # 主数据未被替换
    refreshed = client.get(f"/api/sessions/{session_id}").json()
    assert refreshed["candidate_datasets"] == []
    assert len(refreshed["preview"][0]) == 2


def test_dataset_adopt_unknown_name_returns_404(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()["id"]
    response = client.post(
        f"/api/sessions/{session_id}/datasets/adopt", json={"name": "ghost.csv"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 图表空数据兜底
# ---------------------------------------------------------------------------


def test_plotly_guard_rejects_figure_without_points():
    import plotly.graph_objects as go

    empty_fig = go.Figure(go.Scatter(x=[], y=[]))
    with pytest.raises(ValueError, match="图表数据为空"):
        _ensure_plotly_fig_has_data(empty_fig, "scatter")

    ok_fig = go.Figure(go.Scatter(x=[1, 2], y=[3, 4]))
    _ensure_plotly_fig_has_data(ok_fig, "scatter")


def test_echarts_guard_rejects_all_empty_series():
    with pytest.raises(ValueError, match="图表数据为空"):
        _ensure_echarts_series_has_data(
            {"series": [{"type": "bar", "data": []}, {"type": "line", "data": []}]},
            "bar",
        )
    _ensure_echarts_series_has_data(
        {"series": [{"type": "bar", "data": []}, {"type": "line", "data": [1, 2]}]},
        "bar",
    )


def test_create_visualization_refuses_all_null_value_column(tmp_path, monkeypatch):
    """聚合列全空时（轴写反的典型后果）拒绝产出图表而不是静默空白。

    语义校验（_validate_chart_semantics）先拦下大部分输入错误；这里是
    纵深防御：无论哪层拦下，只要数据画不出东西就抛 ValueError 让模型
    重试，绝不产出"能打开但一片空白"的 HTML。
    """
    _skip_kaleido()
    ws = DataWorkspace(tmp_path / "runs", session_id="empty-chart")
    source = tmp_path / "data.csv"
    source.write_text("region,sales\nEast,\nWest,\n", encoding="utf-8")
    ws.load(source, copy_into_workspace=True)
    tools = {tool.name: tool for tool in build_tools(ws)}
    with pytest.raises(ValueError):
        tools["create_visualization"].invoke(
            {"chart_type": "bar", "x": "region", "y": "sales", "aggregation": "sum"}
        )
    # 图表产物没有落盘（拒绝了就不留空白 HTML）
    assert not list(ws.artifacts_dir.glob("*.html"))


# ---------------------------------------------------------------------------
# workspace 层：候选登记持久化与磁盘同步
# ---------------------------------------------------------------------------


def test_candidate_registry_roundtrip(tmp_path):
    ws = DataWorkspace(tmp_path, session_id="rt")
    ws.register_candidate_dataset("joined_data.csv", rows=42, note="left join")
    restored = DataWorkspace(tmp_path, session_id="rt")
    restored.restore_candidates(ws.candidate_datasets)
    assert restored.candidate_datasets[0]["name"] == "joined_data.csv"
    assert restored.candidate_datasets[0]["rows"] == 42


def test_sync_artifacts_from_disk_is_idempotent(tmp_path):
    ws = DataWorkspace(tmp_path, session_id="sync")
    source = tmp_path / "data.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    ws.load(source, copy_into_workspace=True)
    (ws.artifacts_dir / "foo.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    assert ws.sync_artifacts_from_disk() == 1
    assert ws.sync_artifacts_from_disk() == 0
