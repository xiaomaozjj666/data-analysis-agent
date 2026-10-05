"""图表生成辅助函数包：聚合、标注、尺度控制、标题清理与文件名生成。

从原 ``charts.py`` 拆分为多个子模块，本 ``__init__`` 重新导出全部公共符号，
保持 ``from data_agent.tools.charts import X`` 导入路径不变。

子模块职责：
- ``constants``: 命名常量与正则模式。
- ``annotate``: 标题附注与极值标注。
- ``aggregate``: 分组聚合、缺失组合标记、布尔值本地化。
- ``scale``: 极端值尺度控制（主体尺度/全量视图切换）。
- ``validate``: 列校验、标识符检测、高基数拦截、时间维度识别。
- ``infer``: 自动选图推断。
- ``title``: 标题清理与文件名生成。
- ``interpret``: Plotly 分支白话解读。
- ``stat``: 大数据统计图服务端聚合（直方图分箱、箱线图五数概括）。
"""

from __future__ import annotations

from .aggregate import (
    _add_missing_combination_markers,
    _aggregate_for_chart,
    _localize_boolean_categories,
)
from .annotate import _annotate_extreme_values, _append_title_note
from .constants import (
    _BOOLEAN_VALUE_LABELS,
    _CHART_TITLE_MAX_CHARS,
    _CHART_TITLE_TECHNICAL_PATTERNS,
    _CHART_TYPE_LABELS_ZH,
    _HAS_RECORDS_COLUMN,
    _HOVER_TEXT_COLUMN,
    _MAX_REINDEX_COMBINATIONS,
    _SAMPLE_COUNT_COLUMN,
    STAT_AGG_THRESHOLD,
    VIOLIN_SAMPLE_ROWS,
)
from .infer import _infer_chart_type
from .interpret import (
    _plotly_auto_interpret,
    _plotly_interpret_box,
    _plotly_interpret_heatmap,
    _plotly_interpret_hierarchy,
    _plotly_interpret_pie,
    _plotly_interpret_scatter,
    _plotly_interpret_trend,
)
from .scale import (
    _apply_outlier_scale_controls,
    _severe_axis_compression,
    _trace_axis_values,
)
from .stat import (
    _box_stats,
    _plotly_binned_histogram,
    _plotly_box_from_stats,
    _stat_tick_format,
)
from .title import _chart_filename_stem, _humanize_chart_title
from .validate import (
    _checked_columns,
    _looks_like_datetime_series,
    _looks_like_id_column,
    _name_looks_like_id,
    _numeric_columns,
    _validate_chart_semantics,
)

__all__ = [
    "STAT_AGG_THRESHOLD",
    "VIOLIN_SAMPLE_ROWS",
    "_BOOLEAN_VALUE_LABELS",
    "_CHART_TITLE_MAX_CHARS",
    "_CHART_TITLE_TECHNICAL_PATTERNS",
    "_CHART_TYPE_LABELS_ZH",
    "_HAS_RECORDS_COLUMN",
    "_HOVER_TEXT_COLUMN",
    "_MAX_REINDEX_COMBINATIONS",
    "_SAMPLE_COUNT_COLUMN",
    "_add_missing_combination_markers",
    "_aggregate_for_chart",
    "_annotate_extreme_values",
    "_append_title_note",
    "_apply_outlier_scale_controls",
    "_box_stats",
    "_chart_filename_stem",
    "_checked_columns",
    "_humanize_chart_title",
    "_infer_chart_type",
    "_localize_boolean_categories",
    "_looks_like_datetime_series",
    "_looks_like_id_column",
    "_name_looks_like_id",
    "_numeric_columns",
    "_plotly_auto_interpret",
    "_plotly_binned_histogram",
    "_plotly_box_from_stats",
    "_plotly_interpret_box",
    "_plotly_interpret_heatmap",
    "_plotly_interpret_hierarchy",
    "_plotly_interpret_pie",
    "_plotly_interpret_scatter",
    "_plotly_interpret_trend",
    "_severe_axis_compression",
    "_stat_tick_format",
    "_trace_axis_values",
    "_validate_chart_semantics",
]
