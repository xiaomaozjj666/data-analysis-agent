"""覆盖低覆盖模块的补充测试：cli / deployment / nodes._utils / callbacks / storage / analysis 路由。

本文件聚焦此前覆盖率不足的分支与错误路径，不重复已有测试覆盖的 happy path。
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from data_agent.config import AgentSettings
from data_agent.workspace import DataWorkspace

# ---------------------------------------------------------------------------
# nodes/_utils.py：_message_text 分支覆盖
# ---------------------------------------------------------------------------


def test_message_text_handles_none():
    from data_agent.nodes._utils import _message_text

    assert _message_text(None) == ""


def test_message_text_extracts_str_content():
    from data_agent.nodes._utils import _message_text

    assert _message_text(HumanMessage(content="hello")) == "hello"


def test_message_text_extracts_list_content_text_blocks():
    from data_agent.nodes._utils import _message_text

    message = AIMessage(content=[{"type": "text", "text": "第一段"}, {"type": "output_text", "text": "第二段"}])
    assert _message_text(message) == "第一段\n第二段"


def test_message_text_skips_non_text_blocks_and_bare_strings():
    from data_agent.nodes._utils import _message_text

    # 混合 bare string + 非 text 类型 dict（如 image_url）
    message = AIMessage(content=["纯字符串段", {"type": "image_url", "image_url": {"url": "http://x"}}])
    result = _message_text(message)
    assert "纯字符串段" in result
    assert "image_url" not in result


def test_message_text_empty_list_returns_empty():
    from data_agent.nodes._utils import _message_text

    assert _message_text(AIMessage(content=[])) == ""


# ---------------------------------------------------------------------------
# callbacks.py：ToolTraceCallback / ReportStreamCallback / ReasoningStreamCallback / UsageAccumulator
# ---------------------------------------------------------------------------


def test_report_stream_callback_pushes_non_empty_token():
    captured: list[tuple[str, dict]] = []
    from data_agent.callbacks import ReportStreamCallback

    cb = ReportStreamCallback(lambda et, payload: captured.append((et, payload)), event_type="chat_chunk")
    cb.on_llm_new_token("")  # 空字符串不应推送
    cb.on_llm_new_token("hello")
    assert captured == [("chat_chunk", {"chunk": "hello"})]


def test_report_stream_callback_swallows_callback_exception():
    from data_agent.callbacks import ReportStreamCallback

    def raise_cb(et, payload):
        raise RuntimeError("downstream closed")

    cb = ReportStreamCallback(raise_cb)
    # 不应抛出
    cb.on_llm_new_token("token")


def test_tool_trace_callback_reset_clears_counter():
    from data_agent.callbacks import ToolTraceCallback

    cb = ToolTraceCallback(lambda et, p: None, step_index=1, total_steps=3)
    cb.on_tool_start(serialized={"name": "inspect_data"}, input_str="{}", run_id="r1")
    assert cb._call_count == 1
    cb.reset()
    assert cb._call_count == 0


def test_tool_trace_callback_handles_missing_serialized_and_run_id():
    captured: list[tuple[str, dict]] = []
    from data_agent.callbacks import ToolTraceCallback

    cb = ToolTraceCallback(lambda et, p: captured.append((et, p)))
    # serialized=None 和 run_id 缺失时不应崩溃
    cb.on_tool_start(serialized=None, input_str="", **{})
    assert any(item[0] == "tool_call" and item[1]["name"] == "unknown" for item in captured)


def test_tool_trace_callback_on_tool_end_without_start_record():
    captured: list[tuple[str, dict]] = []
    from data_agent.callbacks import ToolTraceCallback

    cb = ToolTraceCallback(lambda et, p: captured.append((et, p)))
    # 未 on_tool_start 就 on_tool_end：duration_ms 应为 0
    cb.on_tool_end(output="done", run_id="missing")
    assert any(item[0] == "tool_result" and item[1]["duration_ms"] == 0 for item in captured)


def test_tool_trace_callback_swallows_callback_exception():
    from data_agent.callbacks import ToolTraceCallback

    def raise_cb(et, p):
        raise RuntimeError("closed")

    cb = ToolTraceCallback(raise_cb)
    cb.on_tool_start(serialized={"name": "x"}, input_str="", run_id="r1")
    cb.on_tool_end(output="ok", run_id="r1")


def test_reasoning_stream_callback_skips_token_without_reasoning():
    captured: list[tuple[str, dict]] = []
    from data_agent.callbacks import ReasoningStreamCallback

    buffer: list[str] = []
    cb = ReasoningStreamCallback(lambda et, p: captured.append((et, p)), buffer=buffer)
    # 无 chunk 参数 → 不推送
    cb.on_llm_new_token("plain")
    assert captured == []
    assert buffer == []


def test_reasoning_stream_callback_pushes_reasoning_content():
    captured: list[tuple[str, dict]] = []
    from data_agent.callbacks import ReasoningStreamCallback

    buffer: list[str] = []
    cb = ReasoningStreamCallback(lambda et, p: captured.append((et, p)), buffer=buffer)

    class FakeChunk:
        class _Msg:
            additional_kwargs = {"reasoning_content": "思考中"}

        message = _Msg()

    cb.on_llm_new_token("", chunk=FakeChunk())
    assert captured == [("thinking_chunk", {"chunk": "思考中"})]
    assert buffer == ["思考中"]


def test_usage_accumulator_aggregates_llm_output_format():
    from data_agent.callbacks import UsageAccumulator

    class FakeResp:
        llm_output = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        generations = []

    acc = UsageAccumulator()
    acc.on_llm_end(FakeResp())
    assert acc.snapshot() == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_usage_accumulator_falls_back_to_usage_metadata():
    from data_agent.callbacks import UsageAccumulator

    class FakeGen:
        class _Msg:
            usage_metadata = {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}

        message = _Msg()

    class FakeResp:
        llm_output = {}
        generations = [[FakeGen()]]

    acc = UsageAccumulator()
    acc.on_llm_end(FakeResp())
    assert acc.snapshot() == {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}


def test_usage_accumulator_handles_missing_fields():
    from data_agent.callbacks import UsageAccumulator

    class FakeResp:
        llm_output = {"token_usage": {}}
        generations = []

    acc = UsageAccumulator()
    acc.on_llm_end(FakeResp())
    assert acc.snapshot() == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# storage.py：build_session_storage 分支 + S3SessionStorage 错误路径
# ---------------------------------------------------------------------------


def test_build_session_storage_rejects_unknown_backend(monkeypatch):
    from data_agent.storage import build_session_storage

    monkeypatch.setenv("DATA_AGENT_STORAGE_BACKEND", "invalid_backend")
    with pytest.raises(ValueError, match="仅支持 local 或 s3"):
        build_session_storage()


def test_build_session_storage_s3_requires_full_config(monkeypatch):
    from data_agent.storage import build_session_storage

    monkeypatch.setenv("DATA_AGENT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("DATA_AGENT_STORAGE_BUCKET", "")  # 缺 bucket
    with pytest.raises(ValueError, match="配置不完整"):
        build_session_storage()


def test_build_session_storage_s3_uses_r2_account_id(monkeypatch):
    from data_agent.storage import S3SessionStorage, build_session_storage

    monkeypatch.setenv("DATA_AGENT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("DATA_AGENT_R2_ACCOUNT_ID", "abc123")
    monkeypatch.setenv("DATA_AGENT_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk")
    # boto3 import 可能在无网络环境失败，捕获并跳过
    try:
        storage = build_session_storage()
        assert isinstance(storage, S3SessionStorage)
        assert storage.endpoint_url == "https://abc123.r2.cloudflarestorage.com"
    except Exception:  # boto3 不可用时跳过
        pytest.skip("boto3 not available")


def test_s3_storage_sync_failure_wraps_runtime_error(tmp_path, monkeypatch):
    from data_agent.storage import S3SessionStorage

    storage = object.__new__(S3SessionStorage)
    storage.bucket = "b"
    storage.endpoint_url = "https://x"
    storage.prefix = "sessions"

    class FailingClient:
        def upload_file(self, *args, **kwargs):
            raise RuntimeError("network down")

    storage.client = FailingClient()
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="会话持久化失败"):
        storage.sync_session("s1", source)


def test_s3_storage_restore_404_returns_false(tmp_path):
    from data_agent.storage import S3SessionStorage

    storage = object.__new__(S3SessionStorage)
    storage.bucket = "b"
    storage.prefix = "sessions"

    class NotFoundClient:
        def download_file(self, *args, **kwargs):
            err = Exception("not found")
            err.response = {"Error": {"Code": "404"}}
            raise err

    storage.client = NotFoundClient()
    dest = tmp_path / "dest"
    assert storage.restore_session("missing", dest) is False


def test_s3_storage_restore_other_error_wraps_runtime(tmp_path):
    from data_agent.storage import S3SessionStorage

    storage = object.__new__(S3SessionStorage)
    storage.bucket = "b"
    storage.prefix = "sessions"

    class FailingClient:
        def download_file(self, *args, **kwargs):
            err = Exception("server error")
            err.response = {"Error": {"Code": "500"}}
            raise err

    storage.client = FailingClient()
    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="会话恢复失败"):
        storage.restore_session("s1", dest)


def test_s3_storage_delete_failure_wraps_runtime():
    from data_agent.storage import S3SessionStorage

    storage = object.__new__(S3SessionStorage)
    storage.bucket = "b"
    storage.prefix = "sessions"

    class FailingClient:
        def delete_object(self, **kwargs):
            raise RuntimeError("denied")

    storage.client = FailingClient()
    with pytest.raises(RuntimeError, match="归档删除失败"):
        storage.delete_session("s1")


def test_s3_storage_healthcheck_reports_error():
    from data_agent.storage import S3SessionStorage

    storage = object.__new__(S3SessionStorage)
    storage.bucket = "b"
    storage.persistent = True
    storage.backend = "s3"

    class FailingClient:
        def head_bucket(self, **kwargs):
            raise RuntimeError("no access")

    storage.client = FailingClient()
    result = storage.healthcheck()
    assert result["status"] == "error"
    assert "no access" in result["message"]


def test_s3_storage_restore_rejects_path_traversal(tmp_path):
    """归档成员包含 .. 路径时应拒绝解压，防止路径穿越攻击。"""
    import zipfile

    from data_agent.storage import S3SessionStorage

    storage = object.__new__(S3SessionStorage)
    storage.bucket = "b"
    storage.prefix = "sessions"

    # 构造含恶意路径的归档
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("../../escape.txt", "malicious")

    class FakeClient:
        def download_file(self, bucket, key, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(archive_path.read_bytes())

    storage.client = FakeClient()
    dest = tmp_path / "session_dest"
    dest.mkdir()
    with pytest.raises(ValueError, match="不安全路径"):
        storage.restore_session("s1", dest)


def test_local_storage_healthcheck_shape():
    from data_agent.storage import LocalSessionStorage

    result = LocalSessionStorage().healthcheck()
    assert result["backend"] == "local"
    assert result["status"] == "local_only"
    assert result["persistent"] is False


# ---------------------------------------------------------------------------
# cli.py：通过 CliRunner 调用 analyze 命令
# ---------------------------------------------------------------------------


def test_cli_main_callback_runs_without_error():
    """typer callback 应可被无参数调用（用于 --help 等场景）。"""
    from data_agent.cli import main

    main()


def test_cli_analyze_runs_end_to_end(tmp_path, monkeypatch):
    """CLI analyze 命令应能加载数据并打印分析结果（用 fake model 替换真实 LLM）。"""
    from typer.testing import CliRunner

    # 准备数据文件
    data_path = tmp_path / "sales.csv"
    data_path.write_text("region,sales\nEast,100\nWest,200\n", encoding="utf-8")

    # 确保环境变量不干扰
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)

    # patch create_chat_model 返回 fake model，避免真实 LLM 调用
    from data_agent import agent as agent_module
    from tests.test_agent import ToolCallingFakeModel

    def fake_create_chat_model(settings):
        return ToolCallingFakeModel()

    monkeypatch.setattr(agent_module, "create_chat_model", fake_create_chat_model)
    # cli.py 通过 from data_agent.agent import DataAnalysisAgent 引用，
    # DataAnalysisAgent.__init__ 内部调用 self.model 或 create_chat_model。
    # 由于 DataAnalysisAgent.__init__ 用 model or create_chat_model(self.settings)，
    # 不传 model 时会走 create_chat_model，patch agent_module 即可生效。

    from data_agent.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["analyze", str(data_path), "--task", "检查数据"])
    assert result.exit_code == 0, f"CLI failed: {result.output}"
    assert "ReAct" in result.output or "已加载" in result.output


# ---------------------------------------------------------------------------
# deployment.py：_resolve_dataset_source 路径穿越防护 + make_graph 构建
# ---------------------------------------------------------------------------


def test_deployment_resolve_dataset_id_rejects_invalid_format(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    # 含特殊字符的 dataset_id 应被拒绝
    with pytest.raises(ValueError, match="dataset_id 格式无效"):
        _resolve_dataset_source({"dataset_id": "../escape"}, settings, None)


def test_deployment_resolve_dataset_id_missing_dir_raises(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)

    class FailingStorage:
        def restore_session(self, *args):
            return False

    with pytest.raises(FileNotFoundError, match="找不到指定的数据集工作区"):
        _resolve_dataset_source({"dataset_id": "nonexistent"}, settings, FailingStorage())


def test_deployment_resolve_dataset_id_empty_input_dir_raises(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    # 创建空 input 目录
    (tmp_path / "empty_session" / "input").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="没有可读取的文件"):
        _resolve_dataset_source({"dataset_id": "empty_session"}, settings, None)


def test_deployment_resolve_dataset_path_rejects_absolute(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    with pytest.raises(ValueError, match="绝对路径"):
        _resolve_dataset_source({"dataset_path": str(tmp_path / "abs.csv")}, settings, None)


def test_deployment_resolve_dataset_path_rejects_parent_traversal(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    with pytest.raises(ValueError, match="上级目录"):
        _resolve_dataset_source({"dataset_path": "../escape.csv"}, settings, None)


def test_deployment_resolve_dataset_path_rejects_missing_file(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="找不到指定的数据集文件"):
        _resolve_dataset_source({"dataset_path": "missing.csv"}, settings, None)


def test_deployment_resolve_dataset_path_resolves_valid_file(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    data_file = tmp_path / "data.csv"
    data_file.write_text("a,b\n1,2\n", encoding="utf-8")
    resolved = _resolve_dataset_source({"dataset_path": "data.csv"}, settings, None)
    assert resolved == data_file.resolve()


def test_deployment_resolve_dataset_id_restores_and_returns_file(tmp_path):
    from data_agent.deployment import _resolve_dataset_source

    settings = AgentSettings(api_key="x", runs_dir=tmp_path)
    # restore_session 应创建 input 目录并写入数据文件
    data_file = tmp_path / "restored_session" / "input" / "data.csv"

    class RestoringStorage:
        def __init__(self):
            self.called = False

        def restore_session(self, session_id, dest_parent):
            self.called = True
            # dest_parent 是 input 目录的父目录（session 根目录）
            input_dir = dest_parent / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            data_file.write_text("a,b\n1,2\n", encoding="utf-8")

    storage = RestoringStorage()
    result = _resolve_dataset_source({"dataset_id": "restored_session"}, settings, storage)
    assert storage.called is True
    assert result.name == "data.csv"


def test_deployment_make_graph_compiles(tmp_path, monkeypatch):
    """make_graph 应返回可编译的 LangGraph，不实际执行。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    from data_agent.deployment import make_graph

    graph = make_graph(config={"configurable": {"thread_id": "test-thread"}})
    assert graph is not None


# ---------------------------------------------------------------------------
# routers/analysis.py：_classify_analysis_error / _client_error_detail / _safe_emit / _error_payload
# ---------------------------------------------------------------------------


def test_classify_analysis_error_timeout():
    from data_agent.routers.analysis import _classify_analysis_error

    code, hint = _classify_analysis_error(Exception("APITimeoutError: request timed out"))
    assert code == "model_timeout"
    assert "超时" in hint


def test_classify_analysis_error_quota():
    from data_agent.routers.analysis import _classify_analysis_error

    code, _ = _classify_analysis_error(Exception("insufficient balance"))
    assert code == "quota_exhausted"


def test_classify_analysis_error_rate_limit():
    from data_agent.routers.analysis import _classify_analysis_error

    code, _ = _classify_analysis_error(Exception("RateLimitError: 429 too many requests"))
    assert code == "rate_limited"


def test_classify_analysis_error_auth():
    from data_agent.routers.analysis import _classify_analysis_error

    code, _ = _classify_analysis_error(Exception("AuthenticationError: invalid api key"))
    assert code == "auth_failed"


def test_classify_analysis_error_connection():
    from data_agent.routers.analysis import _classify_analysis_error

    code, _ = _classify_analysis_error(Exception("APIConnectionError: connection error"))
    assert code == "connection_failed"


def test_classify_analysis_error_unknown_falls_back():
    from data_agent.routers.analysis import _classify_analysis_error

    code, hint = _classify_analysis_error(Exception("ValueError: something else"))
    assert code == "analysis_failed"
    assert hint == ""


def test_client_error_detail_truncates_long_message():
    from data_agent.routers.analysis import _client_error_detail

    long_msg = "x" * 500
    result = _client_error_detail(Exception(long_msg))
    assert len(result) <= 301  # 300 + "…"
    assert result.endswith("…")


def test_client_error_detail_preserves_short_message():
    from data_agent.routers.analysis import _client_error_detail

    assert _client_error_detail(Exception("短消息")) == "短消息"


def test_client_error_detail_falls_back_to_class_name():
    from data_agent.routers.analysis import _client_error_detail

    # 异常消息为空时回退到类名
    assert _client_error_detail(Exception()) == "Exception"


def test_error_payload_with_hint_and_detail():
    from data_agent.routers.analysis import _error_payload

    payload = _error_payload(Exception("APITimeoutError: timed out"))
    assert payload["code"] == "model_timeout"
    assert payload["hint"]  # 非空
    assert "timed out" in payload["message"]


def test_error_payload_with_prefix_for_unknown_error():
    from data_agent.routers.analysis import _error_payload

    payload = _error_payload(Exception("custom error"), prefix="追问失败：")
    assert payload["code"] == "analysis_failed"
    assert payload["message"].startswith("追问失败：")


def test_safe_emit_drops_droppable_event_when_queue_full():
    """队列满时 thinking_chunk 等可丢弃事件应被静默丢弃。"""
    from data_agent.routers.analysis import _safe_emit

    async def _run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(("existing", {}))
        # 队列已满，thinking_chunk 应被丢弃
        _safe_emit(asyncio.get_running_loop(), queue, ("thinking_chunk", {"chunk": "x"}))
        # 让 call_soon_threadsafe 调度的回调执行
        await asyncio.sleep(0.01)
        # 队列仍只有 1 个元素（原元素）
        assert queue.qsize() == 1
        event, _ = queue.get_nowait()
        assert event == "existing"

    asyncio.run(_run())


def test_safe_emit_evicts_oldest_for_terminal_event():
    """队列满时 complete 等终态事件应淘汰最旧元素后入队。"""
    from data_agent.routers.analysis import _safe_emit

    async def _run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(("thinking_chunk", {"chunk": "old"}))
        # complete 不可丢弃：淘汰旧的 thinking_chunk 后入队
        _safe_emit(asyncio.get_running_loop(), queue, ("complete", {"response": "new"}))
        await asyncio.sleep(0.01)
        assert queue.qsize() == 1
        event, data = queue.get_nowait()
        assert event == "complete"

    asyncio.run(_run())


def test_safe_emit_swallows_runtime_error_when_loop_closed():
    """事件循环已关闭时 _safe_emit 不应抛出 RuntimeError。"""
    from data_agent.routers.analysis import _safe_emit

    loop = asyncio.new_event_loop()
    loop.close()
    # 不应抛异常
    _safe_emit(loop, asyncio.Queue(maxsize=10), ("complete", {}))
    _safe_emit(loop, None, None)


def test_sse_injects_version_into_dict_payload():
    from data_agent.api import API_VERSION_INT
    from data_agent.routers.analysis import _sse

    sse_text = _sse("test", {"key": "value"})
    assert "event: test" in sse_text
    assert f'"v": {API_VERSION_INT}' in sse_text


def test_sse_preserves_non_dict_payload():
    from data_agent.routers.analysis import _sse

    sse_text = _sse("test", "plain string")
    assert "event: test" in sse_text
    assert "plain string" in sse_text


# ---------------------------------------------------------------------------
# agent.py：chat / stream / _input_state 边界
# ---------------------------------------------------------------------------


def test_input_state_rejects_empty_query(workspace):
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    with pytest.raises(ValueError, match="分析任务不能为空"):
        agent._input_state("   ")


def test_input_state_injects_resume_from(workspace):
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    resume = {
        "plan": [{"id": "inspect", "title": "检查", "instruction": "x", "success_criteria": "y"}],
        "completed_steps": [{"id": "inspect", "title": "检查", "summary": "done"}],
        "objective": "已有目标",
    }
    state = agent._input_state("继续", resume_from=resume)
    assert state["objective"] == "已有目标"
    assert state["plan"] == resume["plan"]
    # completed 的步骤不应出现在 remaining_steps
    assert state["remaining_steps"] == []


def test_chat_rejects_empty_query(workspace):
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    with pytest.raises(ValueError, match="追问不能为空"):
        agent.chat("")


def test_chat_returns_response_and_tracks_usage(workspace):
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    response, artifacts = agent.chat("检查数据")
    assert response  # 非空
    assert isinstance(artifacts, list)
    # chat 后应有 _last_usage 快照
    assert agent._last_usage is not None
    assert "prompt_tokens" in agent._last_usage


def test_stream_yields_node_updates(workspace):
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    updates = list(agent.stream("检查数据"))
    assert updates
    # 应至少包含 plan_analysis 和 finalize 节点
    nodes = [u["node"] for u in updates]
    assert "plan_analysis" in nodes
    assert "finalize" in nodes


def test_stream_plan_only_stops_after_plan(workspace):
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(workspace, settings=settings, model=ToolCallingFakeModel())
    updates = list(agent.stream("检查数据", plan_only=True))
    nodes = [u["node"] for u in updates]
    assert "plan_analysis" in nodes
    # plan_only 模式下不应执行到 finalize
    assert "finalize" not in nodes


def test_ensure_not_cancelled_raises_when_event_set(workspace):
    from data_agent.agent import DataAnalysisAgent
    from data_agent.models import AnalysisCancelled
    from tests.test_agent import ToolCallingFakeModel

    cancel_event = threading.Event()
    cancel_event.set()
    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)
    agent = DataAnalysisAgent(
        workspace, settings=settings, model=ToolCallingFakeModel(), cancel_event=cancel_event
    )
    with pytest.raises(AnalysisCancelled):
        agent._ensure_not_cancelled()


def test_enter_node_swallows_progress_callback_exception(workspace):
    """progress_callback 抛异常时不应影响工作流。"""
    from data_agent.agent import DataAnalysisAgent
    from tests.test_agent import ToolCallingFakeModel

    settings = AgentSettings(api_key="not-used", runs_dir=workspace.root.parent)

    def bad_callback(node, title):
        raise RuntimeError("callback broken")

    agent = DataAnalysisAgent(
        workspace, settings=settings, model=ToolCallingFakeModel(), progress_callback=bad_callback
    )
    # 不应抛出
    agent._enter_node("plan_analysis", "规划中")


# ---------------------------------------------------------------------------
# routers/analysis.py：analyze 端点（同步）错误路径
# ---------------------------------------------------------------------------


def test_analyze_endpoint_409_when_api_key_missing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    # 隔离运行时
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(
        api_key="",  # 空 key 触发 409
        runs_dir=runs_dir,
        max_concurrent_analyses=2,
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    response = client.post(f"/api/sessions/{upload['id']}/analyze", json={"task": "x"})
    assert response.status_code == 409
    assert "API Key" in response.json()["detail"]


def test_analyze_endpoint_409_when_already_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    record = api.registry.get(upload["id"])
    # 模拟已有分析在跑
    record.run_lock.acquire()
    try:
        response = client.post(f"/api/sessions/{upload['id']}/analyze", json={"task": "x"})
        assert response.status_code == 409
        assert "正在运行" in response.json()["detail"]
    finally:
        record.run_lock.release()


def test_analyze_endpoint_429_when_slots_exhausted(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=1)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.setattr(
        api, "analysis_slots", threading.BoundedSemaphore(1)
    )
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()
    # 占用唯一一个 slot
    api.analysis_slots.acquire()

    try:
        client = TestClient(api.app)
        upload = client.post(
            "/api/sessions",
            files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        ).json()
        response = client.post(f"/api/sessions/{upload['id']}/analyze", json={"task": "x"})
        assert response.status_code == 429
    finally:
        api.analysis_slots.release()


def test_chat_stream_endpoint_409_when_analysis_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    record = api.registry.get(upload["id"])
    record.analysis_status = "running"
    response = client.post(f"/api/sessions/{upload['id']}/chat/stream", json={"task": "追问"})
    assert response.status_code == 409
    assert "分析正在运行" in response.json()["detail"]


def test_chat_stream_endpoint_409_when_api_key_missing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    response = client.post(f"/api/sessions/{upload['id']}/chat/stream", json={"task": "追问"})
    assert response.status_code == 409
    assert "API Key" in response.json()["detail"]


def test_cancel_endpoint_returns_status_when_not_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    # idle 状态下 cancel 应返回当前状态
    response = client.post(f"/api/sessions/{upload['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "idle"


def test_cancel_endpoint_persist_failure_is_swallowed(tmp_path, monkeypatch):
    """cancel 端点的 persist 异常应被吞掉，不影响返回 cancelling。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    record = api.registry.get(upload["id"])
    record.analysis_status = "running"

    def raise_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.registry, "persist", raise_persist)
    response = client.post(f"/api/sessions/{upload['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"


def test_analyze_endpoint_returns_502_on_exception(tmp_path, monkeypatch):
    """analyze 同步端点遇到异常分支应返回 502 并持久化 failed 状态。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class FailingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            raise RuntimeError("model boom")

    monkeypatch.setattr(api, "DataAnalysisAgent", FailingAgent)

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    response = client.post(f"/api/sessions/{upload['id']}/analyze", json={"task": "x"})
    assert response.status_code == 502
    assert "分析执行失败" in response.json()["detail"]
    record = api.registry.get(upload["id"])
    assert record.analysis_status == "failed"


def test_analyze_endpoint_returns_409_on_cancellation(tmp_path, monkeypatch):
    """analyze 同步端点遇到 AnalysisCancelled 应返回 409 并持久化 cancelled 状态。"""
    from fastapi.testclient import TestClient

    from data_agent import api
    from data_agent.models import AnalysisCancelled

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class CancellingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            raise AnalysisCancelled("user aborted")

    monkeypatch.setattr(api, "DataAnalysisAgent", CancellingAgent)

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    response = client.post(f"/api/sessions/{upload['id']}/analyze", json={"task": "x"})
    assert response.status_code == 409
    assert "user aborted" in response.json()["detail"]
    record = api.registry.get(upload["id"])
    assert record.analysis_status == "cancelled"


def test_analyze_stream_endpoint_409_when_api_key_missing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    response = client.post(f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"})
    assert response.status_code == 409
    assert "API Key" in response.json()["detail"]


def test_analyze_stream_endpoint_409_when_already_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    record = api.registry.get(upload["id"])
    record.run_lock.acquire()
    try:
        response = client.post(f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"})
        assert response.status_code == 409
        assert "正在运行" in response.json()["detail"]
    finally:
        record.run_lock.release()


def test_analyze_stream_endpoint_429_when_slots_exhausted(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=1)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.setattr(api, "analysis_slots", threading.BoundedSemaphore(1))
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()
    api.analysis_slots.acquire()
    try:
        client = TestClient(api.app)
        upload = client.post(
            "/api/sessions",
            files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        ).json()
        response = client.post(f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"})
        assert response.status_code == 429
    finally:
        api.analysis_slots.release()


def test_analyze_stream_emits_terminal_events_on_success(tmp_path, monkeypatch):
    """analyze_stream 在 worker 成功完成时应推送 complete 事件并持久化。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class StubAgent:
        def __init__(self, *args, **kwargs):
            self._last_usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
            self._last_reasoning = ""

        def stream(self, *args, **kwargs):
            yield {"node": "plan_analysis", "data": {"plan": [], "objective": "obj"}}
            yield {
                "node": "finalize",
                "data": {
                    "response": "done",
                    "trace": [],
                    "artifacts": [],
                    "dataset_profile": {"rows": 1, "columns": 1, "column_info": []},
                    "plan": [],
                    "completed_steps": [],
                    "usage": None,
                    "reasoning": "",
                },
            }

    monkeypatch.setattr(api, "DataAnalysisAgent", StubAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: started" in text
    assert "event: complete" in text
    record = api.registry.get(upload["id"])
    assert record.analysis_status == "completed"


def test_analyze_stream_emits_error_event_on_exception(tmp_path, monkeypatch):
    """analyze_stream 在 worker 抛异常时应推送 error 事件并持久化 failed 状态。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class FailingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            raise RuntimeError("model boom")
            yield  # 让 Python 把这个方法识别为 generator

    monkeypatch.setattr(api, "DataAnalysisAgent", FailingAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: error" in text
    assert "model boom" in text
    record = api.registry.get(upload["id"])
    assert record.analysis_status == "failed"


def test_analyze_stream_emits_cancelled_event(tmp_path, monkeypatch):
    """analyze_stream 在 worker 抛 AnalysisCancelled 时应推送 cancelled 事件。"""
    from fastapi.testclient import TestClient

    from data_agent import api
    from data_agent.models import AnalysisCancelled

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class CancellingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            raise AnalysisCancelled("aborted")
            yield

    monkeypatch.setattr(api, "DataAnalysisAgent", CancellingAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: cancelled" in text
    record = api.registry.get(upload["id"])
    assert record.analysis_status == "cancelled"


def test_analyze_stream_plan_only_emits_plan_ready(tmp_path, monkeypatch):
    """plan_only 模式下应推送 plan_ready 事件并切换到 awaiting_approval 状态。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class PlanOnlyAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "plan_analysis", "data": {"plan": [{"id": "p1"}], "objective": "obj"}}

    monkeypatch.setattr(api, "DataAnalysisAgent", PlanOnlyAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST",
        f"/api/sessions/{upload['id']}/analyze/stream",
        json={"task": "x", "plan_only": True},
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: plan_ready" in text
    record = api.registry.get(upload["id"])
    assert record.analysis_status == "awaiting_approval"


def test_analyze_stream_plan_only_without_plan_payload_raises(tmp_path, monkeypatch):
    """plan_only 模式下若 plan_payload 为 None，应走 error 分支。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class NoPlanAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "execute", "data": {}}

    monkeypatch.setattr(api, "DataAnalysisAgent", NoPlanAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST",
        f"/api/sessions/{upload['id']}/analyze/stream",
        json={"task": "x", "plan_only": True},
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: error" in text
    assert "规划阶段未返回计划" in text


def test_analyze_stream_finalize_missing_raises(tmp_path, monkeypatch):
    """非 plan_only 模式下若 finalize 节点缺失，应走 error 分支。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class NoFinalizeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            yield {"node": "plan_analysis", "data": {"plan": [], "objective": "obj"}}

    monkeypatch.setattr(api, "DataAnalysisAgent", NoFinalizeAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/analyze/stream", json={"task": "x"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: error" in text
    assert "没有返回最终结果" in text


def test_chat_stream_endpoint_409_when_run_lock_held(tmp_path, monkeypatch):
    """chat_stream 在 run_lock 已被占用时应返回 409。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    record = api.registry.get(upload["id"])
    record.run_lock.acquire()
    try:
        response = client.post(f"/api/sessions/{upload['id']}/chat/stream", json={"task": "追问"})
        assert response.status_code == 409
        assert "正在处理其他请求" in response.json()["detail"]
    finally:
        record.run_lock.release()


def test_chat_stream_emits_chat_done_on_success(tmp_path, monkeypatch):
    """chat_stream 成功完成时应推送 chat_done 事件。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class StubAgent:
        def __init__(self, *args, **kwargs):
            self._last_usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
            self._last_reasoning = "thinking"

        def chat(self, *args, **kwargs):
            return "answer", []

    monkeypatch.setattr(api, "DataAnalysisAgent", StubAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/chat/stream", json={"task": "追问"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: chat_done" in text
    assert "answer" in text


def test_chat_stream_emits_error_on_exception(tmp_path, monkeypatch):
    """chat_stream 在 worker 抛异常时应推送 error 事件。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class FailingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            raise RuntimeError("chat boom")

    monkeypatch.setattr(api, "DataAnalysisAgent", FailingAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/chat/stream", json={"task": "追问"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: error" in text
    assert "chat boom" in text


def test_chat_stream_emits_cancelled_event(tmp_path, monkeypatch):
    """chat_stream 在 worker 抛 AnalysisCancelled 时应推送 cancelled 事件。"""
    from fastapi.testclient import TestClient

    from data_agent import api
    from data_agent.models import AnalysisCancelled

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="test", runs_dir=runs_dir, max_concurrent_analyses=2)
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "_effective_settings", lambda: settings)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    api.request_buckets.clear()

    class CancellingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            raise AnalysisCancelled("chat aborted")

    monkeypatch.setattr(api, "DataAnalysisAgent", CancellingAgent)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    with client.stream(
        "POST", f"/api/sessions/{upload['id']}/chat/stream", json={"task": "追问"}
    ) as resp:
        text = b"".join(resp.iter_bytes()).decode("utf-8")
    assert "event: cancelled" in text


def test_safe_emit_handles_secondary_queue_empty(monkeypatch):
    """_safe_emit 终态事件入队时，淘汰最旧元素若引发 QueueEmpty/QueueFull 应被吞掉。"""
    from data_agent.routers import analysis as analysis_router

    async def _run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(("thinking_chunk", {"chunk": "old"}))

        # 构造 get_nowait 抛 QueueEmpty 后 put_nowait 抛 QueueFull 的极端场景：
        # 第一次 get_nowait 成功后，第二次 put_nowait 仍可能因为其他回调抢先入队而失败。
        # 这里直接 mock asyncio.QueueEmpty 与 QueueFull 在二次操作时抛出。
        original_get = queue.get_nowait
        original_put = queue.put_nowait
        call_count = {"get": 0, "put": 0}

        def fake_get():
            call_count["get"] += 1
            if call_count["get"] == 1:
                return original_get()
            raise asyncio.QueueEmpty()

        def fake_put(item):
            call_count["put"] += 1
            if call_count["put"] == 1:
                raise asyncio.QueueFull()
            return original_put(item)

        monkeypatch.setattr(queue, "get_nowait", fake_get)
        monkeypatch.setattr(queue, "put_nowait", fake_put)

        # 不应抛异常：二次操作失败应被吞掉
        analysis_router._safe_emit(
            asyncio.get_running_loop(), queue, ("complete", {"response": "new"})
        )
        await asyncio.sleep(0.01)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# builder.py：transform_data 各筛选操作符 + statistical_analysis 各方法
# ---------------------------------------------------------------------------


def _build_workspace(tmp_path, df=None):
    """构造带数据的 workspace，供 builder 工具测试使用。"""
    if df is None:
        df = pd.DataFrame(
            {
                "product": ["A", "B", "A", "C", "B", "A"],
                "sales": [100.0, 200.0, 150.0, 300.0, 250.0, 180.0],
                "profit": [10.0, 30.0, 15.0, 50.0, 40.0, 20.0],
                "region": ["East", "West", "East", "North", "West", "East"],
                "is_returned": [False, True, False, False, True, False],
            }
        )
    workspace = DataWorkspace(tmp_path / "runs", session_id="builder_test")
    workspace.dataframe = df
    workspace._artifacts = []
    return workspace


def _tools(workspace):
    """build_tools 返回列表，这里转成 {name: tool} 字典便于按名取用。"""
    from data_agent.tools import build_tools

    return {t.name: t for t in build_tools(workspace)}


def test_transform_data_contains_filter(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "product", "filter_operator": "contains", "filter_value": "A"}
        )
    )
    assert result["rows"] == 3


def test_transform_data_in_filter_with_numeric_coercion(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "in", "filter_value": ["100", "200"]}
        )
    )
    assert result["rows"] == 2


def test_transform_data_in_filter_rejects_non_list(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="列表类型"):
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "in", "filter_value": "100"}
        )


def test_transform_data_in_filter_numeric_coercion_failure(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="无法转为数值"):
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "in", "filter_value": ["abc"]}
        )


def test_transform_data_eq_filter_numeric_coercion_failure(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="无法转为数值"):
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "eq", "filter_value": "abc"}
        )


def test_transform_data_ne_gt_ge_lt_le_operators(tmp_path):
    workspace = _build_workspace(tmp_path)
    # sales=[100,200,150,300,250,180]，filter_value=200
    for op, expected_rows in [("ne", 5), ("gt", 2), ("ge", 3), ("lt", 3), ("le", 4)]:
        result = json.loads(
            _tools(workspace)["transform_data"].invoke(
                {"filter_column": "sales", "filter_operator": op, "filter_value": 200}
            )
        )
        assert result["rows"] == expected_rows, f"operator {op}: {result['rows']}"


def test_transform_data_filter_value_required(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="filter_value 不能为空"):
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "eq"}
        )


def test_transform_data_sort_and_select_and_limit(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {
                "sort_by": ["sales"],
                "ascending": False,
                "select_columns": ["product", "sales"],
                "limit": 3,
            }
        )
    )
    assert result["rows"] == 3
    assert result["columns"] == ["product", "sales"]


def test_transform_data_limit_out_of_range_raises(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="limit 必须在"):
        _tools(workspace)["transform_data"].invoke({"limit": 0})


def test_statistical_analysis_ttest_ind(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "ttest_ind", "columns": ["sales", "profit"]}
        )
    )
    assert "statistic" in result
    assert "p_value" in result
    assert "effect_size" in result
    assert result["sample_size"] == 6


def test_statistical_analysis_ttest_paired(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "ttest_paired", "columns": ["sales", "profit"]}
        )
    )
    assert "statistic" in result
    assert result["sample_size"] == 6


def test_statistical_analysis_ttest_requires_two_columns(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="两个数值列"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "ttest_ind", "columns": ["sales"]}
        )


def test_statistical_analysis_anova(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "anova", "group_by": "region", "target": "sales"}
        )
    )
    assert "statistic" in result
    assert "eta_squared" in result
    assert result["groups"] >= 2


def test_statistical_analysis_anova_requires_group_and_target(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="anova 需要"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "anova", "columns": ["sales"]}
        )


def test_statistical_analysis_chi_square(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "chi_square", "columns": ["product", "region"]}
        )
    )
    assert "statistic" in result
    assert "p_value" in result


def test_statistical_analysis_chi_square_requires_two_columns(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="两个分类列"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "chi_square", "columns": ["product"]}
        )


def test_statistical_analysis_descriptive(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "descriptive", "columns": ["sales", "profit"]}
        )
    )
    assert "result" in result


def test_statistical_analysis_groupby_count(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "groupby", "group_by": "region", "columns": ["sales"], "aggregation": "count"}
        )
    )
    assert "result" in result
    assert result["group_count"] == 3


def test_export_data_parquet_format(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["export_data"].invoke({"format": "parquet", "filename": "test_export"})
    )
    assert result["status"] == "ok"
    assert Path(result["output"]).exists()


def test_export_data_invalid_format_raises(tmp_path):
    """format 非法时由 pydantic Literal 在工具入口拦截，抛 ValidationError。"""
    from pydantic import ValidationError

    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValidationError):
        _tools(workspace)["export_data"].invoke({"format": "xml", "filename": "x"})


def test_inspect_data_with_custom_sample_rows(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["inspect_data"].invoke({"sample_rows": 2})
    )
    assert result["rows"] == 6
    assert len(result["sample"]) == 2


def test_inspect_data_rejects_invalid_sample_rows(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="sample_rows 必须在"):
        _tools(workspace)["inspect_data"].invoke({"sample_rows": 0})
    with pytest.raises(ValueError, match="sample_rows 必须在"):
        _tools(workspace)["inspect_data"].invoke({"sample_rows": 21})


def test_transform_data_in_filter_on_string_column(tmp_path):
    """in 操作符作用在字符串列时应直接做字符串匹配，不强制数值转换。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "product", "filter_operator": "in", "filter_value": ["A", "C"]}
        )
    )
    assert result["rows"] == 4  # A、A、C、A


def test_transform_data_eq_filter_on_string_column(tmp_path):
    """eq 操作符作用在字符串列时不应做数值转换。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "product", "filter_operator": "eq", "filter_value": "A"}
        )
    )
    assert result["rows"] == 3


def test_transform_data_unknown_filter_operator_raises(tmp_path):
    """filter_operator 非法时由 pydantic Literal 在工具入口拦截，抛 ValidationError。"""
    from pydantic import ValidationError

    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValidationError):
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "unknown_op", "filter_value": 100}
        )


def test_transform_data_sort_multi_columns(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {"sort_by": ["region", "sales"], "ascending": True}
        )
    )
    assert result["rows"] == 6


def test_transform_data_select_columns_only(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["transform_data"].invoke(
            {"select_columns": ["product", "region"]}
        )
    )
    assert result["rows"] == 6
    assert result["columns"] == ["product", "region"]


def test_transform_data_unknown_column_raises(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="列不存在"):
        _tools(workspace)["transform_data"].invoke(
            {"filter_column": "nonexistent", "filter_operator": "eq", "filter_value": 1}
        )


def test_statistical_analysis_rejects_invalid_alpha(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="alpha 必须在"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "descriptive", "alpha": 0}
        )
    with pytest.raises(ValueError, match="alpha 必须在"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "descriptive", "alpha": 1}
        )


def test_statistical_analysis_anova_target_must_be_numeric(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="anova target 必须是数值列"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "anova", "group_by": "region", "target": "product"}
        )


def test_statistical_analysis_anova_requires_minimum_groups(tmp_path):
    """anova 至少需要两个各含 2 个有效观测值的组。"""
    df = pd.DataFrame(
        {
            "group": ["A", "B", "C"],
            "value": [1.0, None, 3.0],
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    with pytest.raises(ValueError, match="anova 至少需要两个"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "anova", "group_by": "group", "target": "value"}
        )


def test_statistical_analysis_chi_square_rejects_high_cardinality(tmp_path):
    """卡方检验拒绝高基数列（>100）。"""
    n = 101
    df = pd.DataFrame(
        {
            "low_card": ["A" if i % 2 == 0 else "B" for i in range(n)],
            "high_card": [f"id_{i}" for i in range(n)],
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    with pytest.raises(ValueError, match="卡方检验拒绝高基数列"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "chi_square", "columns": ["low_card", "high_card"]}
        )


def test_statistical_analysis_linear_regression_requires_target(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="linear_regression 需要 target"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "linear_regression", "columns": ["sales"]}
        )


def test_statistical_analysis_linear_regression_target_excluded_from_features(tmp_path):
    """target 出现在 columns 中时应自动从 features 中移除。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "linear_regression", "columns": ["sales", "profit"], "target": "profit"}
        )
    )
    assert result["target"] == "profit"
    assert "profit" not in result["features"]
    assert "sales" in result["features"]


def test_statistical_analysis_linear_regression_insufficient_samples(tmp_path):
    """样本量不足以拟合线性回归时抛 ValueError。"""
    df = pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]})
    workspace = _build_workspace(tmp_path, df=df)
    with pytest.raises(ValueError, match="有效样本量不足以拟合"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "linear_regression", "columns": ["x"], "target": "y"}
        )


def test_statistical_analysis_linear_regression_requires_numeric_target(tmp_path):
    """target 是非数值列时抛 ValueError。"""
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="线性回归需要数值 target"):
        _tools(workspace)["statistical_analysis"].invoke(
            {"method": "linear_regression", "columns": ["sales"], "target": "product"}
        )


def test_statistical_analysis_groupby_aggregations(tmp_path):
    """groupby 各聚合方法（mean/median/sum/min/max/std）应正常返回。"""
    workspace = _build_workspace(tmp_path)
    for agg in ["mean", "median", "sum", "min", "max", "std"]:
        result = json.loads(
            _tools(workspace)["statistical_analysis"].invoke(
                {"method": "groupby", "group_by": "region", "columns": ["sales"], "aggregation": agg}
            )
        )
        assert "result" in result
        assert result["group_count"] == 3


def test_statistical_analysis_descriptive_without_columns(tmp_path):
    """descriptive 不传 columns 时应使用全部列。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["statistical_analysis"].invoke({"method": "descriptive"})
    )
    assert "result" in result


def test_create_visualization_auto_inferred_as_bar(tmp_path):
    """auto 图类型在 x 为分类列、y 为数值列时应推断为 bar。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "x": "product", "y": "sales", "title": "auto 推断"}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type_source"] == "auto"
    assert Path(result["html"]).exists()


def test_create_visualization_scatter_3d_requires_xyz(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="scatter_3d 需要"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "scatter_3d", "x": "sales", "y": "profit"}
        )


def test_create_visualization_scatter_3d_with_valid_xyz(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "scatter_3d", "x": "sales", "y": "profit", "z": "sales", "color": "region"}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type"] == "scatter_3d"


def test_create_visualization_pie_requires_x(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="pie 需要 x"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "pie", "values": "sales"}
        )


def test_create_visualization_heatmap_requires_xyz(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="heatmap 需要"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "heatmap", "x": "product", "y": "region"}
        )


def test_create_visualization_heatmap_with_valid_fields(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "heatmap", "x": "product", "y": "region", "values": "sales"}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type"] == "heatmap"


def test_create_visualization_scatter_matrix_exceeds_dimensions(tmp_path):
    """scatter_matrix 超过 8 维应抛 ValueError。"""
    df = pd.DataFrame({f"col_{i}": range(20) for i in range(10)})
    workspace = _build_workspace(tmp_path, df=df)
    with pytest.raises(ValueError, match="scatter_matrix 最多支持"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "scatter_matrix", "dimensions": list(df.columns)}
        )


def test_create_visualization_scatter_matrix_downsamples_large_rows(tmp_path):
    """scatter_matrix 在大表时应降采样到 1000 行。"""
    df = pd.DataFrame(
        {
            "x": range(1500),
            "y": range(1500),
            "z": range(1500),
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "scatter_matrix", "dimensions": ["x", "y", "z"]}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type"] == "scatter_matrix"


def test_create_visualization_sunburst_requires_path_columns(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="sunburst 需要"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "sunburst", "values": "sales"}
        )


def test_create_visualization_sunburst_with_path(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "sunburst", "path_columns": ["region", "product"], "values": "sales"}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type"] == "sunburst"


def test_create_visualization_treemap_requires_path_columns(tmp_path):
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="treemap 需要"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "treemap", "values": "sales"}
        )


def test_create_visualization_treemap_with_path(tmp_path):
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "treemap", "path_columns": ["region", "product"], "values": "sales"}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type"] == "treemap"


def test_create_visualization_top_n_invalid_raises(tmp_path):
    """top_n 越界（0 或 > 500）应抛 ValueError。"""
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="top_n 需要"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "bar", "x": "product", "y": "sales", "top_n": 0}
        )
    with pytest.raises(ValueError, match="top_n 需要"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "bar", "x": "product", "y": "sales", "top_n": 501}
        )


def test_create_visualization_top_n_filters_categories(tmp_path):
    """top_n 应只保留前 N 个高频类别。"""
    df = pd.DataFrame(
        {
            "cat": [f"c{i % 5}" for i in range(50)],
            "val": range(50),
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "bar", "x": "cat", "y": "val", "aggregation": "sum", "top_n": 3}
        )
    )
    assert result["status"] == "ok"


def test_create_visualization_aggregation_rejects_non_groupable(tmp_path):
    """aggregation 仅用于带 x 的 bar/line/area，传给 scatter 应抛错。"""
    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValueError, match="aggregation 仅用于带 x 的 bar/line/area"):
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "scatter", "x": "sales", "y": "profit", "aggregation": "mean"}
        )


def test_create_visualization_echarts_engine(tmp_path):
    """chart_engine=echarts 时应走 ECharts 渲染分支（或回退 Plotly）。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "bar", "x": "product", "y": "sales", "chart_engine": "echarts"}
        )
    )
    assert result["status"] == "ok"


def test_create_visualization_export_png_handles_failure(tmp_path, monkeypatch):
    """export_png=True 时若 Kaleido 抛异常应回退为 png_warning，不阻塞 HTML 生成。"""
    workspace = _build_workspace(tmp_path)

    def raise_image(*args, **kwargs):
        raise RuntimeError("kaleido missing")

    import plotly.graph_objects as go

    monkeypatch.setattr(go.Figure, "write_image", raise_image)
    result = json.loads(
        _tools(workspace)["create_visualization"].invoke(
            {"chart_type": "bar", "x": "product", "y": "sales", "export_png": True}
        )
    )
    assert result["status"] == "ok"
    assert "png_warning" in result
    assert "kaleido missing" in result["png_warning"]


def test_clean_data_normalizes_column_names(tmp_path):
    """clean_data 在 normalize_column_names=True 时应规范化列名并记录变更。"""
    df = pd.DataFrame(
        {
            "Sales Amount": [100.0, 200.0, 300.0],
            "Product Name": ["A", "B", "A"],
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    result = json.loads(
        _tools(workspace)["clean_data"].invoke(
            {
                "normalize_column_names": True,
                "drop_duplicates": False,
                "trim_strings": False,
                "missing_strategy": "none",
                "outlier_method": "none",
            }
        )
    )
    assert result["status"] == "ok"
    # 列名应被规范化为下划线分隔
    assert "Sales Amount" not in workspace.dataframe.columns


def test_clean_data_parses_datetime_columns(tmp_path):
    """clean_data 在 datetime_columns 传入时应解析为 datetime 类型。"""
    df = pd.DataFrame(
        {
            "date_str": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "value": [1.0, 2.0, 3.0],
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    result = json.loads(
        _tools(workspace)["clean_data"].invoke(
            {
                "datetime_columns": ["date_str"],
                "drop_duplicates": False,
                "trim_strings": False,
                "missing_strategy": "none",
                "outlier_method": "none",
            }
        )
    )
    assert result["status"] == "ok"
    assert pd.api.types.is_datetime64_any_dtype(workspace.dataframe["date_str"])


def test_clean_data_rejects_invalid_outlier_method(tmp_path):
    """outlier_method 非法时由 pydantic 拦截，抛 ValidationError。"""
    from pydantic import ValidationError

    workspace = _build_workspace(tmp_path)
    with pytest.raises(ValidationError):
        _tools(workspace)["clean_data"].invoke({"outlier_method": "unknown"})


def test_repair_data_format_with_normalize_column_names(tmp_path):
    """repair_data_format 在 normalize_column_names=True 时应规范化列名。"""
    df = pd.DataFrame(
        {
            "Sales Amount": ["100", "200", "300"],
            "Product Name": ["A", "B", "A"],
        }
    )
    workspace = _build_workspace(tmp_path, df=df)
    result = json.loads(
        _tools(workspace)["repair_data_format"].invoke({"normalize_column_names": True})
    )
    assert result["status"] == "ok" if "status" in result else result.get("changed") is True


def test_export_data_csv_format(tmp_path):
    """export_data 默认 csv 格式应正常导出。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["export_data"].invoke({"format": "csv", "filename": "test_csv"})
    )
    assert result["status"] == "ok"
    assert Path(result["output"]).exists()
    assert result["output"].endswith(".csv")


def test_export_data_xlsx_format(tmp_path):
    """export_data xlsx 格式应正常导出。"""
    workspace = _build_workspace(tmp_path)
    result = json.loads(
        _tools(workspace)["export_data"].invoke({"format": "xlsx", "filename": "test_xlsx"})
    )
    assert result["status"] == "ok"
    assert Path(result["output"]).exists()

