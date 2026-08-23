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
import math
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
from data_agent.tools.builder import _render_plotly_html
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


def _repair_unterminated_plotly_script(html_text: str) -> str:
    """修复旧版生成器（<2026-08）的转义 bug：``</script>`` → ``<\\/script>``
    的 XSS 转义把 to_html 自身脚本块的闭合标签也一并转义，导致该 script
    元素无法闭合、与后续注入的暗色脚本合并成同一 script 块（内含字面
    ``<script>``），产生 ``Unexpected token '<'`` 语法错误，Plotly 图表
    预览空白、下载文件离线打开也空白。

    判定（结构感知）：坏文件中，最后一个 ``<\\/script>``（被误转义的
    闭合标签）与下一个原始 ``</script>`` 之间必然夹着暗色脚本的
    ``<script`` 开标签；正常文件中，最后一个 ``<\\/script>``（数据里的
    转义）到下一个原始 ``</script>``（to_html 的真实闭合）之间只有
    newPlot 调用、没有任何 ``<script``。据此精确区分，绝不误伤数据
    中合法的转义，也不重新引入 XSS 风险。新版生成器产出的文件结构
    正确，原样返回。
    """
    marker = "<\\/script>"
    idx = html_text.rfind(marker)
    if idx == -1:
        return html_text
    after = html_text[idx + len(marker):]
    next_close = after.find("</script>")
    if next_close == -1:
        return html_text
    if after.find("<script", 0, next_close) == -1:
        return html_text
    return html_text[:idx] + "</script>" + html_text[idx + len(marker):]


#: 旧版暗色脚本（<2026-08）的 relayout 键带 'layout.' 前缀，在 Plotly v3
#: 会被静默忽略（图表画布/文字/网格保持浅色，只有页面背景变暗）。
#: 这里按（旧键, 新键）逐一替换为合法的根路径键，幂等：新版文件不含旧键。
_LEGACY_PLOTLY_THEME_KEYS: tuple[tuple[str, str], ...] = (
    ("'layout.paper_bgcolor'", "'paper_bgcolor'"),
    ("'layout.plot_bgcolor'", "'plot_bgcolor'"),
    ("'layout.font.color'", "'font.color'"),
    ("'layout.xaxis.gridcolor'", "'xaxis.gridcolor'"),
    ("'layout.yaxis.gridcolor'", "'yaxis.gridcolor'"),
    ("'layout.xaxis.zerolinecolor'", "'xaxis.zerolinecolor'"),
    ("'layout.yaxis.zerolinecolor'", "'yaxis.zerolinecolor'"),
)


def _repair_legacy_plotly_theme_keys(html_text: str) -> str:
    """修复旧版暗色脚本的 relayout 键（``'layout.paper_bgcolor'`` 等带
    ``layout.`` 前缀的写法在 Plotly v3 被静默忽略），替换为合法的根路径键
    （``'paper_bgcolor'`` / ``'font.color'`` 等），让历史图表在深色主题下
    真正变暗。纯字符串替换、幂等，不涉及结构解析。"""
    for old, new in _LEGACY_PLOTLY_THEME_KEYS:
        if old in html_text:
            html_text = html_text.replace(old, new)
    return html_text


#: 图例修正脚本标记：新图生成时图例已是"绘图区下方居中横向排布"
#: （orientation=h，不遮挡数据、窄容器不溢出）；历史图仍是默认右侧竖排
#: （x=1.02）或绘图区内 overlay（会遮挡数据）。此脚本轮询等待图表就绪，
#: 若图例不是横向则 relayout 为横向底部并加大底部 margin；幂等
#: （含标记不再注入，条件不满足不动作）。
_LEGEND_ANCHOR_FIX_MARKER = "/*legend-anchor-fix*/"
_LEGEND_ANCHOR_FIX_SCRIPT = (
    "<script>/*legend-anchor-fix*/\n"
    "(function() {\n"
    "  var tries = 0;\n"
    "  var timer = setInterval(function() {\n"
    "    tries++;\n"
    "    var gd = document.querySelector('.plotly-graph-div');\n"
    "    if (gd && window.Plotly && gd._fullLayout && gd._fullLayout.legend) {\n"
    "      clearInterval(timer);\n"
    "      var lg = gd._fullLayout.legend;\n"
    "      if (lg.orientation !== 'h') {\n"
    "        try { Plotly.relayout(gd, {\n"
    "          'legend.orientation': 'h',\n"
    "          'legend.x': 0.5, 'legend.xanchor': 'center',\n"
    "          'legend.y': -0.15, 'legend.yanchor': 'top',\n"
    "          'margin.b': 120\n"
    "        }); } catch (_) {}\n"
    "      }\n"
    "    } else if (tries > 40) { clearInterval(timer); }\n"
    "  }, 200);\n"
    "})();\n"
    "</script>"
)


def _inject_legend_anchor_fix(html_text: str) -> str:
    """为历史 Plotly 图表注入图例锚定修正脚本（窄容器下图例不再溢出被裁）。

    幂等：已含标记的文档跳过；非 Plotly 文档（无 plotly-graph-div 容器）
    跳过；注入位置与 CSP meta 一致（<head> 之后），'unsafe-inline' 放行。
    注意：HTML 属性里是 ``class="plotly-graph-div"``（无前导点），
    CSS 选择器写法 ``.plotly-graph-div`` 在这里匹配不到。
    """
    if _LEGEND_ANCHOR_FIX_MARKER in html_text or "plotly-graph-div" not in html_text:
        return html_text
    head_pattern = re.compile(r"<head(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if head_pattern.search(html_text):
        return head_pattern.sub(
            lambda match: f"{match.group(0)}{_LEGEND_ANCHOR_FIX_SCRIPT}", html_text, count=1
        )
    return html_text


#: Plotly 模式栏（modebar）按钮提示本地化脚本：plotly v3 的 data-title/
#: aria-label 是英文（如 "Download plot as a PNG"），locale 资源未覆盖
#: zh-CN。此脚本把按钮提示替换为中文，MutationObserver 监听 modebar
#: 渲染，5 秒后断开（newPlot 后立即渲染，足够）；幂等（含标记跳过）。
_MODEBAR_I18N_MARKER = "/*modebar-i18n*/"
_MODEBAR_I18N_SCRIPT = (
    "<script>/*modebar-i18n*/\n"
    "(function() {\n"
    "  var map = {\n"
    "    'Download plot as a PNG': '下载为 PNG 图片',\n"
    "    'Zoom': '缩放',\n"
    "    'Pan': '平移',\n"
    "    'Zoom in': '放大',\n"
    "    'Zoom out': '缩小',\n"
    "    'Autoscale': '自动缩放',\n"
    "    'Reset axes': '重置坐标轴',\n"
    "    'Box Select': '框选',\n"
    "    'Lasso Select': '套索选择',\n"
    "    'Toggle Spike Lines': '切换辅助线',\n"
    "    'Show closest data on hover': '悬停显示最近数据',\n"
    "    'Compare data on hover': '悬停对比数据',\n"
    "    'Edit chart': '编辑图表',\n"
    "    'Toggle Hover Info': '切换悬停信息'\n"
    "  };\n"
    "  function localize() {\n"
    "    var btns = document.querySelectorAll('.modebar-btn');\n"
    "    for (var i = 0; i < btns.length; i++) {\n"
    "      var t = btns[i].getAttribute('data-title');\n"
    "      if (t && map[t]) {\n"
    "        btns[i].setAttribute('data-title', map[t]);\n"
    "        btns[i].setAttribute('aria-label', map[t]);\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "  function boot() {\n"
    "    localize();\n"
    "    if (document.body) {\n"
    "      var obs = new MutationObserver(localize);\n"
    "      obs.observe(document.body, { childList: true, subtree: true });\n"
    "      setTimeout(function() { obs.disconnect(); localize(); }, 5000);\n"
    "    }\n"
    "  }\n"
    "  // 脚本注入在 <head> 中，body 可能尚未解析，等 DOM 就绪再启动\n"
    "  if (document.readyState !== 'loading') { boot(); }\n"
    "  else { document.addEventListener('DOMContentLoaded', boot); }\n"
    "})();\n"
    "</script>"
)


def _inject_modebar_i18n(html_text: str) -> str:
    """为 Plotly 图表注入 modebar 按钮提示中文本地化 + 按钮间距统一。

    幂等（含标记跳过）；只作用于 Plotly 文档；注入到 <head> 之后。
    plotly 的 modebar 按钮按功能分组，组内/组间间距不一致（视觉上
    图标疏密不均），统一按钮宽度与组间距让工具栏等距排布。
    """
    if _MODEBAR_I18N_MARKER in html_text or "plotly-graph-div" not in html_text:
        return html_text
    style = (
        "<style>/*modebar-i18n*/\n"
        ".modebar{display:flex;align-items:center}\n"
        ".modebar-group{display:flex;align-items:center;margin:0 !important;"
        "padding-left:0 !important}\n"
        ".modebar-btn{width:26px;height:26px;padding:0 !important;"
        "margin:0 3px !important;"
        "display:inline-flex;align-items:center;justify-content:center}\n"
        ".modebar-group:first-child .modebar-btn:first-child{margin-left:0 !important}\n"
        ".modebar-btn svg{width:16px;height:16px}\n"
        "</style>"
    )
    head_pattern = re.compile(r"<head(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if head_pattern.search(html_text):
        return head_pattern.sub(
            lambda match: f"{match.group(0)}{style}{_MODEBAR_I18N_SCRIPT}",
            html_text,
            count=1,
        )
    return html_text


#: 迷你图数据上限：产物卡片的缩略图只需要"看得出分布形状"，全量点云
#: 会让 JSON 体积和渲染成本随数据行数线性暴涨（30 万行散点的
#: .echarts.json 实测约 11.7MB、Plotly 约 3.3MB）。等距抽样到该上限，
#: 形状保留且卡片渲染恒定 O(1)。
_THUMB_MAX_POINTS = 2500


def _sample_echarts_option_for_thumb(option: dict[str, Any], max_points: int = _THUMB_MAX_POINTS) -> None:
    """就地抽样 ECharts option 的散点系列，供迷你图渲染。

    只处理 ``type=="scatter"`` 系列：折线/柱状/直方图数据量由类别或
    bin 数决定（天然很小），热力图的格子抽样会产生缺格破图，散点矩阵
    与 3D 散点在生成阶段已采样。等距步进抽样（data[::k]），保留
    整体分布形状与两端特征。
    """
    if not isinstance(option, dict):
        return
    series = option.get("series")
    if not isinstance(series, list):
        return
    for item in series:
        if not isinstance(item, dict) or item.get("type") != "scatter":
            continue
        data = item.get("data")
        if not isinstance(data, list) or len(data) <= max_points:
            continue
        step = math.ceil(len(data) / max_points)
        item["data"] = data[::step]


#: 按点一维数组的图型（轨迹数据是"每行一个点"的长数组）；矩阵型
#: （heatmap 的 z 是二维网格）与类别型（bar 的 x/y 是类别维度）排除。
_PLOTLY_SAMPLE_TRACE_TYPES = {
    "scatter", "scattergl", "scatter3d", "scatterternary", "scatterpolar",
    "scatterpolargl", "line", "box", "violin",
}


def _decode_plotly_typed_arrays(value: Any) -> Any:
    """递归解码 Plotly typed-array 序列化，返回普通 Python list。

    plotly.py 的 ``fig.to_json()`` 默认把 numpy 数组压缩成
    ``{"dtype": "f4", "bdata": "<base64>"}``（30 万行散点的 x/y 都是
    这种形式，直接当 dict 处理无法统计/抽样）。解码后用标准 JSON
    数组返回，等距抽样与 json.dumps 都按普通列表工作；
    plotly.js 对普通数组同样支持。
    """
    if isinstance(value, dict):
        dtype = value.get("dtype")
        bdata = value.get("bdata")
        if (
            isinstance(dtype, str)
            and isinstance(bdata, str)
            and set(value) == {"dtype", "bdata"}
            and dtype != "object"
        ):
            try:
                import base64

                import numpy as np

                return np.frombuffer(base64.b64decode(bdata), dtype=np.dtype(dtype)).tolist()
            except Exception:
                return value
        return {key: _decode_plotly_typed_arrays(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_plotly_typed_arrays(item) for item in value]
    return value


def _sample_plotly_figure_for_thumb(figure: dict[str, Any], max_points: int = _THUMB_MAX_POINTS) -> None:
    """就地抽样 Plotly figure 的按点数据数组，供迷你图渲染。

    每个 trace 先取所有数组字段的最大长度 n（x/y/z/text/customdata/
    marker.color 等），n 超过上限时统一按同一等距步长抽样——同一下标
    规则保证 x、y、颜色、悬浮文本一致，不会错位。只处理按点图型；
    heatmap（z 矩阵）/pie/sunburst 等非按点图型不动。
    """
    if not isinstance(figure, dict):
        return
    data = figure.get("data")
    if not isinstance(data, list):
        return
    for trace in data:
        if not isinstance(trace, dict) or trace.get("type") not in _PLOTLY_SAMPLE_TRACE_TYPES:
            continue
        n = 0
        for value in trace.values():
            if isinstance(value, list) and len(value) > n:
                n = len(value)
        if n <= max_points:
            continue
        step = math.ceil(n / max_points)

        def _sample(value: Any, _n: int = n, _step: int = step) -> Any:
            # 递归抽样：散点的 marker.color/marker.size 等嵌套按点数组
            # 与 x/y 同一步长，保证颜色/大小/悬浮文本不错位。
            # 默认参数绑定循环变量（ruff B023）。
            if isinstance(value, list):
                return value[::_step] if len(value) == _n else value
            if isinstance(value, dict):
                return {key: _sample(item, _n, _step) for key, item in value.items()}
            return value

        for key, value in list(trace.items()):
            trace[key] = _sample(value)


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
    # 旧版生成器（<2026-08）的三个问题统一修复后再内联 bundle：
    # 1) to_html 脚本块闭合标签被误转义导致预览空白；
    # 2) 暗色脚本 relayout 键带 'layout.' 前缀被 Plotly v3 静默忽略；
    # 3) 图例默认右侧竖排，窄容器下溢出 SVG 被裁（历史图注入锚定修正）。
    # 另注入 modebar 按钮提示中文本地化（plotly 自带 locale 不含 zh-CN）。
    html_text = _repair_legacy_plotly_theme_keys(
        _repair_unterminated_plotly_script(_read_utf8_robust(path))
    )
    html_text = _inject_legend_anchor_fix(html_text)
    html_text = _inject_modebar_i18n(html_text)
    html_text = _inline_plotly_bundle(record, html_text)
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
        html_text = _repair_legacy_plotly_theme_keys(
            _repair_unterminated_plotly_script(_read_utf8_robust(path))
        )
        html_text = _inject_legend_anchor_fix(html_text)
        html_text = _inject_modebar_i18n(html_text)
        html_text = _inline_plotly_bundle(record, html_text)
        html_text = _inline_echarts_gl_bundle(record, html_text)
        html_text = _inline_echarts_bundle(record, html_text)
        html_text = _harden_preview_document(html_text)
        return Response(
            content=html_text,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"},
        )
    return FileResponse(path, filename=path.name)


@router.get("/api/sessions/{session_id}/artifacts/{filename}/echarts-json")
def get_echarts_option(session_id: str, filename: str) -> Response:
    """返回 ECharts 图表的 option JSON，供前端产物卡片渲染迷你图。

    ECharts 没有像 Plotly 那样的服务端 PNG 缩略图通道（kaleido 仅支持
    Plotly），前端直接读取 option 并用 echarts 原地渲染迷你图，让产物
    卡片无需点击即可预览。安全：文件名经 _artifact_file 基名校验；
    option 中存档的 JS 函数以字符串形式返回，前端渲染迷你图时剥离
    函数字段（不执行任意代码）。大数据兜底：散点系列按等距抽样到
    ``_THUMB_MAX_POINTS``，避免几十万行时迷你图传输/渲染卡顿。
    """
    record, _path = _artifact_file(session_id, filename)
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    json_path = record.workspace.artifacts_dir / f"{stem}.echarts.json"
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="该图表没有 ECharts 数据文件。")
    try:
        option = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"ECharts 数据读取失败：{exc}") from exc
    # 大数据兜底：30 万行散点的 option 可达十几 MB，卡片迷你图无需
    # 全量点云——按等距抽样到固定上限，传输与渲染恒定开销。
    _sample_echarts_option_for_thumb(option)
    return Response(
        content=json.dumps(option, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}/plotly-json")
def get_plotly_option(session_id: str, filename: str) -> Response:
    """返回 Plotly 图表的 figure JSON，供前端产物卡片渲染交互迷你图。

    镜像 echarts-json 端点：Plotly 卡片的缩略图是服务端渲染的静态 PNG
    （kaleido），悬停无任何反应；前端拿到 figure JSON 后用 plotly.js
    原地渲染迷你图，即可像 ECharts 卡片一样悬停查看数据。安全：文件名
    经 _artifact_file 基名校验；Plotly 的 JSON 是纯数据（无函数字段），
    前端不会执行任何代码。大数据兜底：按点图型（散点/箱线等）等距抽样
    到 ``_THUMB_MAX_POINTS``。
    """
    record, _path = _artifact_file(session_id, filename)
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    json_path = record.workspace.artifacts_dir / f"{stem}.plotly.json"
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="该图表没有 Plotly 数据文件。")
    try:
        figure = json.loads(_read_utf8_robust(json_path))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Plotly 数据读取失败：{exc}") from exc
    # plotly.py 把 numpy 数组写成了 typed-array（{"dtype","bdata"}），
    # 先解码成普通列表；再按大数据兜底规则等距抽样到 _THUMB_MAX_POINTS
    # （散点/箱线等按点图型），迷你图传输与渲染恒定开销。
    figure = _decode_plotly_typed_arrays(figure)
    _sample_plotly_figure_for_thumb(figure)
    return Response(
        content=json.dumps(figure, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


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

        fig_dict = json.loads(_read_utf8_robust(json_path))
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
            fig_dict = json.loads(_read_utf8_robust(json_path))
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
                # 必须保留最后一个 </script>（to_html 自身脚本块的闭合
                # 标签），否则 script 元素无法闭合，会与后续注入的暗色
                # 脚本合并成无效 JS（"Unexpected token '<'"），预览空白。
                close_idx = div.rfind("</script>")
                if close_idx != -1:
                    div = div[:close_idx].replace("</script>", "<\\/script>") + div[close_idx:]
                _atomic_write_text(
                    html_path,
                    _render_plotly_html(
                        title=escape(display_title),
                        script_src=relative_script,
                        div=div,
                        dark_script=_PLOTLY_DARK_MODE_SCRIPT,
                        full_page=False,
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
        # 标题变更后同步产物描述并持久化：产物卡片、预览模态头部与会话
        # 归档读的都是 description，只改图表 HTML 会让 UI 停留在旧标题。
        if request.title is not None:
            workspace.update_artifact_description(html_path, display_title)
            try:
                api.registry._persist_locked(session_id, record)
            except Exception:
                # 持久化失败不影响本次编辑结果，仅记录
                import logging
                logging.getLogger(__name__).exception("Failed to persist manifest after chart edit")
        return {"status": "ok", "message": "图表已更新。"}
    finally:
        record.run_lock.release()
