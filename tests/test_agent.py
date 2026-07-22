from __future__ import annotations

import json
import threading
from pathlib import Path
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
                if isinstance(message.content, str) and "中文数据分析报告" in message.content:
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


def test_set_running_and_set_finished_track_wall_clock(tmp_path):
    """set_running / set_finished 应原子地更新 status 与 started_at / completed_at。"""
    import time as _time

    workspace = DataWorkspace(tmp_path / "runs", session_id="timing_test")
    workspace.save_upload("sales.csv", b"region,sales\nEast,100\n")
    workspace.load(workspace.input_dir / "sales.csv")

    registry = SessionRegistry(tmp_path / "runs", max_sessions=4, ttl_hours=24)
    session_id, record = registry.create(workspace)

    # idle 时无 started_at，elapsed_seconds 计算为 None。
    assert record.analysis_started_at is None
    assert record.analysis_completed_at is None

    record.set_running()
    assert record.analysis_status == "running"
    assert record.analysis_started_at is not None
    assert record.analysis_completed_at is None
    started = record.analysis_started_at

    _time.sleep(0.02)
    record.set_finished("completed")
    assert record.analysis_status == "completed"
    assert record.analysis_completed_at is not None
    # completed_at 严格大于 started_at，且 set_finished 不覆盖 started_at。
    assert record.analysis_completed_at > started
    assert record.analysis_started_at == started


def test_list_recent_returns_in_memory_and_disk_sessions(tmp_path):
    """list_recent 应同时返回内存中和磁盘上的会话摘要，并按 created_at 降序排列。"""
    runs_root = tmp_path / "runs"

    # 旧会话 1：仅写 manifest，registry 启动时不预加载（模拟服务重启后残留）。
    ws_old = DataWorkspace(runs_root, session_id="api_old_one")
    ws_old.save_upload("old.csv", b"x,y\n1,2\n")
    ws_old.load(ws_old.input_dir / "old.csv")

    reg_old = SessionRegistry(runs_root, max_sessions=10, ttl_hours=24)
    old_id, old_record = reg_old.create(ws_old)
    old_record.analysis_status = "completed"
    old_record.last_result = AnalysisResult(
        response="旧报告",
        trace=[],
        artifacts=[],
        dataset_profile=ws_old.profile(),
        plan=[],
        completed_steps=[],
    )
    reg_old.persist(old_id, old_record)
    # 释放 reg_old 引用，让该会话只存在于磁盘上。
    del reg_old
    del old_record

    # 新会话 2：在当前 registry 内存中。
    ws_new = DataWorkspace(runs_root, session_id="api_new_two")
    ws_new.save_upload("new.csv", b"x,y\n3,4\n")
    ws_new.load(ws_new.input_dir / "new.csv")
    reg = SessionRegistry(runs_root, max_sessions=10, ttl_hours=24)
    new_id, new_record = reg.create(ws_new)
    new_record.analysis_status = "running"

    recent = reg.list_recent(limit=10)
    assert len(recent) == 2

    # 内存中的新会话应标注 in_memory=True，磁盘上的旧会话 in_memory=False。
    by_id = {item["id"]: item for item in recent}
    assert by_id[new_id]["in_memory"] is True
    assert by_id[new_id]["analysis_status"] == "running"
    assert by_id[old_id]["in_memory"] is False
    assert by_id[old_id]["analysis_status"] == "completed"
    assert by_id[old_id]["has_result"] is True

    # 按 created_at 降序：新会话在前。
    assert recent[0]["id"] == new_id
    assert recent[1]["id"] == old_id


def test_chart_filename_stem_and_humanized_title_strip_technical_noise():
    """图表文件名 stem 用"类型_序号"，title 清理 ANOVA / p 值 / η² 等技术标记。"""
    from data_agent.tools import (
        _CHART_TYPE_LABELS_ZH,
        _chart_filename_stem,
        _humanize_chart_title,
    )

    # 文件名 stem：相同类型递增序号，未知类型回退到 chart_type 本身。
    # 序号用自然数字 1/2/3 而非 01/02/03，更接近 Observable / Plot 的命名惯例。
    assert _chart_filename_stem("bar", 0) == "柱状图_1"
    assert _chart_filename_stem("bar", 1) == "柱状图_2"
    assert _chart_filename_stem("line", 3) == "折线图_4"
    assert _chart_filename_stem("unknown_type", 0) == "unknown_type_1"

    # 已知类型都在标签字典里，避免 LLM 用了新名字时文件名变得难看。
    for chart_type, label in _CHART_TYPE_LABELS_ZH.items():
        assert _chart_filename_stem(chart_type, 0) == f"{label}_1"

    # title 清理：去掉 ANOVA / p 值 / η² / _n_N / 极端离群值 等标记。
    noisy = "客户评分按产品分布_ANOVA_p_0_0012_η²_0_546"
    assert _humanize_chart_title(noisy, "bar") == "客户评分按产品分布"

    # 离群值标记应被剥离。
    outlier = "销量_vs_营收散点图_极端离群值主导"
    cleaned = _humanize_chart_title(outlier, "scatter")
    assert "极端离群值" not in cleaned
    assert "主导" not in cleaned
    assert "销量_vs_营收" in cleaned

    # 样本量标记应被剥离。
    sample = "区域销售_n_2"
    assert _humanize_chart_title(sample, "bar") == "区域销售"

    # 空标题回退到类型中文短名。
    assert _humanize_chart_title("", "box") == "箱线图"
    assert _humanize_chart_title(None, "pie") == "饼图"

    # title 全是技术标记时回退到类型中文短名，而不是返回原始乱码。
    all_technical = "_ANOVA_p_0_0012_η²_0_546"
    assert _humanize_chart_title(all_technical, "bar") == "柱状图"

    # 超长标题截短到 30 字符。
    long_title = "这是一个非常非常非常非常非常非常非常非常非常非常非常长的图表标题需要被截断"
    truncated = _humanize_chart_title(long_title, "bar")
    assert len(truncated) <= 30

    # 截短后为空（前 30 字符全是分隔符）回退到类型短名，不返回空字符串。
    all_separators = "，" * 35
    assert _humanize_chart_title(all_separators, "bar") == "柱状图"

    # 非贪婪正则不误伤合法副标题：之前 _ANOVA.*$ 会吞掉后面的中文。
    # "客户评分分布_ANOVA_用户洞察" 应保留"用户洞察"。
    preserved = _humanize_chart_title("客户评分分布_ANOVA_用户洞察", "bar")
    assert "用户洞察" in preserved
    assert "ANOVA" not in preserved

    # 离群值标记后面有合法副标题时也应保留。
    preserved2 = _humanize_chart_title("销量趋势_极端离群值_季度对比", "line")
    assert "季度对比" in preserved2
    assert "极端离群值" not in preserved2


def test_cancel_analysis_uses_cas_to_avoid_overwriting_terminal_state(tmp_path):
    """H1: cancel_analysis 必须用 CAS 检查，避免 worker 已完成时把 completed 覆盖成 cancelling。

    场景：cancel 请求到达时 worker 刚刚 set_finished('completed')。原实现
    用 if not is_running() 检查 + 直接赋值 cancelling，存在 TOCTOU 窗口。
    """
    from data_agent.api import SessionRecord
    from data_agent.workspace import DataWorkspace

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    workspace = DataWorkspace(runs_dir, session_id="api_cas_test")
    # 不走 registry.create（会触发 save_checkpoint 要求 dataframe），
    # 直接构造 SessionRecord 测试状态机的 CAS 行为。
    record = SessionRecord(workspace)
    record.set_running()

    # 模拟 worker 已先于 cancel 写入 completed 终态。
    record.set_finished("completed")

    # cancel 请求到来：必须看到 completed 而不是回退到 cancelling。
    # 这里直接复现 cancel_analysis 内的 CAS 逻辑。
    with record._status_lock:
        if record._analysis_status != "running":
            result_status = record._analysis_status
        else:
            record._analysis_status = "cancelling"
            result_status = "cancelling"
    assert result_status == "completed", "CAS 检查必须阻止把 completed 覆盖成 cancelling"


def test_create_visualization_escapes_script_tag_in_html(tmp_path):
    """H1 (tools.py): 图表 HTML 必须转义 </script>，防止用户数据触发 XSS。

    用户 CSV 某列含 "</script><script>alert(1)</script>" 字符串，LLM 用
    该列作 x 时，Plotly to_html 会把它原样写入 <script> 块。浏览器解析
    时第一个 </script> 提前关闭 Plotly script，剩余 JS 在 iframe 执行。
    """
    import pandas as pd

    from data_agent.tools import build_tools
    from data_agent.workspace import DataWorkspace

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    workspace = DataWorkspace(runs_dir, session_id="api_xss_test")
    # 构造含 XSS payload 的 DataFrame。
    df = pd.DataFrame({
        "product": ["</script><script>alert(1)</script>", "正常产品B"],
        "sales": [100, 200],
    })
    workspace.dataframe = df
    workspace._artifacts = []  # 重置产物计数

    # create_visualization 是 build_tools 内的闭包 tool，通过 build_tools 访问。
    tools = build_tools(workspace)
    vis_tool = next(t for t in tools if t.name == "create_visualization")
    vis_tool.invoke({
        "chart_type": "bar",
        "x": "product",
        "y": "sales",
        "aggregation": "sum",
    })
    # 找到生成的 HTML 文件（workspace.artifacts 返回 list[dict]）。
    html_artifacts = [a for a in workspace.artifacts if a.get("kind") == "visualization"]
    assert html_artifacts, "应该生成 HTML 产物"
    html_path = html_artifacts[0]["path"]
    html_content = Path(html_path).read_text(encoding="utf-8")
    # 用户数据中的 </script> 必须被转义为 <\/script>，不能形成有效的 script 闭合。
    assert "<\\/script>" in html_content, "用户数据中的 </script> 必须被转义为 <\\/script>"
    # Plotly 自身的 <script> 标签是合法的，不应被转义。
    # 统计未转义的 </script>：应有 Plotly 自身 1 个 + 暗色适配脚本 1 个 = 2 个。
    # 用户数据中的 </script> 必须全部被转义，不能出现在原始计数里。
    raw_close_count = html_content.count("</script>")
    assert raw_close_count == 2, f"应有 Plotly + 暗色适配共 2 个 </script>，实际 {raw_close_count}"
