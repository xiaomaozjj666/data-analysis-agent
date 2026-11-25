"""ECharts 输出层：option 序列化、JSON 兜底与 standalone HTML 组装。"""

from __future__ import annotations

import json
import uuid
from html import escape
from typing import Any

from data_agent.echarts_constants import (
    _ECHARTS_BG_COLOR,
    _ECHARTS_DARK_MODE_SCRIPT,
    _ECHARTS_FONT_FAMILY,
    _ECHARTS_HTML_TEMPLATE,
    _ECHARTS_TEXT_COLOR,
    _JsFunction,
)


def _serialize_option(option: dict[str, Any]) -> str:
    """把 option 序列化为可嵌入 <script> 的 JS 对象字面量字符串。

    支持 _JsFunction 标记，并自动识别 key 为 ``formatter`` 的 JS 函数字符串
    （以 ``function(`` 开头、``}`` 结尾），在 JSON 序列化后把它们从带引号的
    JSON 字符串还原为无引号的 JS 函数字面量，避免 ECharts 把源码当文本显示。
    """
    token_map: dict[str, str] = {}

    def _looks_like_js_function(code: str) -> bool:
        code = code.strip()
        return code.startswith("function(") and code.endswith("}")

    def walk(obj: Any, key: str | None = None) -> Any:
        if isinstance(obj, _JsFunction):
            token_map[obj.token] = obj.code
            return obj.token
        if isinstance(obj, str) and key == "formatter" and _looks_like_js_function(obj):
            token = f"__JS_FN_{uuid.uuid4().hex}__"
            token_map[token] = obj
            return token
        if isinstance(obj, dict):
            return {k: walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v, key) for v in obj]
        return obj

    option_with_tokens = walk(option)
    option_json = json.dumps(option_with_tokens, ensure_ascii=False, default=str)
    option_json = option_json.replace("</script>", "<\\/script>")
    for token, code in token_map.items():
        option_json = option_json.replace(f'"{token}"', code)
    return option_json


def _json_default(obj: Any) -> Any:
    """json.dumps 的 default：把 _JsFunction 还原为源码字符串，保持 JSON 合法。"""
    if isinstance(obj, _JsFunction):
        return obj.code
    return str(obj)


def _build_echarts_html(
    *,
    title: str,
    option: dict[str, Any],
    script_src: str,
    interpretation: str,
    extra_script_src: str | None = None,
    data_table: str = "",
) -> str:
    """组装 standalone HTML：ECharts option + 解读块 + 可访问数据表 + 视觉规范。

    ``data_table`` 是 ``render_data_table_html`` 产出的折叠表格，默认空串
    （编辑/缩略图等不需要数据表的调用方保持原行为）。
    """
    option_json = _serialize_option(option)

    if interpretation:
        interp_block = (
            '<div class="interpretation">'
            '<div class="interpretation-title">数据解读</div>'
            f"{escape(interpretation)}"
            "</div>"
        )
    else:
        interp_block = ""

    return _ECHARTS_HTML_TEMPLATE.format(
        title=escape(title),
        script=script_src,
        extra_script=(f'\n<script src="{extra_script_src}"></script>' if extra_script_src else ""),
        option=option_json,
        # 数据表与解读块同属 .layout 的底部区域：默认折叠，不改变图表高度行为。
        interpretation_block=interp_block + data_table,
        bg=_ECHARTS_BG_COLOR,
        font=_ECHARTS_FONT_FAMILY,
        text=_ECHARTS_TEXT_COLOR,
        dark_script=_ECHARTS_DARK_MODE_SCRIPT,
    )
