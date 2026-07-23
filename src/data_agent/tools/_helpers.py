"""工具集内部通用辅助函数：列名可读化与数值紧凑格式化。

本模块仅包含无业务依赖的纯函数，供 charts / builder 等模块复用。
"""

from __future__ import annotations


def _human_column_label(column: str | None) -> str:
    """将列名转为可读标签：下划线转空格，不硬编码业务域翻译。"""
    if not column:
        return ""
    return str(column).replace("_", " ").strip()


def _compact_number(value: float) -> str:
    """将数值格式化为紧凑显示（如 1.2M、35.6K），用于图表标注。"""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    if absolute >= 10:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")
