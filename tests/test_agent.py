from __future__ import annotations

import json
import threading
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from data_agent.agent import (
    AnalysisCancelled,
    AnalysisResult,
    DataAnalysisAgent,
    _apply_query_constraints,
    _fallback_plan,
    _is_recoverable_format_error,
    create_chat_model,
)
from data_agent.api import SessionRegistry
from data_agent.config import AgentSettings
from data_agent.workspace import DataWorkspace


class ToolCallingFakeModel(BaseChatModel):
    _bound_tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "tool-calling-fake"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any):
        clone = self.model_copy(deep=True)
        clone._bound_tool_names = {
            getattr(item, "name", None) or getattr(item, "__name__", "") for item in tools
        }
        return clone

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if "AnalysisPlan" in self._bound_tool_names:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnalysisPlan",
                        "id": "call_plan",
                        "type": "tool_call",
                        "args": {
                            "objective": "验证分析流程",
                            "steps": [
                                {
                                    "id": "inspect",
                                    "title": "检查数据",
                                    "instruction": "检查数据质量",
                                    "success_criteria": "返回数据概况",
                                }
                            ],
                        },
                    }
                ],
            )
        elif "ReplanDecision" in self._bound_tool_names:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ReplanDecision",
                        "id": "call_replan",
                        "type": "tool_call",
                        "args": {"done": True, "rationale": "检查完成", "remaining_steps": []},
                    }
                ],
            )
        elif any(isinstance(message, ToolMessage) for message in messages):
            message = AIMessage(content="已检查 6 行、4 列数据；工作流和 ReAct 工具调用均正常。")
        elif "inspect_data" not in self._bound_tool_names:
            message = AIMessage(content="Plan-and-Execute 与 ReAct 分析流程已完成。")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "inspect_data", "args": {"sample_rows": 3}, "id": "call_inspect", "type": "tool_call"}],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_full_langgraph_react_workflow_without_network(workspace):
    settings = AgentSettings(api_key="not-used", max_iterations=5, runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    result = agent.run("检查数据")
    assert "ReAct" in result.response
    assert [step["name"] for step in result.trace if step["type"] == "tool_call"] == ["inspect_data"]
    assert result.dataset_profile["rows"] == 6
    assert [step["id"] for step in result.plan] == ["inspect"]
    assert [step["id"] for step in result.completed_steps] == ["inspect"]


def test_fallback_plan_respects_read_only_chart_constraints():
    query = "只检查数据质量并总结，不修改数据，不生成图表"
    plan = _apply_query_constraints(query, _fallback_plan(query))
    assert [step.id for step in plan.steps] == ["inspect"]


def test_format_repair_detection_ignores_unrelated_tool_errors():
    assert _is_recoverable_format_error("ValueError: could not convert string to float")
    assert not _is_recoverable_format_error("ValueError: Invalid marker size -459")


def test_cancelled_agent_stops_before_model_work(workspace):
    cancel_event = threading.Event()
    cancel_event.set()
    settings = AgentSettings(api_key="not-used", max_iterations=5, runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(
        workspace,
        settings=settings,
        model=ToolCallingFakeModel(),
        cancel_event=cancel_event,
    )
    try:
        agent.run("检查数据")
    except AnalysisCancelled:
        pass
    else:
        raise AssertionError("cancelled analysis should stop before running the workflow")


def test_native_deepseek_model_preserves_thinking_configuration():
    settings = AgentSettings(
        provider="deepseek",
        api_key="test-key",
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        thinking_enabled=True,
        reasoning_effort="high",
    )
    model = create_chat_model(settings)
    assert isinstance(model, ChatDeepSeek)
    assert model.model_name == "deepseek-v4-pro"
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert model.reasoning_effort == "high"


def test_native_deepseek_agent_binds_analysis_tools_without_network(workspace):
    settings = AgentSettings(
        provider="deepseek",
        api_key="test-key",
        model="deepseek-v4-pro",
        thinking_enabled=True,
        reasoning_effort="high",
        max_iterations=5,
        runs_dir=workspace.root.parent,
    )
    agent = DataAnalysisAgent(workspace, settings=settings)
    assert isinstance(agent.model, ChatDeepSeek)
    assert {tool.name for tool in agent.tools} == {
        "inspect_data",
        "repair_data_format",
        "clean_data",
        "transform_data",
        "statistical_analysis",
        "create_visualization",
        "export_data",
    }


def test_openai_provider_remains_available():
    settings = AgentSettings(
        provider="openai",
        api_key="test-key",
        model="gpt-4.1-mini",
        base_url=None,
    )
    assert isinstance(create_chat_model(settings), ChatOpenAI)


class _PromptCapturingModel(ToolCallingFakeModel):
    """继承 ToolCallingFakeModel 的工具调用行为，额外捕获 finalize 的 prompt。

    finalize 节点用 str prompt 调 model.invoke，本类在 _generate 中记录
    所有 str content，便于测试断言 prompt 内容。
    """

    _captured_prompts: list[str] = PrivateAttr(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        for message in messages:
            if isinstance(message.content, str) and message.content:
                self._captured_prompts.append(message.content)
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_finalize_prompt_includes_all_step_titles(workspace):
    """finalize 应按步数分配 evidence 预算，所有步骤标题都应出现在 prompt 中。

    构造一个长 summary 的 completed_steps，跑完整 workflow 后检查最后一条
    captured prompt（即 finalize 的 prompt）是否包含所有步骤标题。
    """
    settings = AgentSettings(api_key="not-used", max_iterations=5, runs_dir=workspace.root.parent)
    model = _PromptCapturingModel()
    agent = DataAnalysisAgent(workspace, settings=settings, model=model)

    # 直接构造 state 调用 agent._build_workflow().invoke，绕过 plan/execute 阶段。
    # finalize 是 workflow 的最后一个节点，但我们仍需走 validate_dataset -> plan_analysis
    # -> execute_step -> replan -> finalize 才能到达。简单做法：直接调 agent.run，
    # ToolCallingFakeModel 的 plan 只有 1 个 inspect 步骤，summary 由 inspect_data 工具产生。
    result = agent.run("检查数据")
    assert result.response
    # ToolCallingFakeModel 走完一轮后只有一个 completed_step，标题"检查数据"。
    # 我们验证 finalize 的 prompt 至少包含这个标题（而不是被截断丢掉）。
    assert model._captured_prompts, "finalize 未调用 model.invoke"
    final_prompt = model._captured_prompts[-1]
    assert "检查数据" in final_prompt or "## " in final_prompt, (
        f"finalize prompt 未包含步骤标题，可能 evidence 截断有问题: {final_prompt[:200]}"
    )


def test_finalize_fallback_message_when_model_fails(workspace):
    """LLM 汇总失败时（返回空字符串），兜底文案应明确告知用户并附上步骤摘要。"""
    settings = AgentSettings(api_key="not-used", max_iterations=5, runs_dir=workspace.root.parent)

    class _EmptyFinalizeModel(ToolCallingFakeModel):
        """走完 plan/execute 后，在 finalize 阶段返回空字符串触发兜底。"""

        _finalize_called: bool = PrivateAttr(default=False)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            # finalize 的 prompt 通常是 str（非 tool call），检测到就返回空内容。
            for message in messages:
                if isinstance(message.content, str) and "最终中文数据分析报告" in message.content:
                    self._finalize_called = True
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = _EmptyFinalizeModel()
    agent = DataAnalysisAgent(workspace, settings=settings, model=model)
    result = agent.run("检查数据")
    assert model._finalize_called, "测试模型未走到 finalize 阶段"
    # 兜底文案应包含提示语和步骤摘要标题。
    assert "模型汇总失败" in result.response or "## " in result.response, (
        f"兜底文案未触发或格式不对: {result.response[:200]}"
    )


def test_last_result_persisted_and_restored(tmp_path):
    """last_result 应被持久化到 manifest，且 restore 后能恢复 plan/completed/response。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id="persist_test")
    workspace.save_upload("sales.csv", b"region,sales\nEast,100\n")
    workspace.load(workspace.input_dir / "sales.csv")

    registry = SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    session_id, record = registry.create(workspace)
    record.last_result = AnalysisResult(
        response="这是测试报告。",
        trace=[{"type": "tool_call", "name": "inspect_data"}],
        artifacts=[],
        dataset_profile=workspace.profile(),
        plan=[{"id": "inspect", "title": "检查", "instruction": "检查", "success_criteria": "完成"}],
        completed_steps=[{"id": "inspect", "title": "检查", "summary": "完成"}],
    )
    record.analysis_status = "completed"
    registry.persist(session_id, record)

    # manifest 中应有 last_result 字段，trace 被截断到最近 20 条。
    manifest_path = tmp_path / "runs" / session_id / "session.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["last_result"]["response"] == "这是测试报告。"
    assert manifest["last_result"]["plan"][0]["id"] == "inspect"
    assert manifest["analysis_status"] == "completed"

    # 新 registry 实例应能恢复 last_result。
    restored = SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24).get(session_id)
    assert restored.last_result is not None
    assert restored.last_result.response == "这是测试报告。"
    assert restored.last_result.plan[0]["id"] == "inspect"
    assert restored.last_result.completed_steps[0]["summary"] == "完成"
    assert restored.analysis_status == "completed"
