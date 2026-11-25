"""数据分析工具集：为 ReAct 执行器提供会话绑定的数据操作能力。

工具定义已按域拆分到同包子模块，本模块保留 ``build_tools`` 工厂并重新导出
原有模块级符号，``from data_agent.tools.builder import build_tools`` 的导入
路径与返回结构保持不变。工具列表顺序仍与拆分前一致：

- ``_tools_check``: inspect_data / repair_data_format
- ``_tools_clean``: clean_data / transform_data
- ``_tools_datasets``: join_datasets / adopt_dataset
- ``_tools_sources``: web_search / query_database
- ``_tools_analysis``: compute_registered_metrics / statistical_analysis
- ``_tools_charts``: create_visualization（含 Plotly nice ticks 辅助）
- ``_tools_export``: export_data
- ``_sandbox``: run_python_code

设计原则：
- 所有工具通过闭包绑定同一个 DataWorkspace 实例，确保状态一致性。
- 清洗和变换操作带有严格的安全护栏（最小行数、最大删除比例）。
- 可视化自动检测极端值并提供主体尺度/全量视图切换，不修改原始数据。
- 工具返回 JSON 字符串，由 json_text() 统一序列化，确保 NaN/Infinity
  等非法 JSON 值被转为 null。
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool

from data_agent.workspace import DataWorkspace

from ._cleaning import (  # noqa: F401
    _apply_missing_strategy,
    _handle_outliers,
    _normalize_column_names,
    _parse_numeric_columns,
    _trim_string_columns,
)
from ._helpers import (  # noqa: F401
    _human_column_label,
    _nice_ticks,
    _plotly_axis_tickformat,
    _scatter_structure,
)
from ._join import (  # noqa: F401
    list_available_sources,
    merge_datasets,
    normalize_keys,
    resolve_source,
    validate_keys,
)
from ._limits import (  # noqa: F401
    _CHI_SQUARE_MAX_CARDINALITY,
    _GROUPBY_MAX_ROWS,
    _SCATTER_MATRIX_MAX_DIMENSIONS,
    _TRANSFORM_LIMIT_MAX,
)
from ._plotly_render import (  # noqa: F401
    _AGGREGATION_LABELS,
    _CHART_COLORS,
    _PLOTLY_DARK_MODE_SCRIPT,
    _PLOTLY_GL_REFRESH_SCRIPT,
    _PLOTLY_WEBGL_THRESHOLD,
    SCATTER_POINT_STYLE,
    _as_scattergl,
    _collect_trace_values,
    _density_view_for,
    _plotly_webgl_if_large,
    _refine_scatter_3d,
    _render_plotly_html,
    _scatter_3d_aspect,
    _stat_agg_note,
    _stratified_sample,
    append_html_body_block,
    render_data_table_html,
    scatter3d_data_summary,
    scatter_point_style,
    scatter_symbol_style,
)
from ._sandbox import build_run_python_code
from ._stats import run_statistical_analysis  # noqa: F401
from ._tools_analysis import build_analysis_tools
from ._tools_charts import (
    _apply_plotly_nice_ticks,  # noqa: F401
    build_chart_tools,
)
from ._tools_check import build_check_tools
from ._tools_clean import build_clean_tools
from ._tools_datasets import build_dataset_tools
from ._tools_export import build_export_tools
from ._tools_sources import build_source_tools
from .charts import (  # noqa: F401
    _BOOLEAN_VALUE_LABELS,
    _HAS_RECORDS_COLUMN,
    _HOVER_TEXT_COLUMN,
    _SAMPLE_COUNT_COLUMN,
    STAT_AGG_THRESHOLD,
    VIOLIN_SAMPLE_ROWS,
    _add_missing_combination_markers,
    _aggregate_for_chart,
    _annotate_extreme_values,
    _apply_outlier_scale_controls,
    _chart_filename_stem,
    _checked_columns,
    _ensure_plotly_fig_has_data,
    _humanize_chart_title,
    _infer_chart_type,
    _localize_boolean_categories,
    _numeric_columns,
    _plotly_auto_interpret,
    _plotly_binned_histogram,
    _plotly_box_from_stats,
    _validate_chart_semantics,
)
from .density_plotly import (  # noqa: F401
    density_figure,
    density_interpretation,
    finalize_density_layout,
)

logger = logging.getLogger(__name__)


def build_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建绑定到指定工作区的工具集，供 ReAct Agent 使用。

    所有工具通过闭包捕获同一个 workspace 实例，确保数据状态一致性。
    工具列表的顺序即为 Agent 看到的工具顺序。

    Args:
        workspace: 当前分析会话的数据工作区。

    Returns:
        11 个 BaseTool 实例的列表。
    """
    return [
        *build_check_tools(workspace),
        *build_clean_tools(workspace),
        *build_dataset_tools(workspace),
        *build_source_tools(workspace),
        *build_analysis_tools(workspace),
        *build_chart_tools(workspace),
        *build_export_tools(workspace),
        build_run_python_code(workspace),
    ]
