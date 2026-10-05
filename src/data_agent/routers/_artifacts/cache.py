"""预览文档加固与准备结果缓存。

从 ``artifacts.py`` 拆分而来。CSP 沙箱策略、预览文档加固、ETag 生成，
以及预览准备结果的 LRU 缓存（避免热路径上重复读文件 + 注入 + 内联 bundle）。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path

_PREVIEW_CSP = (
    "default-src 'none'; "
    # ECharts 离线 fallback 时需引用 jsdelivr CDN，加入白名单。
    # 'unsafe-eval'：echarts-gl（claygl 内核）用 new Function 求值渲染目标
    # 尺寸表达式，缺它时 3D 图初始化直接报 Invalid expression 空白。
    "script-src 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-src 'none'; "
    "manifest-src 'none'"
)


def _harden_preview_document(html_text: str) -> str:
    """Confine generated chart HTML to a script-only, offline document."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{_PREVIEW_CSP}">'
    head_pattern = re.compile(r"<head(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if head_pattern.search(html_text):
        return head_pattern.sub(lambda match: f"{match.group(0)}{meta}", html_text, count=1)
    # 如果原文已是完整文档但缺少 <head>（罕见），直接在 <html> 后注入 <head>。
    html_tag_pattern = re.compile(r"<html(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if html_tag_pattern.search(html_text):
        return html_tag_pattern.sub(
            lambda match: f"{match.group(0)}<head>{meta}</head>", html_text, count=1
        )
    # 原文是 body 片段，包一层完整文档。先检测是否已带 doctype，避免重复声明
    # 导致浏览器进入怪异模式。
    if re.match(r"\s*<!doctype", html_text, flags=re.IGNORECASE):
        return html_text
    return f"<!doctype html><html><head>{meta}</head><body>{html_text}</body></html>"


def _preview_etag(path: Path) -> str:
    """基于文件 mtime + size 生成 ETag，文件重写即失效。"""
    st = path.stat()
    return f'"{int(st.st_mtime)}:{st.st_size}"'


#: 预览文档准备结果缓存：preview 每次命中 200 都要重读产物文件、跑三段
#: 注入修复链、内联 ~4.8MB 图表库（几十 MB 字符串拼接），热路径上纯属
#: 重复劳动。按（路径, mtime_ns, size）缓存准备完成的文档，文件重写即
#: 自然失配。只缓存中小文档（大数据图表的嵌入降采样后本就 ≤ 数 MB），
#: 防止内存膨胀；ETag 仍基于磁盘文件，缓存不影响新鲜度语义。
_PREVIEW_PREPARED_CACHE_MAX = 4
_PREVIEW_PREPARED_CACHE_SKIP_BYTES = 24 * 1024 * 1024
_prepared_previews: OrderedDict[tuple[str, int, int], str] = OrderedDict()


def _get_prepared_preview(path: Path) -> str | None:
    """命中返回准备好的文档文本，未命中返回 None。"""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _prepared_previews.get(key)
    if cached is not None:
        _prepared_previews.move_to_end(key)
    return cached


def _put_prepared_preview(path: Path, html_text: str) -> None:
    st = path.stat()
    if st.st_size > _PREVIEW_PREPARED_CACHE_SKIP_BYTES:
        return
    key = (str(path), st.st_mtime_ns, st.st_size)
    _prepared_previews[key] = html_text
    _prepared_previews.move_to_end(key)
    while len(_prepared_previews) > _PREVIEW_PREPARED_CACHE_MAX:
        _prepared_previews.popitem(last=False)
