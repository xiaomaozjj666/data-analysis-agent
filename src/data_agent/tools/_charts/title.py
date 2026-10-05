"""图表标题清理与文件名生成。

从 ``charts.py`` 拆分而来。LLM 常在 title 后追加技术标记（ANOVA/p 值/η² 等），
本模块负责把这些噪声清理掉，生成用户可读的标题与稳定的磁盘文件名。
"""

from __future__ import annotations

import re

from .constants import (
    _CHART_TITLE_MAX_CHARS,
    _CHART_TITLE_TECHNICAL_PATTERNS,
    _CHART_TYPE_LABELS_ZH,
)


def _humanize_chart_title(title: str | None, chart_type: str) -> str:
    """Strip technical noise from LLM-provided chart titles.

    LLM 经常把 ANOVA / p 值 / η² / _n_2 / 极端离群值主导 等内部标记塞进
    title。这些在 UI 上展示给用户会变成"看不懂的乱码"。本函数取 title
    第一段可读部分，去掉技术标记并截短，文件名 stem 单独用 chart_type
    中文短名 + 序号生成（见 ``_chart_filename_stem``），磁盘文件名不再
    嵌入 LLM 自由发挥的标题。
    """
    raw = (title or "").strip()
    if not raw:
        return _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "数据图表")
    cleaned = raw
    for pattern in _CHART_TITLE_TECHNICAL_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_ ").strip()
    # 如果清理后为空（title 全是技术标记），回退到类型中文短名，
    # 而不是返回原始乱码 title —— 那正是用户"看不懂"的根源。
    if not cleaned:
        return _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "数据图表")
    # 截短到 30 个字符（按 Unicode 字符计数）以避免前端溢出。
    if len(cleaned) > _CHART_TITLE_MAX_CHARS:
        cleaned = cleaned[:_CHART_TITLE_MAX_CHARS].rstrip("_ ,，;；-—")
    # 截短后可能再次为空（前 30 字符全是分隔符），二次回退到类型短名。
    if not cleaned:
        return _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "数据图表")
    return cleaned


def _chart_filename_stem(chart_type: str, index: int) -> str:
    """Build a short, stable filename stem like ``柱状图_1``.

    ``index`` 由 ``workspace.allocate_chart_index()`` 原子分配（全局递增，
    跨图表类型共用一套序号），保证同一轮并行生成的多张图不会重号。
    用自然数字 1/2/3 而非 01/02/03：前者读起来更像人话（"柱状图 1"），
    与 Observable / Plot 等业界惯例一致。
    """
    label = _CHART_TYPE_LABELS_ZH.get(chart_type, chart_type or "图表")
    return f"{label}_{index}"
