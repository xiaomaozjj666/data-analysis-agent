"""产物辅助函数包：bundle 内联、HTML 修复、脚本注入、预览缓存。

从原 ``artifacts.py`` 拆分为多个子模块，本 ``__init__`` 重新导出全部公共符号，
保持 ``from data_agent.routers.artifacts import X`` 导入路径不变。
"""

from __future__ import annotations

from .bundle import (
    _BUNDLE_CACHE_MAX,
    _BUNDLE_TEXT_CACHE,
    _ECHARTS_GL_TAG_PATTERN,
    _ECHARTS_TAG_PATTERN,
    _PLOTLY_TAG_PATTERN,
    _inline_echarts_bundle,
    _inline_echarts_gl_bundle,
    _inline_plotly_bundle,
    _read_bundle_cached,
)
from .cache import (
    _PREVIEW_CSP,
    _PREVIEW_PREPARED_CACHE_MAX,
    _PREVIEW_PREPARED_CACHE_SKIP_BYTES,
    _get_prepared_preview,
    _harden_preview_document,
    _prepared_previews,
    _preview_etag,
    _put_prepared_preview,
)
from .inject import (
    _GL_REFRESH_FIX_MARKER,
    _LEGEND_ANCHOR_FIX_MARKER,
    _LEGEND_ANCHOR_FIX_SCRIPT,
    _MODEBAR_I18N_MARKER,
    _MODEBAR_I18N_SCRIPT,
    _inject_gl_refresh_fix,
    _inject_legend_anchor_fix,
    _inject_modebar_i18n,
)
from .repair import (
    _LEGACY_PLOTLY_THEME_KEYS,
    _PREVIEW_TEXT_CANDIDATES,
    _read_utf8_robust,
    _repair_legacy_plotly_theme_keys,
    _repair_unterminated_plotly_script,
)

__all__ = [
    "_BUNDLE_CACHE_MAX",
    "_BUNDLE_TEXT_CACHE",
    "_ECHARTS_GL_TAG_PATTERN",
    "_ECHARTS_TAG_PATTERN",
    "_GL_REFRESH_FIX_MARKER",
    "_LEGACY_PLOTLY_THEME_KEYS",
    "_LEGEND_ANCHOR_FIX_MARKER",
    "_LEGEND_ANCHOR_FIX_SCRIPT",
    "_MODEBAR_I18N_MARKER",
    "_MODEBAR_I18N_SCRIPT",
    "_PLOTLY_TAG_PATTERN",
    "_PREVIEW_CSP",
    "_PREVIEW_PREPARED_CACHE_MAX",
    "_PREVIEW_PREPARED_CACHE_SKIP_BYTES",
    "_PREVIEW_TEXT_CANDIDATES",
    "_get_prepared_preview",
    "_harden_preview_document",
    "_inline_echarts_bundle",
    "_inline_echarts_gl_bundle",
    "_inline_plotly_bundle",
    "_inject_gl_refresh_fix",
    "_inject_legend_anchor_fix",
    "_inject_modebar_i18n",
    "_prepared_previews",
    "_preview_etag",
    "_put_prepared_preview",
    "_read_bundle_cached",
    "_read_utf8_robust",
    "_repair_legacy_plotly_theme_keys",
    "_repair_unterminated_plotly_script",
]
