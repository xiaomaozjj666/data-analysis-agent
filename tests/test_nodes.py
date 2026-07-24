"""Direct unit tests for LangGraph node functions.

Each test calls a node function (validate_dataset, plan_analysis, execute_step,
replan, finalize) directly with a constructed WorkflowState and a FakeAgent
mock, covering edge cases not exercised by the integration tests in
test_agent.py. The LLM is always mocked — no real API calls are made.
"""

from __future__ import annotations

import threading
from typing import Any

import pandas as pd
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from data_agent.config import AgentSettings
from data_agent.models import AnalysisCancelled, AnalysisPlan, PlanStep, ReplanDecision
from data_agent.nodes.execute import execute_step
from data_agent.nodes.finalize import finalize
from data_agent.nodes.graph import route_after_review
from data_agent.nodes.plan import plan_analysis
from data_agent.nodes.replan import replan
from data_agent.nodes.validate import validate_dataset
from data_agent.prompts import get_prompts
from data_agent.workspace import DataWorkspace

# ---------------------------------------------------------------------------
# Test helpers: mock runnable, fake agent, workspace factory
# ---------------------------------------------------------------------------


class _MockRunnable:
    """Mock LangChain runnable that returns a predefined response or raises.

    Tracks invoke calls so tests can assert the LLM was (or was not) called
    and inspect the inputs passed to it.
    """

    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.invoke_count = 0
        self.captured_inputs: list[Any] = []

    def invoke(self, input: Any, config: dict | None = None, **kwargs: Any) -> Any:
        self.invoke_count += 1
        self.captured_inputs.append(input)
        if self._exc is not None:
            raise self._exc
        return self._response


class FakeAgent:
    """Minimal agent mock for direct node function unit tests.

    Provides exactly the attributes and methods that node functions access on
    DataAnalysisAgent, without constructing a real agent (no LLM, no tools,
    no graph). Each LLM-facing runnable (planner / replanner / react_agent /
    model) is a _MockRunnable whose response can be configured per test.
    """

    def __init__(
        self,
        workspace: DataWorkspace,
        *,
        settings: AgentSettings | None = None,
        planner: _MockRunnable | None = None,
        replanner: _MockRunnable | None = None,
        react_agent: _MockRunnable | None = None,
        model: _MockRunnable | None = None,
        cancel_event: threading.Event | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.settings = settings or AgentSettings(
            api_key="not-used",
            runs_dir=workspace.root.parent,
            max_iterations=5,
            max_plan_steps=8,
        )
        self.prompts = get_prompts(self.settings.language)
        self.cancel_event = cancel_event or threading.Event()
        self.event_callback: Any = lambda event_type, payload: None
        self.progress_callback: Any = lambda node, title: None
        self.tools = tools or []
        self.planner = planner or _MockRunnable()
        self.replanner = replanner or _MockRunnable()
        self.react_agent = react_agent or _MockRunnable()
        self.model = model or _MockRunnable()
        self.entered_nodes: list[tuple[str, str]] = []

    def _ensure_not_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise AnalysisCancelled("分析已取消。")

    def _enter_node(self, node: str, title: str) -> None:
        self.entered_nodes.append((node, title))

    def _invoke_config(self, *extra_callbacks: Any, **extra: Any) -> dict[str, Any]:
        return {"callbacks": list(extra_callbacks), **extra}


def _make_workspace(tmp_path: Any, session_id: str = "node_test") -> DataWorkspace:
    """Create an isolated DataWorkspace with 6 rows of test data.

    Each test gets its own workspace under tmp_path / "runs" to guarantee
    independence (no shared mutable state across tests).
    """
    data = pd.DataFrame(
        {
            "region": ["East", "West", "East", "West", "East", "East"],
            "sales": [100.0, 200.0, 120.0, 230.0, None, 100.0],
            "profit": [10.0, 32.0, 14.0, 40.0, 12.0, 10.0],
            "category": ["A", "B", "A", "B", "A", "A"],
        }
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    workspace = DataWorkspace(runs_dir, session_id=session_id)
    source = tmp_path / f"{session_id}.csv"
    data.to_csv(source, index=False)
    workspace.load(source, copy_into_workspace=True)
    return workspace


# ---------------------------------------------------------------------------
# validate_dataset
# ---------------------------------------------------------------------------


def test_validate_dataset_returns_profile_when_workspace_has_data(tmp_path):
    workspace = _make_workspace(tmp_path, "validate_has_data")
    agent = FakeAgent(workspace)
    state = {"query": "检查数据", "input_messages": [HumanMessage(content="检查数据")]}

    result = validate_dataset(agent, state)

    assert result["dataset_profile"]["rows"] == 6
    assert result["dataset_profile"]["columns"] == 4
    assert len(result["input_messages"]) == 1
    assert result["artifacts"] == []
    assert result["trace"] == []
    # Resume fields are preserved (empty when not resuming).
    assert result["completed_steps"] == []
    assert result["plan"] == []
    assert result["remaining_steps"] == []
    assert result["objective"] == ""


def test_validate_dataset_creates_input_message_from_query_when_missing(tmp_path):
    workspace = _make_workspace(tmp_path, "validate_no_msgs")
    agent = FakeAgent(workspace)
    state = {"query": "分析销售趋势"}

    result = validate_dataset(agent, state)

    # When input_messages is absent, the node creates a HumanMessage from the
    # query so downstream nodes have context.
    assert len(result["input_messages"]) == 1
    assert isinstance(result["input_messages"][0], HumanMessage)
    assert result["input_messages"][0].content == "分析销售趋势"


def test_validate_dataset_handles_empty_workspace_gracefully(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    workspace = DataWorkspace(runs_dir, session_id="validate_empty")
    # Simulate a header-only CSV: columns exist but zero rows.
    workspace.dataframe = pd.DataFrame(
        {"col1": pd.Series([], dtype="object"), "col2": pd.Series([], dtype="int64")}
    )
    agent = FakeAgent(workspace)
    state = {"query": "检查数据"}

    result = validate_dataset(agent, state)

    assert result["dataset_profile"]["rows"] == 0
    assert result["dataset_profile"]["columns"] == 2


# ---------------------------------------------------------------------------
# plan_analysis
# ---------------------------------------------------------------------------


def test_plan_analysis_generates_plan_from_llm_response(tmp_path):
    workspace = _make_workspace(tmp_path, "plan_ok")
    plan = AnalysisPlan(
        objective="测试分析目标",
        steps=[
            PlanStep(
                id="inspect",
                title="检查数据",
                instruction="检查字段与缺失",
                success_criteria="返回数据概况",
            ),
            PlanStep(
                id="analyze",
                title="统计分析",
                instruction="描述统计",
                success_criteria="返回指标",
            ),
        ],
    )
    agent = FakeAgent(workspace, planner=_MockRunnable(response=plan))
    state = {"query": "分析数据", "dataset_profile": {"rows": 6, "columns": 4, "column_info": []}}

    result = plan_analysis(agent, state)

    assert result["objective"] == "测试分析目标"
    assert len(result["plan"]) == 2
    # Each step must have the PlanStep structure (id / title / instruction /
    # success_criteria).
    step = result["plan"][0]
    assert step["id"] == "inspect"
    assert step["title"] == "检查数据"
    assert step["instruction"] == "检查字段与缺失"
    assert step["success_criteria"] == "返回数据概况"
    # remaining_steps mirrors plan for execute_step to consume.
    assert result["remaining_steps"] == result["plan"]


def test_plan_analysis_falls_back_when_planner_fails(tmp_path):
    workspace = _make_workspace(tmp_path, "plan_fallback")
    # Planner raises — simulating invalid JSON or a malformed structured output.
    agent = FakeAgent(workspace, planner=_MockRunnable(exc=ValueError("invalid JSON")))
    state = {"query": "分析销售数据", "dataset_profile": {"rows": 6, "columns": 4, "column_info": []}}

    result = plan_analysis(agent, state)

    # _fallback_plan produces 4 steps: inspect, prepare, analyze, visualize.
    step_ids = [step["id"] for step in result["plan"]]
    assert step_ids == ["inspect", "prepare", "analyze", "visualize"]
    # The objective template includes the user query.
    assert "分析销售数据" in result["objective"]
    # Each step has the required PlanStep fields.
    for step in result["plan"]:
        assert {"id", "title", "instruction", "success_criteria"} <= set(step.keys())


def test_plan_analysis_resume_reuses_existing_plan_without_llm_call(tmp_path):
    workspace = _make_workspace(tmp_path, "plan_resume")
    agent = FakeAgent(workspace, planner=_MockRunnable())  # should not be invoked
    existing_plan = [
        {"id": "inspect", "title": "检查", "instruction": "检查", "success_criteria": "完成"},
        {"id": "analyze", "title": "分析", "instruction": "分析", "success_criteria": "结果"},
    ]
    existing_completed = [{"id": "inspect", "title": "检查", "summary": "已完成"}]
    state = {
        "query": "分析数据",
        "dataset_profile": {"rows": 6, "columns": 4, "column_info": []},
        "plan": existing_plan,
        "completed_steps": existing_completed,
        "objective": "已有目标",
    }

    result = plan_analysis(agent, state)

    assert agent.planner.invoke_count == 0
    assert result["plan"] == existing_plan
    assert result["objective"] == "已有目标"
    # remaining = plan minus completed_ids -> only "analyze" remains.
    assert len(result["remaining_steps"]) == 1
    assert result["remaining_steps"][0]["id"] == "analyze"


# ---------------------------------------------------------------------------
# execute_step
# ---------------------------------------------------------------------------


def test_execute_step_returns_trace_and_summary_on_success(tmp_path):
    workspace = _make_workspace(tmp_path, "exec_ok")
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "inspect_data",
                    "args": {"sample_rows": 3},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="6行4列，无缺失。", tool_call_id="call_1", name="inspect_data"),
        AIMessage(content="数据检查完成：6行4列，无缺失。"),
    ]
    agent = FakeAgent(workspace, react_agent=_MockRunnable(response={"messages": messages}))
    state = {
        "query": "检查数据",
        "objective": "分析目标",
        "dataset_profile": {"rows": 6, "columns": 4, "column_info": []},
        "remaining_steps": [
            {"id": "inspect", "title": "检查数据", "instruction": "检查", "success_criteria": "完成"},
        ],
        "completed_steps": [],
        "input_messages": [],
        "trace": [],
    }

    result = execute_step(agent, state)

    assert result["current_step"]["id"] == "inspect"
    assert result["last_step_result"]["status"] == "ok"
    assert "6行4列" in result["last_step_result"]["summary"]
    trace_types = [entry["type"] for entry in result["trace"]]
    assert "tool_call" in trace_types
    assert "tool_result" in trace_types


def test_execute_step_handles_tool_failure_gracefully(tmp_path):
    workspace = _make_workspace(tmp_path, "exec_fail")
    # react_agent.invoke raises — simulating a network or runtime error.
    agent = FakeAgent(workspace, react_agent=_MockRunnable(exc=RuntimeError("connection error")))
    state = {
        "query": "检查数据",
        "objective": "分析目标",
        "dataset_profile": {"rows": 6, "columns": 4, "column_info": []},
        "remaining_steps": [
            {"id": "inspect", "title": "检查数据", "instruction": "检查", "success_criteria": "完成"},
        ],
        "completed_steps": [],
        "input_messages": [],
        "trace": [],
    }

    result = execute_step(agent, state)

    assert result["last_step_result"]["status"] == "failed"
    assert "失败" in result["last_step_result"]["summary"]
    error_entries = [entry for entry in result["trace"] if entry["type"] == "error"]
    assert len(error_entries) == 1
    assert "RuntimeError" in error_entries[0]["detail"]
    assert result["artifacts"] == []


def test_execute_step_rolls_back_on_unrecovered_tool_error(tmp_path):
    workspace = _make_workspace(tmp_path, "exec_rollback")
    # Messages end with an error ToolMessage and no subsequent recovery — the
    # node should roll back the workspace snapshot and mark the step as failed.
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "statistical_analysis",
                    "args": {},
                    "id": "call_err",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content="ValueError: could not convert string to float",
            tool_call_id="call_err",
            name="statistical_analysis",
            additional_kwargs={"error_code": "tool_error"},
        ),
    ]
    agent = FakeAgent(workspace, react_agent=_MockRunnable(response={"messages": messages}))
    state = {
        "query": "分析数据",
        "objective": "分析目标",
        "dataset_profile": {"rows": 6, "columns": 4, "column_info": []},
        "remaining_steps": [
            {"id": "analyze", "title": "统计分析", "instruction": "分析", "success_criteria": "完成"},
        ],
        "completed_steps": [],
        "input_messages": [],
        "trace": [],
    }

    result = execute_step(agent, state)

    assert result["last_step_result"]["status"] == "failed"
    assert "回滚" in result["last_step_result"]["summary"]


def test_execute_step_skips_when_plan_is_empty(tmp_path):
    workspace = _make_workspace(tmp_path, "exec_skip")
    agent = FakeAgent(workspace, react_agent=_MockRunnable())  # should not be invoked
    state = {
        "query": "检查数据",
        "objective": "分析目标",
        "dataset_profile": {"rows": 6, "columns": 4, "column_info": []},
        "remaining_steps": [],
        "completed_steps": [],
        "input_messages": [],
        "trace": [],
    }

    result = execute_step(agent, state)

    assert result == {"current_step": {}, "last_step_result": {}}
    assert agent.react_agent.invoke_count == 0


# ---------------------------------------------------------------------------
# replan
# ---------------------------------------------------------------------------


def test_replan_adds_new_steps_when_not_done(tmp_path):
    workspace = _make_workspace(tmp_path, "replan_add")
    decision = ReplanDecision(
        done=False,
        rationale="需要补充可视化步骤",
        remaining_steps=[
            PlanStep(
                id="visualize",
                title="生成图表",
                instruction="绘图",
                success_criteria="图表",
            ),
        ],
    )
    agent = FakeAgent(workspace, replanner=_MockRunnable(response=decision))
    state = {
        "query": "分析数据",
        "objective": "分析目标",
        "remaining_steps": [
            {"id": "old_step", "title": "旧步骤", "instruction": "...", "success_criteria": "..."},
        ],
        "completed_steps": [],
        "last_step_result": {"id": "inspect", "title": "检查", "status": "ok", "summary": "完成"},
        "artifacts": [],
    }

    result = replan(agent, state)

    # completed_steps includes the last_step_result.
    assert len(result["completed_steps"]) == 1
    assert result["completed_steps"][0]["id"] == "inspect"
    # New steps from the replanner decision.
    assert len(result["remaining_steps"]) == 1
    assert result["remaining_steps"][0]["id"] == "visualize"
    assert result["replan_reason"] == "需要补充可视化步骤"


def test_replan_returns_finalize_signal_when_done(tmp_path):
    workspace = _make_workspace(tmp_path, "replan_done")
    decision = ReplanDecision(done=True, rationale="分析完成", remaining_steps=[])
    agent = FakeAgent(workspace, replanner=_MockRunnable(response=decision))
    state = {
        "query": "分析数据",
        "objective": "分析目标",
        "remaining_steps": [],
        "completed_steps": [{"id": "inspect", "title": "检查", "summary": "完成"}],
        "last_step_result": {"id": "analyze", "title": "分析", "status": "ok", "summary": "完成"},
        "artifacts": [],
    }

    result = replan(agent, state)

    # completed_steps = existing + last_step_result.
    assert len(result["completed_steps"]) == 2
    assert result["remaining_steps"] == []
    assert result["replan_reason"] == "分析完成"


def test_replan_preserves_original_remaining_on_llm_failure(tmp_path):
    workspace = _make_workspace(tmp_path, "replan_fail")
    agent = FakeAgent(workspace, replanner=_MockRunnable(exc=RuntimeError("LLM error")))
    state = {
        "query": "分析数据",
        "objective": "分析目标",
        "remaining_steps": [
            {"id": "step1", "title": "步骤1", "instruction": "...", "success_criteria": "..."},
            {"id": "step2", "title": "步骤2", "instruction": "...", "success_criteria": "..."},
        ],
        "completed_steps": [],
        "last_step_result": {"id": "inspect", "title": "检查", "status": "ok", "summary": "完成"},
        "artifacts": [],
    }

    result = replan(agent, state)

    # original_remaining = remaining_steps[1:] = [step2]; exception falls back
    # to it.
    assert len(result["remaining_steps"]) == 1
    assert result["remaining_steps"][0]["id"] == "step2"
    assert "保留" in result["replan_reason"]


def test_replan_finalizes_when_max_plan_steps_reached(tmp_path):
    workspace = _make_workspace(tmp_path, "replan_max")
    agent = FakeAgent(
        workspace,
        replanner=_MockRunnable(),  # should not be invoked
        settings=AgentSettings(
            api_key="not-used",
            runs_dir=workspace.root.parent,
            max_iterations=5,
            max_plan_steps=2,
        ),
    )
    state = {
        "query": "分析数据",
        "objective": "分析目标",
        "remaining_steps": [
            {"id": "more", "title": "更多", "instruction": "...", "success_criteria": "..."},
        ],
        "completed_steps": [{"id": "s1", "title": "1", "summary": "ok"}],
        "last_step_result": {"id": "s2", "title": "2", "status": "ok", "summary": "ok"},
        "artifacts": [],
    }

    result = replan(agent, state)

    # completed = [s1, s2] (2 items) >= max_plan_steps(2) -> finalize.
    assert agent.replanner.invoke_count == 0
    assert result["remaining_steps"] == []
    assert "上限" in result["replan_reason"]


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def test_finalize_returns_response_and_artifacts(tmp_path):
    workspace = _make_workspace(tmp_path, "finalize_ok")
    agent = FakeAgent(workspace, model=_MockRunnable(response=AIMessage(content="这是分析报告。")))
    state = {
        "query": "分析数据",
        "completed_steps": [
            {"id": "inspect", "title": "检查数据", "summary": "6行4列，无缺失。"},
        ],
        "plan": [
            {"id": "inspect", "title": "检查数据", "instruction": "...", "success_criteria": "..."},
        ],
        "trace": [{"type": "tool_call", "name": "inspect_data"}],
        "artifacts": [],
    }

    result = finalize(agent, state)

    assert result["response"] == "这是分析报告。"
    assert result["artifacts"] == []
    assert result["dataset_profile"]["rows"] == 6
    assert result["plan"] == state["plan"]
    assert result["completed_steps"] == state["completed_steps"]
    assert result["trace"] == state["trace"]
    assert "usage" in result
    assert "reasoning" in result


def test_finalize_trims_long_summaries(tmp_path):
    workspace = _make_workspace(tmp_path, "finalize_trim")
    model = _MockRunnable(response=AIMessage(content="报告"))
    agent = FakeAgent(workspace, model=model)
    long_summary = "A" * 50_000  # far exceeds the per-step evidence budget.
    state = {
        "query": "分析数据",
        "completed_steps": [
            {"id": "s1", "title": "步骤一", "summary": long_summary},
            {"id": "s2", "title": "步骤二", "summary": long_summary},
        ],
        "plan": [],
        "trace": [],
        "artifacts": [],
    }

    result = finalize(agent, state)

    assert result["response"] == "报告"
    # The finalize prompt (captured by the mock model) should contain the
    # truncation marker for summaries that exceeded the per-step budget.
    assert model.captured_inputs, "finalize did not invoke model"
    prompt = model.captured_inputs[0]
    assert "已截断" in prompt


def test_finalize_fallback_when_model_fails(tmp_path):
    workspace = _make_workspace(tmp_path, "finalize_fallback")
    agent = FakeAgent(workspace, model=_MockRunnable(exc=RuntimeError("LLM error")))
    state = {
        "query": "分析数据",
        "completed_steps": [
            {"id": "inspect", "title": "检查数据", "summary": "完成检查。"},
        ],
        "plan": [],
        "trace": [],
        "artifacts": [],
    }

    result = finalize(agent, state)

    # Fallback message should mention the failure and include step summaries.
    assert "模型汇总失败" in result["response"]
    assert "检查数据" in result["response"]
    assert result["usage"] is None  # usage_acc set to None on exception.


def test_finalize_returns_workspace_artifacts_not_state_artifacts(tmp_path):
    workspace = _make_workspace(tmp_path, "finalize_artifacts")
    # Register a real artifact in the workspace.
    workspace.save_dataframe("cleaned.csv")
    agent = FakeAgent(workspace, model=_MockRunnable(response=AIMessage(content="报告")))
    state = {
        "query": "分析数据",
        "completed_steps": [],
        "plan": [],
        "trace": [],
        "artifacts": [{"name": "stale_from_state"}],  # should be ignored.
    }

    result = finalize(agent, state)

    # Artifacts come from agent.workspace.artifacts, not state["artifacts"].
    artifact_names = [item["name"] for item in result["artifacts"]]
    assert "cleaned.csv" in artifact_names
    assert "stale_from_state" not in artifact_names


# ---------------------------------------------------------------------------
# route_after_review
# ---------------------------------------------------------------------------


def test_route_after_review_returns_execute_when_steps_remain():
    assert route_after_review({"remaining_steps": [{"id": "step1"}]}) == "execute_step"


def test_route_after_review_returns_finalize_when_no_steps_remain():
    assert route_after_review({"remaining_steps": []}) == "finalize"


def test_route_after_review_returns_finalize_when_key_missing():
    assert route_after_review({}) == "finalize"
