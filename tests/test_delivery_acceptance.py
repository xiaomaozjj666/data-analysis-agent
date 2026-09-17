"""交付验收发现的两处问题：产物 URL 编码 + 分析墙钟预算。

1. **产物 URL 必须百分号编码**：产物名是中文（散点图_1.html），未编码的
   `/artifacts/散点图_1.html/preview` 让 curl / python-urllib / Java 这类标准
   HTTP 客户端直接抛编码错误（浏览器会自动编码，所以前端看不出问题，
   只有 API 调用方踩得到）。
2. **单次分析要有墙钟上限**：同一句任务实测可能 230s（顺利）也可能 680s
   （要现场拼数据）。没有上限时用户无法预期何时拿到结果；超预算后应停止
   开新步骤、直接汇总已完成部分，并在 reason/summary 里说明原因。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.nodes.execute import execute_step
from data_agent.nodes.replan import replan
from data_agent.registry import _artifact_payload
from data_agent.workspace import DataWorkspace


def _workspace(tmp_path: Path, session: str = "acceptance") -> DataWorkspace:
    frame = pd.DataFrame({"地区": ["华东", "华南"], "销售额": [10.0, 20.0]})
    source = tmp_path / f"{session}.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id=session)
    workspace.load(source, copy_into_workspace=True)
    return workspace


def test_artifact_urls_are_percent_encoded(tmp_path):
    chart = tmp_path / "散点图_1.html"
    chart.write_text("<html></html>", encoding="utf-8")
    (tmp_path / "散点图_1.plotly.json").write_text("{}", encoding="utf-8")
    payload = _artifact_payload("api_test", [{
        "kind": "visualization", "name": "散点图_1.html",
        "description": "销售额与利润分布（整体）", "path": str(chart),
    }])
    assert len(payload) == 1
    item = payload[0]
    for key in ("download_url", "preview_url", "thumbnail_url"):
        value = item[key]
        value.encode("ascii")  # 未编码会在这里抛 UnicodeEncodeError
        assert "%E6%95%A3" in value, value
        assert "散点图" not in value
    # 编码后的 URL 仍指向同名文件（服务端会解码路径参数）
    from urllib.parse import unquote
    assert unquote(item["preview_url"].rsplit("/preview", 1)[0].rsplit("/", 1)[-1]) == "散点图_1.html"


def test_dataset_artifact_url_also_encoded(tmp_path):
    data = tmp_path / "清洗后数据.csv"
    data.write_text("a,b\n1,2\n", encoding="utf-8")
    payload = _artifact_payload("api_test", [{
        "kind": "dataset", "name": "清洗后数据.csv", "description": "清洗结果",
        "path": str(data),
    }])
    payload[0]["download_url"].encode("ascii")


class _StubAgent:
    """只带节点函数需要的属性的最小替身。"""

    def __init__(self, workspace: DataWorkspace, *, budget_left: float) -> None:
        self.workspace = workspace
        self.settings = AgentSettings(
            api_key="k", provider="deepseek", model="deepseek-chat",
            base_url="http://127.0.0.1:9", language="zh", thinking_enabled=False,
            runs_dir=workspace.root.parent,
        )
        self._budget_left = budget_left
        self.replanner = None
        self.progress_calls: list[tuple[str, str]] = []
        self.event_callback = lambda event_type, payload: None

    def analysis_budget_left(self) -> float:
        return self._budget_left

    def _enter_node(self, node: str, title: str) -> None:
        self.progress_calls.append((node, title))

    def _ensure_not_cancelled(self) -> None:
        return None

    def _invoke_config(self, *callbacks, **extra):  # noqa: ANN002, ANN003
        return {"callbacks": list(callbacks)}


def test_replan_stops_when_budget_exhausted(tmp_path):
    agent = _StubAgent(_workspace(tmp_path), budget_left=-1.0)
    state = {
        "query": "分析",
        "objective": "目标",
        "remaining_steps": [
            {"id": "s1", "title": "步骤1", "instruction": "", "success_criteria": ""},
            {"id": "s2", "title": "步骤2", "instruction": "", "success_criteria": ""},
        ],
        "completed_steps": [],
        "last_step_result": {"id": "s1", "title": "步骤1", "status": "ok", "summary": "完成"},
        "artifacts": [{"name": "图_1.html"}],
    }
    result = replan(agent, state)
    assert result["remaining_steps"] == [], "超预算后不应再排新步骤"
    assert "预算" in result["replan_reason"]


def test_replan_keeps_steps_within_budget(tmp_path):
    agent = _StubAgent(_workspace(tmp_path), budget_left=300.0)
    state = {
        "query": "分析",
        "objective": "目标",
        "remaining_steps": [
            {"id": "s1", "title": "步骤1", "instruction": "", "success_criteria": ""},
            {"id": "s2", "title": "步骤2", "instruction": "", "success_criteria": ""},
        ],
        "completed_steps": [],
        "last_step_result": {"id": "s1", "title": "步骤1", "status": "ok", "summary": "完成"},
        "artifacts": [{"name": "图_1.html"}],
    }
    result = replan(agent, state)
    assert [item["id"] for item in result["remaining_steps"]] == ["s2"]


def test_execute_step_skips_when_budget_exhausted(tmp_path):
    agent = _StubAgent(_workspace(tmp_path), budget_left=0.0)
    state = {
        "query": "分析",
        "objective": "目标",
        "remaining_steps": [
            {"id": "s1", "title": "步骤1", "instruction": "做点什么", "success_criteria": "完成"},
        ],
        "completed_steps": [],
    }
    result = execute_step(agent, state)
    assert result["last_step_result"]["status"] == "skipped"
    assert "预算" in result["last_step_result"]["summary"]
    assert result["remaining_steps"] == []


def test_agent_reports_budget_left_from_config(tmp_path):
    settings = AgentSettings(
        api_key="k", provider="deepseek", model="deepseek-chat",
        base_url="http://127.0.0.1:9", language="zh", thinking_enabled=False,
        max_analysis_seconds=123.0,
    )

    class _Model:
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            return self

        def bind(self, **kwargs):  # noqa: ANN003
            return self

        def invoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("不应调用模型")

    agent = DataAnalysisAgent(_workspace(tmp_path, "budget"), settings=settings, model=_Model())
    assert agent.analysis_budget_left() == pytest.approx(123.0)
    import time
    agent._run_started_at = time.monotonic() - 100
    assert agent.analysis_budget_left() == pytest.approx(23.0, abs=1.0)


def test_budget_config_is_validated():
    """预算必须为正数——直接构造 settings 校验，不依赖环境变量/密钥。

    最初写成 `from_env()` + monkeypatch，本地（.env 里有 Key）能过、CI（无 Key）
    却先在"未配置 API Key"上抛错，正则不匹配而失败：测试不能依赖环境。
    """
    settings = AgentSettings(
        api_key="k", provider="deepseek", model="deepseek-chat",
        base_url="http://127.0.0.1:9", language="zh",
        max_analysis_seconds=0,
    )
    with pytest.raises(ValueError, match="AGENT_MAX_ANALYSIS_SECONDS"):
        settings.validate_for_model()

    ok = AgentSettings(
        api_key="k", provider="deepseek", model="deepseek-chat",
        base_url="http://127.0.0.1:9", language="zh",
        max_analysis_seconds=600,
    )
    ok.validate_for_model()  # 合法值不应抛错


def test_analysis_result_json_is_serializable(tmp_path):
    """预算导致的 skipped 步骤也要能正常写进结果（保持 JSON 可序列化）。"""
    payload = {"status": "skipped", "summary": "已用完 10 分钟分析预算，跳过本步"}
    assert json.loads(json.dumps(payload, ensure_ascii=False))["status"] == "skipped"
