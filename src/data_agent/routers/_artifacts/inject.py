"""历史产物脚本注入：图例锚定、modebar 本地化、scattergl 缩放修复。

从 ``artifacts.py`` 拆分而来。均为幂等注入——已含标记跳过，非目标文档跳过。
"""

from __future__ import annotations

import re

from data_agent.tools.builder import _PLOTLY_GL_REFRESH_SCRIPT

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


#: scattergl 缩放修复标记：新版生成器已在模板里带 gl-refresh-fix 脚本，
#: 历史产物预览/下载时由本函数注入（与 legend/modebar 修复同一模式）。
_GL_REFRESH_FIX_MARKER = "/*gl-refresh-fix*/"


def _inject_gl_refresh_fix(html_text: str) -> str:
    """为历史 Plotly 图表注入 scattergl 缩放修复脚本（暗色模式白带）。

    plotly.js 的 WebGL 画布（scattergl）缓存初始化时的背景色，暗色脚本
    用 relayout 换肤后，深度缩放时绘图区会退回初始浅色（"白带"）。
    修复脚本监听 plotly_relayout 事件，在暗色主题下对含 WebGL 轨迹的
    图表做一次 react 重渲染。幂等（含标记跳过）；只作用于 Plotly 文档。
    """
    if _GL_REFRESH_FIX_MARKER in html_text or "plotly-graph-div" not in html_text:
        return html_text
    head_pattern = re.compile(r"<head(?:\s[^>]*)?>", flags=re.IGNORECASE)
    if head_pattern.search(html_text):
        return head_pattern.sub(
            lambda match: f"{match.group(0)}{_PLOTLY_GL_REFRESH_SCRIPT}", html_text, count=1
        )
    return html_text


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
