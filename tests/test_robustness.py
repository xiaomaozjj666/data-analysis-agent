"""补齐低覆盖模块的剩余分支测试：credentials / config / serialization / errors /
middleware / callbacks / cli / agent / api 前端 catch-all。

目标：逐行覆盖此前未触达的防御性分支（keyring 降级、配置校验、序列化类型
分支、速率限制清理、前端静态资源防护等）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest

from data_agent.config import AgentSettings
from data_agent.errors import api_error

# ---------------------------------------------------------------------------
# credentials.py：keyring 异常降级分支
# ---------------------------------------------------------------------------


def test_get_saved_api_key_returns_empty_on_keyring_error(monkeypatch):
    import keyring

    from data_agent import credentials

    def boom(*args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    monkeypatch.setattr(keyring, "get_password", boom)
    assert credentials.get_saved_api_key() == ""


def test_save_api_key_returns_false_and_logs_on_keyring_error(monkeypatch, caplog):
    import keyring

    from data_agent import credentials

    def boom(*args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    monkeypatch.setattr(keyring, "set_password", boom)
    with caplog.at_level(logging.WARNING, logger="data_agent.credentials"):
        assert credentials.save_api_key("sk-test") is False
    assert "凭据存储" in caplog.text


def test_delete_api_key_returns_false_and_logs_on_keyring_error(monkeypatch, caplog):
    import keyring

    from data_agent import credentials

    def boom(*args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    monkeypatch.setattr(keyring, "delete_password", boom)
    with caplog.at_level(logging.WARNING, logger="data_agent.credentials"):
        assert credentials.delete_saved_api_key() is False
    assert "凭据存储" in caplog.text


# ---------------------------------------------------------------------------
# config.py：validate_for_model 全分支
# ---------------------------------------------------------------------------


def test_validate_for_model_rejects_unknown_provider():
    with pytest.raises(ValueError, match="不支持的模型提供商"):
        AgentSettings(api_key="x", provider="anthropic").validate_for_model()


def test_validate_for_model_rejects_empty_api_key():
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        AgentSettings(api_key="", provider="deepseek").validate_for_model()
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        AgentSettings(api_key="", provider="openai").validate_for_model()


def test_validate_for_model_rejects_empty_model():
    with pytest.raises(ValueError, match="模型名称不能为空"):
        AgentSettings(api_key="x", model="").validate_for_model()


@pytest.mark.parametrize(
    "kwargs,pattern",
    [
        ({"max_iterations": 0}, "AGENT_MAX_ITERATIONS"),
        ({"max_iterations": 101}, "AGENT_MAX_ITERATIONS"),
        ({"max_plan_steps": 1}, "AGENT_MAX_PLAN_STEPS"),
        ({"max_plan_steps": 13}, "AGENT_MAX_PLAN_STEPS"),
        ({"max_upload_bytes": 0}, "资源上限"),
        ({"max_rows": 0}, "资源上限"),
        ({"max_cells": 0}, "资源上限"),
        ({"max_active_sessions": 0}, "会话资源上限"),
        ({"session_ttl_hours": 0}, "会话资源上限"),
        ({"rate_limit_per_minute": 0}, "RATE_LIMIT_PER_MINUTE"),
        ({"max_concurrent_analyses": 0}, "MAX_CONCURRENT_ANALYSES"),
        ({"language": "fr"}, "language"),
    ],
)
def test_validate_for_model_rejects_bad_resource_limits(kwargs, pattern):
    with pytest.raises(ValueError, match=pattern):
        AgentSettings(api_key="x", **kwargs).validate_for_model()


# ---------------------------------------------------------------------------
# serialization.py：np 类型 / ndarray / 未知对象回退
# ---------------------------------------------------------------------------


def test_to_jsonable_handles_numpy_scalars_and_arrays():
    from data_agent.serialization import to_jsonable

    assert to_jsonable(np.int64(42)) == 42
    assert to_jsonable(np.float64(1.5)) == 1.5
    assert to_jsonable(np.float64(np.nan)) is None
    assert to_jsonable(np.array([1, 2, 3])) == [1, 2, 3]


def test_to_jsonable_falls_back_to_str_for_unknown_objects():
    from data_agent.serialization import to_jsonable

    class Weird:
        def __str__(self):
            return "weird-repr"

    assert to_jsonable(Weird()) == "weird-repr"


# ---------------------------------------------------------------------------
# errors.py：api_error 辅助
# ---------------------------------------------------------------------------


def test_api_error_builds_http_exception():
    exc = api_error(418, "自定义错误")
    assert exc.status_code == 418
    assert exc.detail == "自定义错误"


# ---------------------------------------------------------------------------
# middleware.py：token 前缀剥离 / 速率限制清理与 429 / 生产环境警告
# ---------------------------------------------------------------------------


def _fake_request(host: str = "client-ip", method: str = "POST", path: str = "/api/sessions"):
    class FakeURL:
        def __init__(self, value):
            self.path = value

    class FakeClient:
        def __init__(self, value):
            self.host = value

    class FakeRequest:
        def __init__(self):
            self.method = method
            self.url = FakeURL(path)
            self.client = FakeClient(host) if host else None
            self.headers = {}

    return FakeRequest()


def test_access_token_accepts_bearer_prefix_on_app_token_header(monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    monkeypatch.setenv("APP_ACCESS_TOKEN", "secret-token")
    client = TestClient(api.app)
    response = client.get("/api/settings", headers={"X-App-Token": "Bearer secret-token"})
    assert response.status_code == 200


def test_access_token_accepts_bearer_prefix_on_authorization_header(monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api

    monkeypatch.setenv("APP_ACCESS_TOKEN", "secret-token")
    client = TestClient(api.app)
    response = client.get("/api/settings", headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200


def test_client_identifier_falls_back_to_unknown_without_client():
    from data_agent.middleware import _client_identifier

    class FakeRequest:
        headers = {}
        client = None

    assert _client_identifier(FakeRequest()) == "unknown"


def test_rate_limit_prunes_expired_entries(monkeypatch):
    from data_agent import api
    from data_agent.middleware import _check_rate_limit

    monkeypatch.setattr(
        api,
        "bootstrap_settings",
        AgentSettings(api_key="x", rate_limit_per_minute=2, runs_dir=Path("runs")),
    )
    fresh: dict[str, deque] = {}
    monkeypatch.setattr(api, "request_buckets", fresh)
    now = time.monotonic()
    fresh["client-ip"] = deque([now - 120, now - 61])  # 两条都已过期

    _check_rate_limit(_fake_request())
    # 过期条目被 popleft 清理，只剩本次请求的新条目
    assert len(fresh["client-ip"]) == 1


def test_rate_limit_raises_429_when_bucket_exhausted(monkeypatch):
    from fastapi import HTTPException

    from data_agent import api
    from data_agent.middleware import _check_rate_limit

    monkeypatch.setattr(
        api,
        "bootstrap_settings",
        AgentSettings(api_key="x", rate_limit_per_minute=2, runs_dir=Path("runs")),
    )
    fresh: dict[str, deque] = {}
    monkeypatch.setattr(api, "request_buckets", fresh)
    now = time.monotonic()
    fresh["client-ip"] = deque([now, now])  # 配额已用满

    with pytest.raises(HTTPException) as exc_info:
        _check_rate_limit(_fake_request())
    assert exc_info.value.status_code == 429


def test_setup_middleware_warns_when_production_without_token(monkeypatch, caplog):
    from fastapi import FastAPI

    from data_agent.middleware import setup_middleware

    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("RENDER", "1")
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="data_agent.middleware"):
        setup_middleware(app)
    assert "APP_ACCESS_TOKEN is empty" in caplog.text


# ---------------------------------------------------------------------------
# callbacks.py：CancelCallback 全钩子 / ReasoningStreamCallback 异常 / UsageAccumulator
# ---------------------------------------------------------------------------


def test_cancel_callback_hooks_raise_when_event_set():
    from data_agent.callbacks import CancelCallback
    from data_agent.models import AnalysisCancelled

    event = threading.Event()
    cb = CancelCallback(event)
    # 未设置：三个钩子都不抛
    cb.on_llm_start()
    cb.on_chat_model_start()
    cb.on_tool_start()
    event.set()
    with pytest.raises(AnalysisCancelled):
        cb.on_llm_start()
    with pytest.raises(AnalysisCancelled):
        cb.on_chat_model_start()
    with pytest.raises(AnalysisCancelled):
        cb.on_tool_start()


def test_reasoning_stream_callback_swallows_callback_exception():
    from data_agent.callbacks import ReasoningStreamCallback

    def raise_cb(et, p):
        raise RuntimeError("closed")

    buffer: list[str] = []
    cb = ReasoningStreamCallback(raise_cb, buffer=buffer)

    class FakeChunk:
        class _Msg:
            additional_kwargs = {"reasoning_content": "思考中"}

        message = _Msg()

    cb.on_llm_new_token("", chunk=FakeChunk())  # 不应抛出
    assert buffer == ["思考中"]


def test_usage_accumulator_skips_generation_without_message():
    from data_agent.callbacks import UsageAccumulator

    class FakeGen:
        message = None

    class FakeResp:
        llm_output = {}
        generations = [[FakeGen()]]

    acc = UsageAccumulator()
    acc.on_llm_end(FakeResp())
    assert acc.snapshot() == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# agent.py：DeepSeek 关闭 thinking 时走 temperature 分支
# ---------------------------------------------------------------------------


def test_deepseek_model_without_thinking_sets_temperature(monkeypatch):
    from langchain_deepseek import ChatDeepSeek

    from data_agent.agent import create_chat_model

    captured: dict = {}

    class SpyChatDeepSeek(ChatDeepSeek):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("data_agent.agent.ChatDeepSeek", SpyChatDeepSeek)
    settings = AgentSettings(
        provider="deepseek",
        api_key="k",
        model="m",
        thinking_enabled=False,
        temperature=0.3,
    )
    create_chat_model(settings)
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["temperature"] == 0.3
    assert "reasoning_effort" not in captured


# ---------------------------------------------------------------------------
# cli.py：provider/model/base_url 覆盖 + 产物打印 + __main__ 守卫
# ---------------------------------------------------------------------------


def test_cli_analyze_with_overrides_and_artifacts(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from data_agent import agent as agent_module
    from data_agent import cli as cli_module
    from data_agent.agent import AnalysisResult

    data_path = tmp_path / "sales.csv"
    data_path.write_text("region,sales\nEast,100\nWest,200\n", encoding="utf-8")
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)

    fake_artifact = {"kind": "dataset", "path": str(tmp_path / "out.csv")}

    class StubAgent:
        def __init__(self, workspace, settings, **kwargs):
            self.workspace = workspace
            self.settings = settings

        def run(self, task, history=None, resume_from=None):
            assert self.settings.provider == "openai"
            assert self.settings.model == "gpt-test"
            assert self.settings.base_url == "http://localhost:9999"
            return AnalysisResult(
                response="完成",
                trace=[],
                artifacts=[fake_artifact],
                dataset_profile=self.workspace.profile(),
                plan=[],
                completed_steps=[],
            )

    # cli.py 在模块顶部 `from data_agent.agent import DataAnalysisAgent` 绑定，
    # 必须替换 cli 模块命名空间里的引用；agent_module 的绑定不影响 cli。
    monkeypatch.setattr(cli_module, "DataAnalysisAgent", StubAgent)
    monkeypatch.setattr(agent_module, "DataAnalysisAgent", StubAgent)

    from data_agent.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "analyze", str(data_path), "--task", "检查", "--provider", "openai",
            "--model", "gpt-test", "--base-url", "http://localhost:9999",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "完成" in result.output
    assert "产物" in result.output
    assert "out.csv" in result.output


def test_cli_main_guard_invokes_app(monkeypatch):
    """__main__ 守卫：runpy 执行 cli 模块主体时应调用 app()（--help 打印后退出）。"""
    import runpy
    import sys

    from data_agent import cli

    monkeypatch.setattr(sys, "argv", ["data-agent", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(cli.__file__)), run_name="__main__")
    assert exc_info.value.code in (0, None)


# ---------------------------------------------------------------------------
# api.py：前端静态资源 catch-all（SPA fallback / 缓存头 / API 404 / 穿越防护）
# ---------------------------------------------------------------------------


def test_frontend_catchall_serves_spa_and_static_assets(monkeypatch):
    """真实 frontend/dist 存在时：SPA fallback、静态资源 immutable 缓存、API 404。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    # 屏蔽本地 .env 注入的访问令牌，避免 401 干扰 404 断言
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)

    if not api.frontend_dist.is_dir():
        pytest.skip("frontend/dist 不存在，catch-all 未注册")

    client = TestClient(api.app)

    spa = client.get("/some/spa/route")
    assert spa.status_code == 200
    assert "no-cache" in spa.headers["cache-control"]
    assert "<html" in spa.text.lower() or "<!doctype" in spa.text.lower()

    # 注意：/assets 由 StaticFiles 挂载处理（不带缓存头），
    # catch-all 的 immutable 缓存分支在 test_frontend_catchall_blocks_traversal_and_symlink
    # 中用临时 dist 根目录下的普通文件覆盖。

    # API 前缀必须 404，不能回退到 SPA（docs/openapi.json 是 FastAPI 自带路由，
    # 由它们自己处理；catch-all 里的 docs 前缀分支属防御性代码）
    assert client.get("/api/not-a-real-endpoint").status_code == 404


def test_frontend_catchall_blocks_traversal_and_symlink(tmp_path, monkeypatch):
    """catch-all 的路径穿越与符号链接防护（临时 dist 构造恶意场景）。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")

    # catch-all 闭包引用模块级 frontend_dist 全局，替换后新请求即生效
    monkeypatch.setattr(api, "frontend_dist", dist)
    client = TestClient(api.app)

    assert client.get("/some/route").status_code == 200
    static = client.get("/app.js")
    assert static.status_code == 200
    assert static.headers["cache-control"] == "public, max-age=31536000, immutable"

    # URL 编码的 ../ 穿越：不得泄露 dist 外文件内容
    traversal = client.get("/%2e%2e/secret.txt")
    assert "top secret" not in traversal.text

    # 符号链接指向 dist 外文件 → 404，不跟随。
    # 当前环境（无管理员权限）无法创建真实 symlink，用 monkeypatch 模拟：
    # 任何路径都被判为符号链接时，静态文件服务必须拒绝（走 404 而非 SPA）。
    monkeypatch.setattr(api.Path, "is_symlink", lambda self: True)
    assert client.get("/app.js").status_code == 404


def test_frontend_catchall_404_when_dist_missing(monkeypatch):
    """dist 目录不存在（如仅部署后端、前端未构建）时，catch-all 必须
    返回 404 而不是对缺失文件调用 FileResponse 触发 500。"""
    from fastapi.testclient import TestClient

    from data_agent import api

    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(api, "frontend_dist", api.frontend_dist / "no-such-build")
    client = TestClient(api.app)
    assert client.get("/some/spa/route").status_code == 404
    # API 前缀逻辑不受影响
    assert client.get("/api/not-a-real-endpoint").status_code == 404


# ---------------------------------------------------------------------------
# artifacts.py：bundle 缓存错误路径 / latin-1 兜底 / 内联失败保持原样
# ---------------------------------------------------------------------------


def test_read_bundle_cached_error_and_eviction(tmp_path, monkeypatch):
    from pathlib import Path as RealPath

    from data_agent.routers import artifacts as artifacts_router

    # stat 失败 → None
    assert artifacts_router._read_bundle_cached(tmp_path / "missing.js") is None

    # read_text 失败 → None
    bundle = tmp_path / "bundle.js"
    bundle.write_text("/* js */", encoding="utf-8")

    def failing_read(self, *a, **k):
        raise OSError("io error")

    monkeypatch.setattr(RealPath, "read_text", failing_read)
    assert artifacts_router._read_bundle_cached(bundle) is None

    # 缓存满 → 淘汰最旧条目
    monkeypatch.setattr(RealPath, "read_text", lambda self, *a, **k: "/* x */")
    cache: dict = {}
    monkeypatch.setattr(artifacts_router, "_BUNDLE_TEXT_CACHE", cache)
    for index in range(8):
        p = tmp_path / f"bundle_{index}.js"
        p.write_text("/* x */", encoding="utf-8")
        artifacts_router._read_bundle_cached(p)
    assert len(cache) <= artifacts_router._BUNDLE_CACHE_MAX


def test_inline_bundles_return_original_when_read_fails(tmp_path, monkeypatch):
    from data_agent.routers import artifacts as artifacts_router

    class FakeWorkspace:
        artifacts_dir = tmp_path

    class FakeRecord:
        workspace = FakeWorkspace()

    echarts = tmp_path / "echarts.min.js"
    echarts.write_text("/* js */", encoding="utf-8")
    monkeypatch.setattr(artifacts_router, "_read_bundle_cached", lambda path: None)
    html = '<script src="echarts.min.js"></script>'
    assert artifacts_router._inline_echarts_bundle(FakeRecord(), html) == html

    plotly = tmp_path / "plotly.min.js"
    plotly.write_text("/* js */", encoding="utf-8")
    html2 = "<script src='plotly.min.js'></script>"
    assert artifacts_router._inline_plotly_bundle(FakeRecord(), html2) == html2


def test_read_utf8_robust_latin1_fallback(tmp_path, monkeypatch):
    from data_agent.routers import artifacts as artifacts_router

    # 所有候选编码都不可用时走 latin-1 兜底（绝不抛错）
    monkeypatch.setattr(artifacts_router, "_PREVIEW_TEXT_CANDIDATES", ())
    p = tmp_path / "bin.html"
    p.write_bytes(b"\xff\xfe\x00binary")
    assert artifacts_router._read_utf8_robust(p) == "\xff\xfe\x00binary"
