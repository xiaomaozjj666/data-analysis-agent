"""图表生成辅助函数：聚合、标注、尺度控制、标题清理与文件名生成。

实现已按职责拆分到 ``_charts`` 子包，本模块仅做重新导出，保持
``from data_agent.tools.charts import X`` 导入路径不变。

这些函数被 ``builder.build_tools`` 中的 ``create_visualization`` 工具调用，
本身不绑定 workspace，可独立测试。
"""

from __future__ import annotations

from ._charts import (  # noqa: F401
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
    _add_missing_combination_markers,
    _aggregate_for_chart,
    _annotate_extreme_values,
    _append_title_note,
    _apply_outlier_scale_controls,
    _box_stats,
    _chart_filename_stem,
    _checked_columns,
    _humanize_chart_title,
    _infer_chart_type,
    _localize_boolean_categories,
    _looks_like_datetime_series,
    _looks_like_id_column,
    _name_looks_like_id,
    _numeric_columns,
    _plotly_auto_interpret,
    _plotly_binned_histogram,
    _plotly_box_from_stats,
    _plotly_interpret_box,
    _plotly_interpret_heatmap,
    _plotly_interpret_hierarchy,
    _plotly_interpret_pie,
    _plotly_interpret_scatter,
    _plotly_interpret_trend,
    _severe_axis_compression,
    _stat_tick_format,
    _trace_axis_values,
    _validate_chart_semantics,
)
