"""结构化输出必须绕开 tool_choice（thinking 模式会拒绝它）。

线上实测：`model.with_structured_output(Schema)` 走 function calling 并强制
tool_choice，DeepSeek thinking 模式直接返回
    400 Thinking mode does not support this tool_choice
规划节点捕获异常后静默退回"默认计划"——分析照样跑完，但模型从未参与规划，
只有日志里的 exception 知道。这里用假模型锁住三条契约：
  1. 走 bind_tools（不强制 tool_choice）的路径能正常解析；
  2. 模型把 JSON 写在正文里时也能解析；
  3. 两者都没有时抛异常（由调用方决定降级，且降级必须可见）。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from data_agent.agent import _extract_json_object, _ToolSchemaRunnable


class _Plan(BaseModel):
    objective: str
    steps: list[str]


class _FakeModel:
    """记录 bind_tools 的调用参数，并按脚本返回消息。"""

    def __init__(self, message: Any) -> None:
        self.message = message
        self.bound_tools: list[Any] = []
        self.tool_choice: Any = "unset"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        self.bound_tools = list(tools)
        self.tool_choice = kwargs.get("tool_choice", "auto(默认，未显式传)")
        return self

    def invoke(self, prompt, config=None):  # noqa: ANN001, ARG002
        return self.message


class _Message:
    def __init__(self, content: str = "", tool_calls: list[dict] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


def _runnable(message: _Message) -> tuple[_ToolSchemaRunnable, _FakeModel]:
    model = _FakeModel(message)
    return _ToolSchemaRunnable(model, _Plan, logger=logging.getLogger("test")), model


def test_uses_bind_tools_without_forcing_tool_choice():
    """关键点：绝不能显式传 tool_choice（thinking 模式会 400）。"""
    runnable, model = _runnable(_Message(tool_calls=[
        {"name": "_Plan", "args": {"objective": "目标", "steps": ["a", "b"]}},
    ]))
    plan = runnable.invoke("prompt")
    assert isinstance(plan, _Plan) and plan.steps == ["a", "b"]
    assert model.bound_tools == [_Plan]
    assert model.tool_choice == "auto(默认，未显式传)", "不应显式设置 tool_choice"


def test_falls_back_to_json_in_content():
    runnable, _ = _runnable(_Message(content='好的：\n```json\n{"objective": "O", "steps": ["x"]}\n```'))
    plan = runnable.invoke("prompt")
    assert plan.objective == "O" and plan.steps == ["x"]


def test_raises_when_nothing_parsable():
    runnable, _ = _runnable(_Message(content="我不会返回 JSON"))
    with pytest.raises(ValueError, match="既未调用"):
        runnable.invoke("prompt")


def test_tool_call_without_args_is_not_treated_as_success():
    """有 tool_calls 但 args 为空时要继续尝试正文解析，而不是当成空计划。"""
    runnable, _ = _runnable(_Message(content='{"objective": "O", "steps": []}',
                                     tool_calls=[{"name": "_Plan", "args": {}}]))
    plan = runnable.invoke("prompt")
    assert plan.objective == "O"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('前言 {"a": {"b": 2}} 后记', {"a": {"b": 2}}),
        ('{"a": "含 } 的字符串"}', {"a": "含 } 的字符串"}),
        ('{"broken": ', None),
        ("没有 JSON", None),
        ("", None),
    ],
)
def test_extract_json_object(text: str, expected):
    assert _extract_json_object(text) == expected
