"""ECharts 渲染引擎：与 Plotly 引擎并列，复用同一套数据准备逻辑。

设计目标：
- 学术级交互：tooltip 业务解读、区间缩放、图例多选、平滑动画、高清导出
- 视觉统一：固定色板、弱化网格、充足留白、圆角阴影、深浅主题
- 零侵入：与 Plotly 引擎完全隔离，HTML/JS bundle 互不冲突
- 自动解读：每次生成图表附带数据驱动的白话分析

调用入口：``_render_echarts(workspace, df, chart_type, ...)`` 返回与 Plotly
分支结构一致的 response dict，``create_visualization`` 据此分派。

实现已按职责拆分到 ``data_agent.render`` 子包（primitives / interpret /
series_* / density_* / output / dispatch），本模块是**门面**：保留
``from data_agent.echarts_engine import X`` 的既有导入路径与全部符号，
其中 ``_auto_interpret`` 仍定义在本模块——它的异常兜底与可被替换的
``_interpret_impl`` 是既有测试契约（见 ``tests/test_echarts_engine.py``）。
"""

from __future__ import annotations

import pandas as pd

from data_agent.density import DensityView
from data_agent.echarts_constants import (  # noqa: F401
    _DENSITY_DETAIL_POINTS,
    _ECHARTS_DARK_MODE_SCRIPT,
    _ECHARTS_FONT_FAMILY,
    _ECHARTS_HTML_TEMPLATE,
    _ECHARTS_PALETTE,
    _JsFunction,
)
from data_agent.render.density_option import (  # noqa: F401
    _density_category_axis,
    _density_marginal_series,
    _density_marginal_value_axis,
    _density_tooltip_formatter,
    _density_visual_map,
    _echarts_scatter_density,
)
from data_agent.render.density_view import (  # noqa: F401
    _density_cells,
    _density_detail_series,
    _density_extreme_series,
    _density_panel_structure,
    _density_panel_title,
    _density_trend_series,
    _density_view_for,
)
from data_agent.render.dispatch import (  # noqa: F401
    _build_echarts_option,
    _render_echarts,
)
from data_agent.render.interpret import (  # noqa: F401
    _density_interpretation,
    _interpret_box,
    _interpret_heatmap,
    _interpret_hierarchy,
    _interpret_impl,
    _interpret_pie,
    _interpret_scatter,
    _interpret_trend,
)
from data_agent.render.output import (  # noqa: F401
    _build_echarts_html,
    _json_default,
    _serialize_option,
)
from data_agent.render.primitives import (  # noqa: F401
    _axis_label_interval,
    _bin_index,
    _bin_labels,
    _build_linear_gradient,
    _echarts_value_axis,
    _format_number,
    _format_time_categories,
    _hex_to_rgba,
    _round_significant,
    _rows_data,
    _safe_value,
    _series_color,
    _snap_edges,
    _stacked_totals,
    _within_range,
)
from data_agent.render.series_3d import (  # noqa: F401
    _echarts_scatter3d,
    _scatter3d_data_summary,
    _stratified_3d_sample,
)
from data_agent.render.series_cartesian import (  # noqa: F401
    _bar_tooltip_formatter,
    _echarts_bar,
    _echarts_line,
)
from data_agent.render.series_distribution import (  # noqa: F401
    _echarts_box,
    _echarts_heatmap,
    _echarts_histogram,
    _echarts_pie,
)
from data_agent.render.series_hierarchy import _echarts_sunburst  # noqa: F401
from data_agent.render.series_matrix import (  # noqa: F401
    _echarts_scatter_matrix,
    _splom_cell_axis,
)
from data_agent.render.series_scatter import (  # noqa: F401
    _append_structure_annotations,
    _echarts_scatter,
    _scatter_tooltip_formatter,
    _size_func,
)
from data_agent.workspace import (  # noqa: F401
    ECHARTS_CDN_URL,
    ECHARTS_GL_CDN_URL,
    DataWorkspace,
    _atomic_write_text,
)

__all__ = [
    # 公开面 / 常量
    "ECHARTS_CDN_URL",
    "ECHARTS_GL_CDN_URL",
    "DataWorkspace",
    "_DENSITY_DETAIL_POINTS",
    "_ECHARTS_DARK_MODE_SCRIPT",
    "_ECHARTS_FONT_FAMILY",
    "_ECHARTS_HTML_TEMPLATE",
    "_ECHARTS_PALETTE",
    "_JsFunction",
    "_atomic_write_text",
    # 入口
    "_auto_interpret",
    "_build_echarts_option",
    "_render_echarts",
    # 中性层
    "_axis_label_interval",
    "_bin_index",
    "_bin_labels",
    "_build_linear_gradient",
    "_echarts_value_axis",
    "_format_number",
    "_format_time_categories",
    "_hex_to_rgba",
    "_round_significant",
    "_rows_data",
    "_safe_value",
    "_series_color",
    "_snap_edges",
    "_stacked_totals",
    "_within_range",
    # 解读
    "_density_interpretation",
    "_interpret_box",
    "_interpret_heatmap",
    "_interpret_hierarchy",
    "_interpret_impl",
    "_interpret_pie",
    "_interpret_scatter",
    "_interpret_trend",
    # 各图表族
    "_append_structure_annotations",
    "_bar_tooltip_formatter",
    "_echarts_bar",
    "_echarts_box",
    "_echarts_heatmap",
    "_echarts_histogram",
    "_echarts_line",
    "_echarts_pie",
    "_echarts_scatter",
    "_echarts_scatter3d",
    "_echarts_scatter_density",
    "_echarts_scatter_matrix",
    "_echarts_sunburst",
    "_scatter3d_data_summary",
    "_scatter_tooltip_formatter",
    "_size_func",
    "_splom_cell_axis",
    "_stratified_3d_sample",
    # 密度视图
    "_density_category_axis",
    "_density_cells",
    "_density_detail_series",
    "_density_extreme_series",
    "_density_marginal_series",
    "_density_marginal_value_axis",
    "_density_panel_structure",
    "_density_panel_title",
    "_density_tooltip_formatter",
    "_density_trend_series",
    "_density_view_for",
    "_density_visual_map",
    # 输出
    "_build_echarts_html",
    "_json_default",
    "_serialize_option",
]


def _auto_interpret(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    aggregation: str,
    title: str | None,
    density_view: DensityView | None = None,
    stacked: bool = False,
) -> str:
    """基于聚合结果生成一段业务白话解读。

    解读策略：
    - bar/line/area：找最高最低、计算极差与均值比、识别拐点（最大环比变化）
    - pie：找占比最高与最低的类别，给出结构判断
    - scatter：识别相关性方向、离群点；超大数据走密度视图时换成密度编码说明
      （见 ``density_view`` 参数——逐点解读对聚合后的网格不成立）
    - heatmap/correlation：识别最强正/负相关对
    - box/violin：对比中位数差异与离群点
    - 其他：给通用描述

    所有解读避免统计术语，用业务语言表达。
    """
    try:
        return _interpret_impl(
            df,
            chart_type=chart_type,
            x=x,
            y=y,
            color=color,
            aggregation=aggregation,
            title=title,
            density_view=density_view,
        )
    except Exception:
        # 解读失败不影响图表生成，返回空字符串让前端不渲染解读区。
        return ""
