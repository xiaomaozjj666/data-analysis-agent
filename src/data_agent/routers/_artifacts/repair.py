"""HTML 修复与容错读取。

从 ``artifacts.py`` 拆分而来。修复旧版生成器（<2026-08）的转义 bug、
暗色脚本 relayout 键前缀问题，以及历史产物的编码兼容读取。
"""

from __future__ import annotations

from pathlib import Path

# 历史产物可能以非 UTF-8（如 Windows 默认 GBK / GB18030）写出，直接
# read_text(encoding="utf-8") 会抛 UnicodeDecodeError 或在早前版本里产生
# 中文乱码。这里按"utf-8-sig → utf-8 → gb18030"顺序探测，与 CSV 层
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
    after = html_text[idx + len(marker) :]
    next_close = after.find("</script>")
    if next_close == -1:
        return html_text
    if after.find("<script", 0, next_close) == -1:
        return html_text
    return html_text[:idx] + "</script>" + html_text[idx + len(marker) :]


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
