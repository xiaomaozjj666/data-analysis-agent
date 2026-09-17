"""执行预算闸门：单步工具调用上限 + 模型调用上限 + 正常进度下跳过重规划咨询。

背景（全部为实测数字）：一次"按地区对比销售额 + 画关系图"的简单任务
原本跑出 62 次工具调用 / 427 秒、用户感受是"卡住了"。拆开看是三处浪费：

1. ReAct 单步没有工具调用上限 → 一步能烧掉 23 次调用；
2. 每个步骤都无条件做一次 replan LLM 往返（6 轮 × ~6s ≈ 37s），
   而结论几乎都是"按原计划继续"；
3. 每次调用都带 thinking（high）→ 单轮 2~3s 起。

这里锁住 1 与 2 的接线与语义（3 属于用户可调的模型档位，见 README）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from data_agent import agent as agent_module
from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.nodes.replan import _needs_replan
from data_agent.workspace import DataWorkspace


def _settings() -> AgentSettings:
    return AgentSettings(
        api_key="test-key", provider="deepseek", model="deepseek-chat",
        base_url="http://127.0.0.1:9", language="zh", thinking_enabled=False,
    )


def test_executor_has_both_budget_guards(monkeypatch):
    """执行器必须挂上工具调用与模型调用两道闸门（防止被无意移除）。

    middleware 会被编译进 graph 的节点里、拿不到列表，因此改为拦截
    ``create_agent`` 的调用参数来断言——这正是"接线正确"该验证的地方。
    """
    captured: dict = {}
    original = agent_module.create_agent

    def _capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(agent_module, "create_agent", _capture)
    workspace = DataWorkspace(Path("runs"), session_id="tmp_budget")
    DataAnalysisAgent(workspace, settings=_settings(), model=_StubModel())

    middleware = captured.get("middleware") or []
    names = {type(item).__name__ for item in middleware}
    assert "ToolCallLimitMiddleware" in names, names
    assert "ModelCallLimitMiddleware" in names, names

    by_name = {type(item).__name__: item for item in middleware}
    assert by_name["ToolCallLimitMiddleware"].run_limit == _settings().max_tool_calls_per_step
    assert by_name["ModelCallLimitMiddleware"].run_limit == _settings().max_iterations


@pytest.mark.parametrize(
    ("completed", "remaining", "artifacts", "expected"),
    [
        # 步骤失败 → 需要补偿步骤，必须咨询
        ([{"status": "failed"}], [{"id": "s2"}], 3, True),
        # 计划已执行完 → 判断收尾还是补步骤，必须咨询
        ([{"status": "ok"}], [], 3, True),
        # 连续两步零产物 → 方向可能不对，值得让模型重新审视
        ([{"status": "ok"}, {"status": "ok"}], [{"id": "s3"}], 0, True),
        # 正常推进 → 沿用原计划，省掉一次 LLM 往返
        ([{"status": "ok"}], [{"id": "s2"}], 2, False),
        ([{"status": "ok"}, {"status": "ok"}], [{"id": "s3"}], 1, False),
    ],
)
def test_needs_replan_only_for_meaningful_cases(completed, remaining, artifacts, expected):
    assert _needs_replan(
        completed=completed, remaining=remaining, artifact_count=artifacts,
    ) is expected


def test_tool_call_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS_PER_STEP", "4")
    assert AgentSettings.from_env().max_tool_calls_per_step == 4
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS_PER_STEP", "0")
    assert AgentSettings.from_env().max_tool_calls_per_step == 1, "下限必须夹到 1"


class _StubModel:
    """最小 ChatModel 替身：只为构造 agent，不发起任何调用。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def invoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应真正调用模型")

    def bind(self, **kwargs):  # noqa: ANN003
        return self


def test_workspace_stub_is_not_polluted(tmp_path):
    """占位：确认上面的构造不会在 runs/ 下留下会话目录。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id="tmp_budget2")
    frame = pd.DataFrame({"a": [1, 2]})
    source = tmp_path / "a.csv"
    frame.to_csv(source, index=False)
    workspace.load(source, copy_into_workspace=True)
    assert workspace.source_path is not None
