"""黄金路径 E2E：上传 → 分析 → 产物 → 简报核验 → 确认 → 证据包。

这是产品的主干道，此前没有任何一条测试把它从头走到尾——各函数单测都绿，
集成层照样"打开就是 0"（产物落盘未注册、核验结果刷新即丢、join 静默改主
数据）。本测试用 FakeAgent 驱动**真实工具链**（真写图表 HTML、真落盘候选
数据集），并在中间模拟服务重启，验证跨进程状态一致。

CI 每次跑；失败即禁止合并。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from data_agent import api
from data_agent.agent import AnalysisResult
from data_agent.config import AgentSettings
from data_agent.tools import build_tools

from tests.test_api import _isolate_runtime

_UPLOAD_CSV = (
    b"order_id,sales,quantity,region\n"
    b"A,10.00,1,East\n"
    b"A,2.50,1,East\n"
    b"B,7.50,1,West\n"
    b"C,30.00,4,West\n"
)
_DRAFT = "销售额合计：50.00 元\n销量合计：7 件\n订单数：3 笔\n市场份额：85%"


class RealToolAgent:
    """假装是 LLM Agent，实际把模型该做的工具调用按真实路径跑一遍。

    图表走真实 Plotly 渲染管线（写 HTML + .plotly.json + 注册），派生数据
    走真实 transform_data（view-only 导出）。这样产物注册、文件落盘、
    会话持久化走的都是生产代码路径，不是测试桩。
    """

    def __init__(self, workspace, settings, **kwargs) -> None:
        self.workspace = workspace

    def run(self, task, history=None, resume_from=None):  # noqa: ARG002
        tools = {tool.name: tool for tool in build_tools(self.workspace)}
        tools["inspect_data"].invoke({"sample_rows": 5})
        tools["create_visualization"].invoke(
            {
                "chart_type": "bar",
                "x": "region",
                "y": "sales",
                "aggregation": "sum",
                "title": "各区销售额",
            }
        )
        tools["transform_data"].invoke(
            {"filter_column": "region", "filter_operator": "eq", "filter_value": "East"}
        )
        return AnalysisResult(
            response="分析完成：东区销售额占比与简报口径见核验。",
            trace=[{"type": "tool_call", "name": "create_visualization", "detail": "{}"}],
            artifacts=[],
            dataset_profile=self.workspace.profile(),
            plan=[],
            completed_steps=[],
        )


@pytest.fixture()
def golden_client(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        api,
        "_effective_settings",
        lambda: AgentSettings(api_key="test", runs_dir=tmp_path / "runs"),
    )
    monkeypatch.setattr(api, "DataAnalysisAgent", RealToolAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", _UPLOAD_CSV, "text/csv")},
    )
    assert upload.status_code == 201
    return client, upload.json()["id"]


def test_golden_path_upload_analyze_audit_confirm_bundle(golden_client):
    client, session_id = golden_client

    # ---- 分析：真实工具链产出图表与视图 ----
    analyze = client.post(f"/api/sessions/{session_id}/analyze", json={"task": "分析各区销售额"})
    assert analyze.status_code == 200, analyze.text

    payload = client.get(f"/api/sessions/{session_id}").json()
    viz = [item for item in payload["artifacts"] if item["kind"] == "visualization"]
    assert viz, "分析完成后产物页必须有图表"
    assert any(item["name"].endswith(".html") for item in viz)
    assert any(item.get("preview_url") for item in viz)

    # 图表预览真的能打开（不是 0 字节 / 404）；preview_url 已是完整路径
    preview = client.get(viz[0]["preview_url"])
    assert preview.status_code == 200
    assert len(preview.content) > 500

    # ---- 模拟服务重启：产物、last_result 必须从磁盘完整恢复 ----
    api.registry._items.pop(session_id, None)
    after_restart = client.get(f"/api/sessions/{session_id}").json()
    viz_after = [item for item in after_restart["artifacts"] if item["kind"] == "visualization"]
    assert viz_after, "重启后产物不能丢（磁盘→注册表恢复）"
    assert after_restart["last_result"] is not None

    # ---- 简报核验：草稿里埋一个错误口径（市场份额无依据）----
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit", json={"draft": _DRAFT}
    )
    assert audit.status_code == 200
    audit_body = audit.json()
    assert audit_body["summary"]["verified"] >= 2
    assert audit_body["summary"]["unsupported"] >= 1, "凭空的市场份额必须被判 unsupported"

    # ---- 重开即所见：核验结果随会话持久化 ----
    last = client.get(f"/api/sessions/{session_id}/sales-audit/last")
    assert last.status_code == 200
    assert last.json()["draft"] == _DRAFT
    assert last.json()["result"]["audit_id"] == audit_body["audit_id"]

    # ---- 确认发布：服务端复算 + 账本 ----
    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": _DRAFT, "audit_id": audit_body["audit_id"]},
    )
    assert confirm.status_code == 200, confirm.text
    receipt = confirm.json()["receipt"]

    # 确认回执并入持久化状态：重开会话仍能看到"已发布"
    api.registry._items.pop(session_id, None)
    last_after = client.get(f"/api/sessions/{session_id}/sales-audit/last").json()
    assert last_after["receipt"]["hash"] == receipt["hash"]

    ledger = client.get(f"/api/sessions/{session_id}/sales-audit/ledger").json()
    assert ledger["verification"]["valid"] is True
    kinds = [entry["kind"] for entry in ledger["entries"]]
    assert "audit" in kinds and "confirm" in kinds

    # ---- 证据包：ZIP 可下载且含账本 ----
    bundle = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle", json={"draft": _DRAFT}
    )
    assert bundle.status_code == 200
    assert bundle.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = archive.namelist()
        assert any("ledger" in name for name in names), names

    # ---- 会话整体导出：证据链完整可带走 ----
    export = client.get(f"/api/sessions/{session_id}/export")
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        names = archive.namelist()
        assert any("session.json" in name for name in names)
        assert any("artifacts" in name for name in names)


def test_golden_path_join_candidate_blocks_silent_adoption(golden_client):
    """黄金路径的分支：join 只产候选，主数据行数不变，采纳必须显式。"""
    client, session_id = golden_client
    rows_before = client.get(f"/api/sessions/{session_id}").json()["profile"]["rows"]

    from data_agent.tools import build_tools

    workspace = api.registry.get(session_id).workspace
    workspace.save_upload("region_map.csv", b"region,manager\nEast,Zhang\nWest,Li\n")
    tools = {tool.name: tool for tool in build_tools(workspace)}
    join_payload = json.loads(
        tools["join_datasets"].invoke(
            {"right_source": "region_map.csv", "left_on": ["region"], "how": "left"}
        )
    )
    assert join_payload["status"] == "ok"
    assert join_payload["adopted"] is False
    assert len(workspace.dataframe) == rows_before, "join 不得改变主数据行数"

    payload = client.get(f"/api/sessions/{session_id}").json()
    assert {item["name"] for item in payload["candidate_datasets"]} == {"joined_data.csv"}

    adopted = client.post(
        f"/api/sessions/{session_id}/datasets/adopt", json={"name": "joined_data.csv"}
    )
    assert adopted.status_code == 200
    assert adopted.json()["after_rows"] == rows_before
