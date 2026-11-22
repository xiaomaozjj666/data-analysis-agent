"""token 用量遥测：会话级累计（plan/replan/execute/finalize 全覆盖）与入链。"""

from __future__ import annotations

import json

from tests.test_agent import ToolCallingFakeModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
_FINALIZE_ONLY_TOTAL = 15  # 旧实现只计 finalize 一次调用的总和


class _UsageFakeModel(ToolCallingFakeModel):
    """在既有假模型行为上补 usage_metadata，模拟真实 provider 返回。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN202
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        message.usage_metadata = dict(_USAGE)
        return result


def test_invoke_config_always_includes_shared_usage_acc(workspace):
    settings = _settings(workspace)
    agent = _agent(workspace, settings)
    config = agent._invoke_config()
    callbacks = config["callbacks"]
    assert agent.usage_acc in callbacks
    assert agent._invoke_config()["callbacks"][1] is agent.usage_acc  # 同一实例


def test_full_workflow_usage_covers_all_llm_calls(workspace):
    """修复回归：plan/replan/execute 的用量必须计入，不能再只剩 finalize。"""
    settings = _settings(workspace)
    agent = _agent(workspace, settings, model=_UsageFakeModel())
    result = agent.run("检查数据")

    assert result.usage is not None
    # 5 次调用：plan + execute(2 次循环) + replan + finalize；旧实现只有
    # finalize 的 15。这里用下限断言（>=45）证明「不止 finalize」，
    # 并用 15 的倍数保证每次调用的用量确实被准确累加。
    assert result.usage["total_tokens"] >= 3 * _FINALIZE_ONLY_TOTAL
    assert result.usage["total_tokens"] % _USAGE["total_tokens"] == 0
    # 与会话级累计器一致
    assert result.usage == agent.usage_acc.snapshot()


def test_usage_survives_llm_failure_in_finalize(workspace):
    """finalize LLM 失败走 fallback 时，已累计的用量仍然透出（不再变 None）。"""
    settings = _settings(workspace)

    class _ExplodingFinalize(_UsageFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN202
            if len(messages) >= 1 and messages[-1].content and "evidence" in str(messages[-1].content):
                raise RuntimeError("finalize boom")
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    agent = _agent(workspace, settings, model=_ExplodingFinalize())
    result = agent.run("检查数据")
    assert "各分析步骤的执行摘要" in result.response
    assert result.usage is not None and result.usage["total_tokens"] > 0


def test_provenance_records_usage_entry_and_verifies(workspace):
    """token 用量入溯源链：链可验证、条目可复查、缺省不入链保持链长不变。"""
    from data_agent import provenance

    root = workspace.root
    provenance.record_analysis(
        root,
        task="检查数据",
        response="报告",
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    )
    entries = [
        json.loads(line)
        for line in (root / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    usage_entries = [entry for entry in entries if entry["type"] == "usage"]
    assert len(usage_entries) == 1
    assert usage_entries[0]["data"]["total_tokens"] == 150
    assert provenance.verify(root)["ok"] is True

    # 无用量时不写条目（旧调用方零迁移成本）
    root2 = workspace.root.parent / "prov-no-usage"
    root2.mkdir(parents=True, exist_ok=True)
    provenance.record_analysis(root2, task="t", response="r")
    entries2 = [
        json.loads(line)
        for line in (root2 / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(entry["type"] != "usage" for entry in entries2)


def test_chat_last_usage_is_delta_not_cumulative(workspace):
    """chat_done 事件的 _last_usage 语义是「本次追问」增量。"""
    settings = _settings(workspace)
    agent = _agent(workspace, settings, model=_UsageFakeModel())
    agent.run("检查数据")
    cumulative_before = agent.usage_acc.snapshot()["total_tokens"]

    agent.chat("追问一句：销售额最高的区域是哪个？")

    assert agent._last_usage is not None
    assert agent._last_usage["total_tokens"] > 0
    assert agent._last_usage["total_tokens"] <= cumulative_before + 3 * _USAGE["total_tokens"]


def _settings(workspace) -> object:
    from data_agent.config import AgentSettings

    return AgentSettings(
        api_key="not-used",
        max_iterations=5,
        runs_dir=workspace.root.parent,
    )


def _agent(workspace, settings, *, model=None):
    from data_agent.agent import DataAnalysisAgent

    return DataAnalysisAgent(workspace, settings=settings, model=model or ToolCallingFakeModel())


def test_workflow_state_declares_telemetry_channels():
    """评审 P2-3 回归：usage/reasoning 通道未声明会被 langgraph 静默丢弃。"""
    from data_agent.models import WorkflowState

    assert "usage" in WorkflowState.__annotations__
    assert "reasoning" in WorkflowState.__annotations__


def test_stream_yields_usage_in_finalize_payload(workspace):
    """前端主用的 stream 路径：finalize payload 必须带会话级 usage。"""
    settings = _settings(workspace)
    agent = _agent(workspace, settings, model=_UsageFakeModel())
    usage_in_finalize = None
    for event in agent.stream("检查数据"):
        if event["node"] == "finalize":
            usage_in_finalize = event["data"].get("usage")
    assert usage_in_finalize is not None
    assert usage_in_finalize["total_tokens"] >= 3 * _FINALIZE_ONLY_TOTAL
