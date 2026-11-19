"""kaleido 就绪等待补丁与渲染环境判定的测试。

全部使用假 tab / 假模块，不启动任何浏览器，因此不受 ``DATA_AGENT_SKIP_KALEIDO`` 控制。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
import threading
import types

import pytest
from fastapi.testclient import TestClient

from data_agent import _kaleido_compat as compat

# ---------------------------------------------------------------------------
# 夹具：把补丁状态与"已安装的 kaleido"都隔离成假的
# ---------------------------------------------------------------------------


class _FakeKaleidoTab:
    """假的 ``_KaleidoTab``：navigate 只记录调用并设置 JS 上下文 id。"""

    def __init__(self, tab=None):
        self.tab = tab
        self._current_js_id = 7
        self.navigated: list[tuple] = []

    async def navigate(self, url=""):
        self.navigated.append((url,))
        return "nav-result"


class _FakeCdpTab:
    """假的 choreographer tab：按脚本依次返回 Runtime.evaluate 的结果。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def send_command(self, command, params=None):
        self.calls.append((command, params))
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def _evaluate_result(value):
    return {"result": {"result": {"type": "boolean", "value": value}}}


@pytest.fixture()
def fake_kaleido(monkeypatch):
    """装一个假的 ``kaleido._kaleido_tab`` 模块，版本伪装成 1.5.1，补丁状态复位。"""
    fake_pkg = types.ModuleType("kaleido")
    fake_mod = types.ModuleType("kaleido._kaleido_tab")
    fake_mod._KaleidoTab = type("_KaleidoTab", (_FakeKaleidoTab,), {})
    monkeypatch.setitem(sys.modules, "kaleido", fake_pkg)
    monkeypatch.setitem(sys.modules, "kaleido._kaleido_tab", fake_mod)
    monkeypatch.setattr(compat, "_installed_kaleido_version", lambda: "1.5.1")
    monkeypatch.setattr(compat, "_installed", False)
    return fake_mod


# ---------------------------------------------------------------------------
# install / is_installed
# ---------------------------------------------------------------------------


def test_install_wraps_navigate_and_marks_installed(fake_kaleido):
    original = fake_kaleido._KaleidoTab.navigate
    assert compat.is_installed() is False
    assert compat.install() is True
    assert compat.is_installed() is True
    wrapped = fake_kaleido._KaleidoTab.navigate
    assert wrapped is not original
    assert getattr(wrapped, compat._MARKER) is True
    assert wrapped.__name__ == "navigate"  # functools.wraps 保留原名


def test_install_is_idempotent(fake_kaleido):
    assert compat.install() is True
    first = fake_kaleido._KaleidoTab.navigate
    assert compat.install() is True
    assert fake_kaleido._KaleidoTab.navigate is first  # 没有二次包装


def test_install_does_not_double_wrap_when_state_flag_was_reset(fake_kaleido, monkeypatch):
    """状态位丢失（如本模块被重载）但 navigate 已带标记时，不再叠一层包装。"""
    assert compat.install() is True
    first = fake_kaleido._KaleidoTab.navigate
    monkeypatch.setattr(compat, "_installed", False)
    assert compat.install() is True
    assert fake_kaleido._KaleidoTab.navigate is first
    assert compat.is_installed() is True


def test_install_is_thread_safe(fake_kaleido):
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(compat.install())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 8
    wrapped = fake_kaleido._KaleidoTab.navigate
    # 只包装了一层：解包一次后应是原始的、不带标记的 navigate。
    assert getattr(wrapped.__wrapped__, compat._MARKER, False) is False


@pytest.mark.parametrize("version", ["1.5.0", "1.6.0", "2.0.0", "0.2.1", None])
def test_install_skips_unsupported_version(fake_kaleido, monkeypatch, version):
    monkeypatch.setattr(compat, "_installed_kaleido_version", lambda: version)
    original = fake_kaleido._KaleidoTab.navigate
    assert compat.install() is False
    assert compat.is_installed() is False
    assert fake_kaleido._KaleidoTab.navigate is original


def test_install_skips_when_tab_class_missing(fake_kaleido, caplog):
    del fake_kaleido._KaleidoTab
    with caplog.at_level("DEBUG", logger=compat.log.name):
        assert compat.install() is False
    assert compat.is_installed() is False
    assert any("结构与预期不符" in record.message for record in caplog.records)


def test_install_skips_when_navigate_missing(fake_kaleido):
    fake_kaleido._KaleidoTab = type("_KaleidoTab", (), {})  # 结构变了：没有 navigate
    assert compat.install() is False
    assert compat.is_installed() is False


def test_install_skips_when_navigate_is_not_coroutine(fake_kaleido):
    fake_kaleido._KaleidoTab.navigate = lambda self, url="": None
    original = fake_kaleido._KaleidoTab.navigate
    assert compat.install() is False
    assert fake_kaleido._KaleidoTab.navigate is original


def test_install_skips_when_kaleido_import_fails(monkeypatch):
    """kaleido 元数据存在但导入失败（装坏了）：不打补丁、不抛异常。"""
    monkeypatch.setattr(compat, "_installed_kaleido_version", lambda: "1.5.1")
    monkeypatch.setattr(compat, "_installed", False)
    monkeypatch.setitem(
        sys.modules, "kaleido._kaleido_tab", None
    )  # None 会让 import 抛 ImportError
    assert compat.install() is False
    assert compat.is_installed() is False


def test_installed_kaleido_version_reads_metadata(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.5.1")
    assert compat._installed_kaleido_version() == "1.5.1"

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert compat._installed_kaleido_version() is None


def test_import_does_not_load_kaleido():
    """导入补丁模块本身不得拖入 kaleido（保护 import data_agent.api 的启动速度）。"""
    import subprocess

    code = "import sys; import data_agent._kaleido_compat; print('kaleido' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert out.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# 包装后的 navigate 行为
# ---------------------------------------------------------------------------


def test_wrapped_navigate_returns_original_result_and_waits(fake_kaleido):
    compat.install()
    cdp = _FakeCdpTab([_evaluate_result(False), _evaluate_result(False), _evaluate_result(True)])
    tab = fake_kaleido._KaleidoTab(cdp)

    result = asyncio.run(tab.navigate("file:///index.html"))

    assert result == "nav-result"
    assert tab.navigated == [("file:///index.html",)]
    assert len(cdp.calls) == 3  # 前两次未就绪，第三次就绪
    command, params = cdp.calls[0]
    assert command == "Runtime.evaluate"
    assert params == {
        "expression": "typeof kaleido_scopes === 'object'",
        "returnByValue": True,
        "contextId": 7,
    }


def test_wrapped_navigate_propagates_original_exception(fake_kaleido):
    class Boom(_FakeKaleidoTab):
        async def navigate(self, url=""):
            raise RuntimeError("导航失败")

    fake_kaleido._KaleidoTab = Boom
    compat.install()
    cdp = _FakeCdpTab([_evaluate_result(True)])
    with pytest.raises(RuntimeError, match="导航失败"):
        asyncio.run(Boom(cdp).navigate())
    assert cdp.calls == []  # 原导航失败就不再轮询


def test_wrapped_navigate_times_out_without_raising(fake_kaleido, monkeypatch, caplog):
    """脚本始终没就绪：到上限后放行并记 warning，把原本的报错路径留给调用方。"""
    monkeypatch.setattr(compat, "_WAIT_LIMIT", 0.05)
    monkeypatch.setattr(compat, "_POLL_INTERVAL", 0.01)
    compat.install()
    cdp = _FakeCdpTab([_evaluate_result(False)])
    tab = fake_kaleido._KaleidoTab(cdp)
    with caplog.at_level("WARNING", logger=compat.log.name):
        result = asyncio.run(tab.navigate())
    assert result == "nav-result"
    assert len(cdp.calls) >= 2
    assert any("未就绪" in record.message for record in caplog.records)


def test_wrapped_navigate_tolerates_tab_without_context_id(fake_kaleido):
    """取不到 _current_js_id 时不带 contextId，落到当前页面上下文。"""
    compat.install()
    cdp = _FakeCdpTab([_evaluate_result(True)])
    tab = fake_kaleido._KaleidoTab(cdp)
    tab._current_js_id = None
    asyncio.run(tab.navigate())
    assert "contextId" not in cdp.calls[0][1]


# ---------------------------------------------------------------------------
# _wait_until_ready / _is_ready
# ---------------------------------------------------------------------------


def test_wait_until_ready_returns_true_immediately():
    cdp = _FakeCdpTab([_evaluate_result(True)])
    assert asyncio.run(compat._wait_until_ready(cdp, 3)) is True
    assert len(cdp.calls) == 1


def test_wait_until_ready_times_out_with_fake_tab():
    cdp = _FakeCdpTab([_evaluate_result(False)])
    ok = asyncio.run(compat._wait_until_ready(cdp, 3, limit=0.05, interval=0.01))
    assert ok is False
    assert len(cdp.calls) >= 2


def test_wait_until_ready_zero_limit_probes_once():
    cdp = _FakeCdpTab([_evaluate_result(False)])
    assert asyncio.run(compat._wait_until_ready(cdp, 3, limit=0, interval=0.01)) is False
    assert len(cdp.calls) == 1


def test_wait_until_ready_survives_probe_exceptions():
    """单次探测抛异常（页面刷新、会话关闭）当作未就绪继续轮询，之后就绪仍返回 True。"""
    cdp = _FakeCdpTab([RuntimeError("会话已关闭"), _evaluate_result(True)])
    assert asyncio.run(compat._wait_until_ready(cdp, 3, limit=1, interval=0.01)) is True
    assert len(cdp.calls) == 2


def test_wait_until_ready_gives_up_when_probe_always_raises():
    cdp = _FakeCdpTab([RuntimeError("永远失败")])
    assert asyncio.run(compat._wait_until_ready(cdp, 3, limit=0.05, interval=0.01)) is False


def test_wait_until_ready_default_limit_is_ten_seconds():
    assert compat._WAIT_LIMIT == 10.0
    assert compat._POLL_INTERVAL == 0.1


@pytest.mark.parametrize(
    "response, expected",
    [
        (_evaluate_result(True), True),
        (_evaluate_result(False), False),
        (_evaluate_result("true"), False),  # 只认布尔真值
        (_evaluate_result(1), False),
        ({"error": {"code": -32000, "message": "x"}}, False),
        ({"result": {"exceptionDetails": {}}}, False),
        ({"result": {"result": {}}}, False),
        ({"result": {"result": "x"}}, False),
        ({"result": None}, False),
        ({}, False),
        (None, False),
        ("not a dict", False),
    ],
)
def test_is_ready_parsing(response, expected):
    assert compat._is_ready(response) is expected


# ---------------------------------------------------------------------------
# render_env_problem：环境缺失 vs 渲染失败
# ---------------------------------------------------------------------------


def test_env_problem_for_import_error():
    assert compat.render_env_problem(ImportError("no kaleido")) == compat.KALEIDO_MISSING_HINT


def test_env_problem_for_plotly_kaleido_required_message():
    exc = RuntimeError("\nImage export requires the Kaleido package, v1.0.0 or greater")
    assert compat.render_env_problem(exc) == compat.KALEIDO_MISSING_HINT


def test_env_problem_walks_cause_chain_for_chrome_not_found():
    """plotly 把 ChromeNotFoundError 改包成普通 RuntimeError，必须沿异常链识别。"""
    ChromeNotFoundError = type("ChromeNotFoundError", (RuntimeError,), {})
    try:
        try:
            raise ChromeNotFoundError("Browser not found")
        except ChromeNotFoundError:
            raise RuntimeError("Kaleido requires Google Chrome") from None
    except RuntimeError as wrapped:
        # from None 会抑制 __cause__，但 __context__ 仍保留
        assert wrapped.__cause__ is None
        assert compat.render_env_problem(wrapped) == compat.CHROME_MISSING_HINT


def test_env_problem_walks_explicit_cause():
    BrowserDepsError = type("BrowserDepsError", (RuntimeError,), {})
    outer = RuntimeError("渲染失败")
    outer.__cause__ = BrowserDepsError("缺系统库")
    assert compat.render_env_problem(outer) == compat.CHROME_MISSING_HINT


def test_env_problem_matches_subclass_by_mro():
    base = type("ChromeNotFoundError", (RuntimeError,), {})
    child = type("MyChromeError", (base,), {})
    assert compat.render_env_problem(child("x")) == compat.CHROME_MISSING_HINT


def test_env_problem_none_for_generic_render_failure():
    assert compat.render_env_problem(RuntimeError("ReferenceError: kaleido_scopes")) is None
    assert compat.render_env_problem(ValueError("bad figure")) is None


def test_env_problem_handles_cyclic_exception_chain():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert compat.render_env_problem(a) is None


def test_env_problem_stops_at_depth_limit():
    """异常链超过深度上限时放弃继续追，避免极端情况下的长循环。"""
    deepest = type("ChromeNotFoundError", (RuntimeError,), {})("太深了")
    current: BaseException = deepest
    for _ in range(compat._MAX_CHAIN_DEPTH + 2):
        outer = RuntimeError("层")
        outer.__cause__ = current
        current = outer
    assert compat.render_env_problem(current) is None


def test_real_kaleido_chrome_error_class_is_recognized():
    """用 kaleido 真正抛出的 ChromeNotFoundError 类校验类名匹配（不启动浏览器）。"""
    errors = pytest.importorskip("kaleido.errors")
    assert compat.render_env_problem(errors.ChromeNotFoundError("x")) == compat.CHROME_MISSING_HINT


# ---------------------------------------------------------------------------
# 缩略图 503 / 500 语义与 PNG 导出文案（端到端走 HTTP / 工具，渲染器用假实现）
# ---------------------------------------------------------------------------


@pytest.fixture()
def thumbnail_client(tmp_path, monkeypatch):
    """建一个只有 .plotly.json、没有缩略图缓存的会话，返回 (client, thumbnail_url)。

    自带最小夹具（隔离 registry），不依赖 test_api.py 的私有辅助函数。
    """
    from data_agent import api
    from data_agent.config import AgentSettings
    from data_agent.storage import LocalSessionStorage
    from data_agent.tools import build_tools

    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    runs_dir = tmp_path / "api_runs"
    runs_dir.mkdir()
    settings = AgentSettings(
        api_key="not-used",
        provider="deepseek",
        runs_dir=runs_dir,
        max_active_sessions=10,
        session_ttl_hours=24,
        rate_limit_per_minute=1000,
        max_concurrent_analyses=2,
    )
    registry = api.SessionRegistry(
        runs_dir,
        settings.max_active_sessions,
        settings.session_ttl_hours,
        storage=LocalSessionStorage(),
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "registry", registry)
    monkeypatch.setattr(api, "session_storage", LocalSessionStorage())
    monkeypatch.setattr(
        api, "analysis_slots", threading.BoundedSemaphore(settings.max_concurrent_analyses)
    )
    api.request_buckets.clear()

    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    )
    assert upload.status_code == 201
    session_id = upload.json()["id"]
    tools = {item.name: item for item in build_tools(registry.get(session_id).workspace)}
    tools["create_visualization"].invoke(
        {"chart_type": "bar", "x": "region", "y": "sales", "title": "sales_by_region"}
    )
    artifacts = client.get(f"/api/sessions/{session_id}").json()["artifacts"]
    chart = next(item for item in artifacts if item["kind"] == "visualization")
    return client, chart["thumbnail_url"]


def _patch_write_image(monkeypatch, exc):
    import plotly.graph_objects as go

    def raiser(self, *args, **kwargs):
        raise exc

    monkeypatch.setattr(go.Figure, "write_image", raiser)
    # 渲染器是假的，补丁安装本身与本组测试无关，直接置空避免触碰真实 kaleido。
    monkeypatch.setattr(compat, "install", lambda: False)


def test_thumbnail_503_when_chrome_missing(thumbnail_client, monkeypatch):
    client, url = thumbnail_client
    chrome_error = type("ChromeNotFoundError", (RuntimeError,), {})("Browser not found")
    wrapped = RuntimeError("Kaleido requires Google Chrome to be installed.")
    wrapped.__cause__ = chrome_error
    _patch_write_image(monkeypatch, wrapped)
    response = client.get(url)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "Chrome" in detail and "kaleido_get_chrome" in detail


def test_thumbnail_503_when_kaleido_not_installed(thumbnail_client, monkeypatch):
    client, url = thumbnail_client
    _patch_write_image(monkeypatch, ImportError("No module named 'kaleido'"))
    response = client.get(url)
    assert response.status_code == 503
    assert "kaleido" in response.json()["detail"]


def test_thumbnail_500_on_other_render_failure(thumbnail_client, monkeypatch):
    client, url = thumbnail_client
    _patch_write_image(monkeypatch, RuntimeError("ReferenceError: kaleido_scopes is not defined"))
    response = client.get(url)
    assert response.status_code == 500
    assert "缩略图生成失败" in response.json()["detail"]
    assert "kaleido_scopes" in response.json()["detail"]


def test_thumbnail_calls_install_before_rendering(thumbnail_client, monkeypatch):
    client, url = thumbnail_client
    order: list[str] = []
    import plotly.graph_objects as go

    def fake_install():
        order.append("install")
        return True

    def fake_write(self, path, *args, **kwargs):
        order.append("write")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG fake")

    monkeypatch.setattr(compat, "install", fake_install)
    monkeypatch.setattr(go.Figure, "write_image", fake_write)
    response = client.get(url)
    assert response.status_code == 200
    assert order == ["install", "write"]


def _png_workspace(tmp_path):
    import pandas as pd

    from data_agent.workspace import DataWorkspace

    source = tmp_path / "png.csv"
    pd.DataFrame({"region": ["East", "West"], "sales": [1.0, 2.0]}).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="png-compat")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def _export_png(workspace):
    from data_agent.tools import build_tools

    tools = {tool.name: tool for tool in build_tools(workspace)}
    return json.loads(
        tools["create_visualization"].invoke(
            {"chart_type": "bar", "x": "region", "y": "sales", "export_png": True}
        )
    )


def test_png_warning_distinguishes_environment_missing(tmp_path, monkeypatch):
    chrome_error = type("ChromeNotFoundError", (RuntimeError,), {})("Browser not found")
    wrapped = RuntimeError("Kaleido requires Google Chrome to be installed.")
    wrapped.__cause__ = chrome_error
    _patch_write_image(monkeypatch, wrapped)
    result = _export_png(_png_workspace(tmp_path))
    assert result["status"] == "ok"
    assert "png" not in result
    warning = result["png_warning"]
    assert "环境缺失" in warning and "kaleido_get_chrome" in warning
    assert "渲染失败" not in warning


def test_png_warning_for_render_failure_keeps_original_message(tmp_path, monkeypatch):
    _patch_write_image(monkeypatch, RuntimeError("ReferenceError: kaleido_scopes is not defined"))
    result = _export_png(_png_workspace(tmp_path))
    assert result["status"] == "ok"
    warning = result["png_warning"]
    assert "渲染失败" in warning and "kaleido_scopes" in warning
    assert "环境缺失" not in warning


def test_png_export_calls_install_before_write(tmp_path, monkeypatch):
    import plotly.graph_objects as go

    order: list[str] = []

    def fake_write(self, path, *args, **kwargs):
        order.append("write")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG fake")

    monkeypatch.setattr(compat, "install", lambda: order.append("install") or True)
    monkeypatch.setattr(go.Figure, "write_image", fake_write)
    result = _export_png(_png_workspace(tmp_path))
    assert order == ["install", "write"]
    assert "png" in result and "png_warning" not in result
