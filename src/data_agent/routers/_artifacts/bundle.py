"""Bundle 内联与进程级缓存。

从 ``artifacts.py`` 拆分而来。echarts/plotly 压缩包 1~3.6MB，每次预览/下载
都从磁盘重读代价高。各会话目录里的 bundle 是同一 CDN 版本的拷贝，用
（文件名, 字节数）做键即可跨会话复用。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from data_agent.registry import SessionRecord
from data_agent.workspace import (
    ECHARTS_BUNDLE_NAME,
    ECHARTS_GL_BUNDLE_NAME,
    ECHARTS_GL_CDN_URL,
    PLOTLY_BUNDLE_NAME,
)

_PLOTLY_TAG_PATTERN = re.compile(
    r"<script\s+src=['\"]plotly\.min\.js['\"]\s*></script>",
    flags=re.IGNORECASE,
)

# ECharts bundle 内联正则：匹配相对路径或 CDN URL 的 echarts.min.js 引用，
# 用于把预览/下载 HTML 内联成自包含文档。
_ECHARTS_TAG_PATTERN = re.compile(
    r"<script\s+src=['\"](?:echarts\.min\.js|https?://[^'\"]*echarts[^'\"]*\.js)['\"]\s*></script>",
    flags=re.IGNORECASE,
)

# echarts-gl 扩展 bundle（3D 散点等 gl 系列图表依赖）的 script 标签。
# 预览 CSP 的 script-src 只放行 'unsafe-inline' 与 jsdelivr，相对路径的
# <script src="echarts-gl.min.js"> 会被直接拦截导致 3D 图空白，必须内联。
_ECHARTS_GL_TAG_PATTERN = re.compile(
    r"<script\s+src=['\"](?:echarts-gl\.min\.js|https?://[^'\"]*echarts-gl[^'\"]*\.js)['\"]\s*></script>",
    flags=re.IGNORECASE,
)


_BUNDLE_TEXT_CACHE: dict[tuple[str, int], str] = {}
_BUNDLE_CACHE_MAX = 6
_BUNDLE_CACHE_LOCK = threading.Lock()


def _read_bundle_cached(path: Path) -> str | None:
    """读 bundle 文本带进程级缓存：echarts/plotly 压缩包 1~3.6MB，每次
    预览/下载都从磁盘重读代价高。各会话目录里的 bundle 是同一 CDN
    版本的拷贝，用（文件名, 字节数）做键即可跨会话复用；上限 6 条
    防止内存无限增长。读失败返 None，调用方保持原 HTML 不变。"""
    try:
        key = (path.name, path.stat().st_size)
    except OSError:
        return None
    with _BUNDLE_CACHE_LOCK:
        cached = _BUNDLE_TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    with _BUNDLE_CACHE_LOCK:
        if len(_BUNDLE_TEXT_CACHE) >= _BUNDLE_CACHE_MAX:
            _BUNDLE_TEXT_CACHE.pop(next(iter(_BUNDLE_TEXT_CACHE)), None)
        _BUNDLE_TEXT_CACHE[key] = text
    return text


def _inline_echarts_bundle(record: SessionRecord, html_text: str) -> str:
    """Replace the ECharts ``<script src>`` tag with the full source so previews
    and downloads stay self-contained when the bundle was downloaded locally.

    若 echarts.min.js 未下载到 artifacts_dir（离线场景），保持原 CDN 引用
    不变（在线场景可用），不报错。
    """
    bundle_path = record.workspace.artifacts_dir / ECHARTS_BUNDLE_NAME
    if not bundle_path.is_file():
        return html_text
    echarts_js = _read_bundle_cached(bundle_path)
    if echarts_js is None:
        return html_text
    return _ECHARTS_TAG_PATTERN.sub(
        lambda _match: f"<script>{echarts_js}</script>", html_text, count=1
    )


def _inline_echarts_gl_bundle(record: SessionRecord, html_text: str) -> str:
    """内联 echarts-gl 扩展 bundle（scatter3D 等 gl 系列图表依赖）。

    预览 iframe 的 CSP 不含 'self'，相对路径 script 会被拦截、scatter3D
    没有渲染器，3D 图直接空白。本地 bundle 存在时替换为内联源码；
    bundle 缺失（当时下载失败）时把相对引用改写为 jsdelivr CDN 直引
    （CSP 白名单已放行），保证在线场景仍可渲染。
    """
    if not _ECHARTS_GL_TAG_PATTERN.search(html_text):
        return html_text
    bundle_path = record.workspace.artifacts_dir / ECHARTS_GL_BUNDLE_NAME
    gl_js = _read_bundle_cached(bundle_path) if bundle_path.is_file() else None
    if gl_js is not None:
        return _ECHARTS_GL_TAG_PATTERN.sub(
            lambda _match: f"<script>{gl_js}</script>", html_text, count=1
        )
    return _ECHARTS_GL_TAG_PATTERN.sub(
        lambda _match: f'<script src="{ECHARTS_GL_CDN_URL}"></script>', html_text, count=1
    )


def _inline_plotly_bundle(record: SessionRecord, html_text: str) -> str:
    """Replace the shared ``<script src='plotly.min.js'>`` tag with the full
    Plotly.js source so previews and downloads stay self-contained."""
    bundle_path = record.workspace.artifacts_dir / PLOTLY_BUNDLE_NAME
    if not bundle_path.is_file():
        return html_text
    plotly_js = _read_bundle_cached(bundle_path)
    if plotly_js is None:
        return html_text
    # Use a lambda replacement so backslashes in plotly_js (e.g. "\s" inside
    # the minified source) are treated literally instead of as regex escapes.
    return _PLOTLY_TAG_PATTERN.sub(
        lambda _match: f"<script>{plotly_js}</script>", html_text, count=1
    )
