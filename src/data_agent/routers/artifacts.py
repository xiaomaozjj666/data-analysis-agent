"""产物预览、下载、缩略图与编辑路由。

- GET  /api/sessions/{id}/artifacts/{filename}/preview：图表在线预览（CSP 沙箱）。
- GET  /api/sessions/{id}/artifacts/{filename}：产物下载（HTML 内联 bundle 自包含）。
- GET  /api/sessions/{id}/artifacts/{filename}/thumbnail：Plotly 图表缩略图 PNG。
- PUT  /api/sessions/{id}/artifacts/{filename}/edit：基于 .plotly.json 重新生成 HTML。

辅助函数 ``_inline_plotly_bundle`` / ``_inline_echarts_bundle`` /
``_harden_preview_document`` 从原 ``api.py`` VERBATIM 迁移，由 ``api.py``
re-export 以兼容测试（如 ``test_echarts_engine.test_echarts_api_preview_inlines_bundle``
直接调用 ``api._inline_echarts_bundle``）。``_artifact_file`` 已迁移至
``data_agent.registry``，内部通过 ``api.registry`` 访问以兼容 monkeypatch。
"""

from __future__ import annotations

import json
import os
import re
import threading
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from data_agent.registry import ChartEditRequest, SessionRecord, _artifact_file
from data_agent.tools import _PLOTLY_DARK_MODE_SCRIPT
from data_agent.workspace import (
    ECHARTS_BUNDLE_NAME,
    ECHARTS_GL_BUNDLE_NAME,
    ECHARTS_GL_CDN_URL,
    PLOTLY_BUNDLE_NAME,
    _atomic_write_text,
)

router = APIRouter()


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
    return _PLOTLY_TAG_PATTERN.sub(lambda _match: f"<script>{plotly_js}</script>", html_text, count=1)

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


# 历史产物可能以非 UTF-8（如 Windows 默认 GBK / GB18030）写出，直接
# read_text(encoding="utf-8") 会抛 UnicodeDecodeError 或在早前版本里产生
# 中文乱码。这里按“utf-8-sig → utf-8 → gb18030”顺序探测，与 CSV 层
# _CSV_ENCODING_CANDIDATES 保持一致，确保任何历史 artifact 都能正确解码。
_PREVIEW_TEXT_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030")


def _read_utf8_robust(path: Path) -> str:
    """以容错方式读出 HTML 文本，优先 UTF-8，必要时回退 GBK/GB18030。"""
    raw = path.read_bytes()
    for enc in _PREVIEW_TEXT_CANDIDATES:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # 全都不行则按 latin-1 兜底（绝不抛错，最多是乱码字符而非崩溃）。
    return raw.decode("latin-1")


def _preview_etag(path: Path) -> str:
    """基于文件 mtime + size 生成 ETag，文件重写即失效。"""
    st = path.stat()
    return f'"{int(st.st_mtime)}:{st.st_size}"'


@router.get("/api/sessions/{session_id}/dashboard")
def export_dashboard(session_id: str) -> Response:
    """导出数据画像仪表盘：KPI 指标卡 + 全部图表 + 数据质量告警，
    单一自包含 HTML（离线可开、亮暗双主题）。实时基于当前工作区
    数据与已生成图表组装，不落盘为产物。"""
    from data_agent import api
    from data_agent.dashboard import build_dashboard_html

    record = api.registry.get(session_id)
    try:
        html_text = build_dashboard_html(record.workspace)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="尚未加载数据集，无法生成仪表盘。") from exc
    filename = quote("数据画像仪表盘.html")
    return Response(
        content=html_text,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}/preview")
def preview_artifact(session_id: str, filename: str, request: Request) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() != ".html":
        raise HTTPException(status_code=415, detail="该产物不支持在线预览。")
    etag = _preview_etag(path)
    # 条件请求：文件未变（ETag 一致）时直接返回 304，前端复用其 LRU 缓存，
    # 既避免重复下载内联后的大体积 HTML（Plotly 约 3.5MB），又保证文件一旦
    # 被重写（如重新生成图表）缓存立即失效、拿到最新内容，永不滞留旧版乱码。
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    html_text = _inline_plotly_bundle(record, _read_utf8_robust(path))
    # gl 扩展先内联：其标签更具体，先处理可避免主 bundle 正则的 CDN
    # 分支（https?://...echarts....js）误吞 echarts-gl 的 CDN 引用。
    html_text = _inline_echarts_gl_bundle(record, html_text)
    html_text = _inline_echarts_bundle(record, html_text)
    html_text = _harden_preview_document(html_text)
    return Response(
        content=html_text,
        media_type="text/html",
        headers={
            "Cache-Control": "private, no-store",
            "ETag": etag,
            "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
        },
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}")
def download_artifact(session_id: str, filename: str) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() == ".html":
        # Downloads must remain self-contained so they open offline.
        html_text = _inline_plotly_bundle(record, _read_utf8_robust(path))
        html_text = _inline_echarts_gl_bundle(record, html_text)
        html_text = _inline_echarts_bundle(record, html_text)
        html_text = _harden_preview_document(html_text)
        return Response(
            content=html_text,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"},
        )
    return FileResponse(path, filename=path.name)


@router.get("/api/sessions/{session_id}/artifacts/{filename}/thumbnail")
def get_chart_thumbnail(session_id: str, filename: str) -> FileResponse:
    """生成图表缩略图 PNG（best-effort，依赖 Kaleido）。

    优先返回已缓存的 ``{stem}_thumb.png``；缓存不存在时从 ``{stem}.plotly.json``
    重新渲染。Kaleido 未安装时返回 503，其他渲染异常返回 500。缩略图尺寸
    400×250，去掉 margin 节省空间。
    """
    from data_agent import api

    record = api.registry.get(session_id)
    workspace = record.workspace
    # 取基名防止路径遍历，并去掉可能的 .html 后缀得到原始 stem。
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    # 先查是否已有缓存的缩略图。命中的前提：缩略图不早于图表数据文件——
    # 图表被同名重新生成后（.plotly.json mtime 更新）旧缩略图必须失效
    # 重渲染，否则卡片上一直显示覆盖前的旧图。
    thumb_path = workspace.artifacts_dir / f"{stem}_thumb.png"
    json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
    if thumb_path.is_file() and (
        not json_path.is_file() or thumb_path.stat().st_mtime >= json_path.stat().st_mtime
    ):
        return FileResponse(thumb_path, media_type="image/png")
    # 从 .plotly.json 重新生成
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="图表数据文件不存在。")
    try:
        import plotly.graph_objects as go

        fig_dict = json.loads(json_path.read_text(encoding="utf-8"))
        fig = go.Figure(fig_dict)
        # 缩略图尺寸 400x250，去掉 margin 节省空间
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), showlegend=False)
        # 原子写：先写临时文件再 os.replace，防止并发缩略图请求交错写入损坏 PNG。
        # 临时文件名必须保留 .png 后缀：plotly/kaleido 从扩展名推断输出格式，
        # 用 .tmp 结尾会触发 "Invalid format 'tmp'" 导致缩略图渲染失败。
        tmp_thumb = thumb_path.with_name(thumb_path.name + ".tmp.png")
        fig.write_image(str(tmp_thumb), width=400, height=250, scale=1)
        os.replace(str(tmp_thumb), str(thumb_path))
        return FileResponse(thumb_path, media_type="image/png")
    except ImportError:
        raise HTTPException(status_code=503, detail="服务器未安装图片渲染依赖（kaleido）。") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"缩略图生成失败：{exc}") from exc


@router.put("/api/sessions/{session_id}/artifacts/{filename}/edit")
def edit_chart(session_id: str, filename: str, request: ChartEditRequest) -> dict[str, Any]:
    """编辑已有图表：修改标题或配色，基于 .plotly.json 重新生成 HTML。

    流程：
    1. 根据传入的 filename（可为 ``xxx.html`` 或 ``xxx``）定位同名
       ``xxx.plotly.json`` 数据文件。
    2. 读取 fig_dict，应用 title/color 修改。
    3. 用与 tools.py 一致的 HTML 模板（含暗色模式脚本、XSS 转义、
       原子写）重新生成 HTML，覆盖原文件。
    4. 同步更新 .plotly.json，保证后续编辑基于最新数据。

    安全：
    - filename 经 Path(filename).name 取基名，防止路径遍历。
    - HTML 生成复用 tools.py 的 ``</script>`` → ``<\\/script>`` 转义。
    """
    from html import escape

    import plotly.graph_objects as go

    from data_agent import api

    record = api.registry.get(session_id)
    # 编辑图表需要独占访问：与 worker 并发读写同一 .plotly.json/HTML
    # 会导致读到半写状态的文件。尝试获取 run_lock，失败时返回 409。
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话有分析正在运行，请稍后再编辑图表。")
    try:
        workspace = record.workspace
        # 取基名防止路径遍历，并去掉可能的 .html 后缀得到原始 stem。
        stem = Path(filename).name
        if stem.endswith(".html"):
            stem = stem[: -len(".html")]
        json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
        html_path = workspace.artifacts_dir / f"{stem}.html"
        if not json_path.is_file():
            raise HTTPException(status_code=404, detail="图表数据文件不存在，无法编辑。")
        try:
            fig_dict = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"图表数据读取失败：{exc}") from exc

        # 应用修改：title 写入 layout.title.text（保持 Plotly 标准结构）；
        # color 应用到所有 trace 的 marker.color，无 marker 的 trace 自动创建。
        if request.title is not None:
            fig_dict.setdefault("layout", {})["title"] = {"text": request.title}
        if request.color is not None:
            for trace in fig_dict.get("data", []):
                if "marker" in trace and isinstance(trace["marker"], dict):
                    trace["marker"]["color"] = request.color
                else:
                    trace["marker"] = {"color": request.color}

        # 重新生成 HTML：与 tools.py 保持一致的模板和转义逻辑，
        # 确保编辑后的图表预览/下载体验与原始生成一致。
        try:
            fig = go.Figure(fig_dict)
            shared_plotly = workspace.ensure_plotly_bundle()
            relative_script = (
                shared_plotly.relative_to(workspace.artifacts_dir).as_posix()
                if shared_plotly
                else None
            )
            display_title = (
                (fig_dict.get("layout", {}) or {}).get("title", {}).get("text", "")
                or stem
            )
            if relative_script:
                html_template = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>{title}</title><script src='{script}'></script>"
                    "<style>html,body{{width:100%;height:100%;margin:0;background:#fbfaf5;overflow:hidden}}"
                    ".plotly-graph-div{{width:100% !important;height:100% !important;min-height:560px}}</style>"
                    "</head><body>{div}{dark_script}</body></html>"
                )
                div = fig.to_html(
                    full_html=False,
                    include_plotlyjs=False,
                    default_width="100%",
                    default_height="100%",
                    config={
                        "responsive": True,
                        "displaylogo": False,
                        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                    },
                )
                # XSS 防护：与 tools.py 一致，转义 </script> 避免 Plotly
                # 序列化数据中的 </script> 提前关闭 script 块导致注入。
                div = div.replace("</script>", "<\\/script>")
                _atomic_write_text(
                    html_path,
                    html_template.format(
                        title=escape(display_title),
                        script=relative_script,
                        div=div,
                        dark_script=_PLOTLY_DARK_MODE_SCRIPT,
                    ),
                )
            else:
                # plotly bundle 不可用（极少见）时回退到内联 plotlyjs 的完整 HTML。
                fig.write_html(html_path, include_plotlyjs=True, full_html=True)
            # 同步更新 .plotly.json，保证后续编辑基于最新数据。
            # 使用原子写入（写 .tmp 再 replace），防止进程被杀时留下损坏的 JSON。
            import json as _json
            import os as _os
            tmp_path = json_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json.dump(fig.to_dict(), f, ensure_ascii=False, default=str)
            _os.replace(tmp_path, json_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"图表重新生成失败：{exc}") from exc
        return {"status": "ok", "message": "图表已更新。"}
    finally:
        record.run_lock.release()
