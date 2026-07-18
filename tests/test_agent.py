from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from data_agent.agent import DataAnalysisAgent, _fallback_plan, create_chat_model
from data_agent.config import AgentSettings


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
    plan = _fallback_plan("只检查数据质量并总结，不修改数据，不生成图表")
    assert [step.id for step in plan.steps] == ["inspect"]


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
