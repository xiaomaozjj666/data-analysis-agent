"""数据分析工具集公共入口。

实现已按职责拆分到子模块：
- ``builder``: ``build_tools`` 工厂与 Plotly 暗色模式脚本等渲染常量。
- ``charts``: 图表生成辅助（聚合、标注、尺度控制、标题清理、文件名）。
- ``_cleaning``: 数据清洗辅助（列名规范化、缺失值、离群值）。
- ``_helpers``: 通用纯函数（列名可读化、数值紧凑格式化）。

本 ``__init__`` 重新导出原 ``tools.py`` 的全部模块级符号，保持后向兼容：
``from data_agent.tools import build_tools`` / ``_PLOTLY_DARK_MODE_SCRIPT``
等导入路径不变。
"""

from __future__ import annotations

from ._cleaning import (
    _apply_missing_strategy,
    _handle_outliers,
    _normalize_column_names,
    _parse_numeric_columns,
    _trim_string_columns,
)
from ._helpers import _compact_number, _human_column_label
from .builder import (
    _AGGREGATION_LABELS,
    _CHART_COLORS,
    _CHI_SQUARE_MAX_CARDINALITY,
    _GROUPBY_MAX_ROWS,
    _PLOTLY_DARK_MODE_SCRIPT,
    _SCATTER_MATRIX_MAX_DIMENSIONS,
    _TRANSFORM_LIMIT_MAX,
    build_tools,
)
from .charts import (
    _BOOLEAN_VALUE_LABELS,
    _CHART_TITLE_MAX_CHARS,
    _CHART_TITLE_TECHNICAL_PATTERNS,
    _CHART_TYPE_LABELS_ZH,
    _HAS_RECORDS_COLUMN,
    _HOVER_TEXT_COLUMN,
    _MAX_REINDEX_COMBINATIONS,
    _SAMPLE_COUNT_COLUMN,
    _add_missing_combination_markers,
    _aggregate_for_chart,
    _annotate_extreme_values,
    _append_title_note,
    _apply_outlier_scale_controls,
    _chart_filename_stem,
    _checked_columns,
    _humanize_chart_title,
    _localize_boolean_categories,
    _numeric_columns,
    _severe_axis_compression,
    _trace_axis_values,
)

__all__ = [
    "_AGGREGATION_LABELS",
    "_CHART_COLORS",
    "_CHART_TITLE_MAX_CHARS",
    "_CHART_TITLE_TECHNICAL_PATTERNS",
    "_CHART_TYPE_LABELS_ZH",
    "_CHI_SQUARE_MAX_CARDINALITY",
    "_GROUPBY_MAX_ROWS",
    "_HOVER_TEXT_COLUMN",
    "_HAS_RECORDS_COLUMN",
    "_MAX_REINDEX_COMBINATIONS",
    "_PLOTLY_DARK_MODE_SCRIPT",
    "_SAMPLE_COUNT_COLUMN",
    "_SCATTER_MATRIX_MAX_DIMENSIONS",
    "_TRANSFORM_LIMIT_MAX",
    "_BOOLEAN_VALUE_LABELS",
    "_add_missing_combination_markers",
    "_aggregate_for_chart",
    "_annotate_extreme_values",
    "_append_title_note",
    "_apply_missing_strategy",
    "_apply_outlier_scale_controls",
    "_chart_filename_stem",
    "_checked_columns",
    "_compact_number",
    "_handle_outliers",
    "_human_column_label",
    "_humanize_chart_title",
    "_localize_boolean_categories",
    "_normalize_column_names",
    "_numeric_columns",
    "_parse_numeric_columns",
    "_severe_axis_compression",
    "_trace_axis_values",
    "_trim_string_columns",
    "build_tools",
]
