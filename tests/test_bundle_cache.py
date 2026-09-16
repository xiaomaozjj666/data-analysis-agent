"""CDN 图表 bundle 的共享缓存（消除每个会话重复下载 1MB 的 6 秒等待）。

实测背景：300 万行散点用 ECharts 渲染时，**新会话的首张图比后续图慢约 6 秒**，
Profiler 显示时间不在渲染里，而在 `ensure_echarts_bundle()`——它按会话下载
`echarts.min.js`。这份文件对所有会话完全相同，因此改为机器级共享缓存：
下载一次、各会话硬链接引用，并在服务启动时后台预热。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data_agent import workspace as workspace_module
from data_agent.workspace import (
    ECHARTS_BUNDLE_NAME,
    ECHARTS_CDN_URL,
    DataWorkspace,
    shared_bundle_path,
    warm_bundles,
)


def _workspace(root: Path, session: str) -> DataWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
    source = root / f"{session}.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(root / "runs", session_id=session)
    workspace.load(source, copy_into_workspace=True)
    return workspace


def _fake_download(payload: bytes = b"/*" + b"x" * 2048 + b"*/"):
    """替换 urlopen：记录被请求的 URL，返回一段够长的假 bundle。"""
    calls: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return payload

    def _urlopen(url, timeout=None):  # noqa: ANN001, ARG001
        calls.append(url)
        return _Response()

    return calls, _urlopen


def test_bundle_is_downloaded_once_for_many_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_BUNDLE_CACHE_DIR", str(tmp_path / "cache"))
    calls, urlopen = _fake_download()
    first = _workspace(tmp_path, "s1")
    second = _workspace(tmp_path, "s2")

    with patch("urllib.request.urlopen", urlopen):
        bundle_a = first.ensure_echarts_bundle()
        bundle_b = second.ensure_echarts_bundle()
        # 同一会话再次调用也不应触发新下载
        bundle_a2 = first.ensure_echarts_bundle()

    assert calls == [ECHARTS_CDN_URL], f"应只下载一次，实际 {calls}"
    assert bundle_a and bundle_b and bundle_a2
    assert bundle_a == bundle_a2
    # 两个会话都能拿到可用的 bundle（共享缓存 + 各自 artifacts 目录的引用）
    for path in (bundle_a, bundle_b):
        assert path.exists() and path.stat().st_size > workspace_module.MIN_BUNDLE_BYTES
    assert bundle_a != bundle_b
    assert bundle_a.read_bytes() == bundle_b.read_bytes()
    shared = shared_bundle_path(first.root, ECHARTS_BUNDLE_NAME)
    assert shared.exists(), "共享缓存里必须留下副本，供后续会话复用"


def test_shared_cache_survives_offline_sessions(tmp_path, monkeypatch):
    """共享缓存已存在时，即使网络不可用也能拿到 bundle（离线可用性）。"""
    monkeypatch.setenv("DATA_AGENT_BUNDLE_CACHE_DIR", str(tmp_path / "cache"))
    calls, urlopen = _fake_download()
    warm = _workspace(tmp_path, "warm")
    with patch("urllib.request.urlopen", urlopen):
        assert warm.ensure_echarts_bundle() is not None

    def _offline(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("network is unreachable")

    offline = _workspace(tmp_path, "offline")
    with patch("urllib.request.urlopen", _offline):
        bundle = offline.ensure_echarts_bundle()
    assert bundle is not None and bundle.exists(), "有共享缓存时不应因为断网而拿不到 bundle"


def test_offline_without_cache_returns_none(tmp_path, monkeypatch):
    """无缓存 + 断网：返回 None，调用方按原逻辑 fallback 到 CDN 直引。"""
    monkeypatch.setenv("DATA_AGENT_BUNDLE_CACHE_DIR", str(tmp_path / "empty-cache"))

    def _offline(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("network is unreachable")

    fresh = _workspace(tmp_path, "fresh")
    with patch("urllib.request.urlopen", _offline):
        assert fresh.ensure_echarts_bundle() is None


def test_corrupt_shared_cache_is_refetched(tmp_path, monkeypatch):
    """半截/损坏的缓存文件（小于阈值）必须重新下载，不能被当做好文件复用。"""
    monkeypatch.setenv("DATA_AGENT_BUNDLE_CACHE_DIR", str(tmp_path / "cache"))
    broken = shared_bundle_path(tmp_path, ECHARTS_BUNDLE_NAME)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"truncated")

    calls, urlopen = _fake_download()
    target = _workspace(tmp_path, "corrupt")
    with patch("urllib.request.urlopen", urlopen):
        bundle = target.ensure_echarts_bundle()
    assert calls == [ECHARTS_CDN_URL]
    assert bundle is not None and bundle.stat().st_size > workspace_module.MIN_BUNDLE_BYTES


def test_warm_bundles_reports_status_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_BUNDLE_CACHE_DIR", str(tmp_path / "cache"))
    calls, urlopen = _fake_download()
    with patch("urllib.request.urlopen", urlopen):
        ready = warm_bundles(tmp_path)
    assert set(ready) == {workspace_module.ECHARTS_BUNDLE_NAME, workspace_module.ECHARTS_GL_BUNDLE_NAME}
    assert all(ready.values()), ready

    def _offline(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("offline")

    monkeypatch.setenv("DATA_AGENT_BUNDLE_CACHE_DIR", str(tmp_path / "other-cache"))
    with patch("urllib.request.urlopen", _offline):
        offline_ready = warm_bundles(tmp_path)
    assert offline_ready == {name: False for name in offline_ready}
