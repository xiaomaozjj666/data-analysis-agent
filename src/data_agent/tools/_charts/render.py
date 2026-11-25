"""``create_visualization`` 的 Plotly 渲染：figure 构建、尺度控制与样式。

从 ``_tools_charts.create_visualization`` 拆出的中段（原函数 385~724 行）：
- ``build_figure``: 构造 ``px.*`` figure（按图型分派），并解析密度视图；
- ``apply_scale_and_overlays``: 极端值尺度控制、WebGL 切换、缺失组合标记、
  极值标注（返回可能被替换的 figure 与 scale_details）；
- ``apply_base_layout``: 统一底版布局与坐标轴样式；
- ``apply_trace_style``: 图型相关 trace 样式（柱/hover、散点标记与结构锚点、
  图例标题、密度图收尾）。

调用顺序必须保持"尺度控制 → 底版布局 → nice ticks → trace 样式"——nice ticks
由 ``_tools_charts`` 持有（保留既有 patch 路径），因此单独一步。
"""

from __future__ import annotations

from html import escape
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .._helpers import _human_column_label, _scatter_structure
from .._limits import _SCATTER_MATRIX_MAX_DIMENSIONS
from .._plotly_render import (
    _CHART_COLORS,
    _density_view_for,
    _plotly_webgl_if_large,
    _refine_scatter_3d,
    _stratified_sample,
    scatter_point_style,
)
from ..density_plotly import density_figure, finalize_density_layout
from .aggregate import _add_missing_combination_markers
from .annotate import _annotate_extreme_values
from .constants import (
    _HAS_RECORDS_COLUMN,
    _HOVER_TEXT_COLUMN,
    _SAMPLE_COUNT_COLUMN,
    STAT_AGG_THRESHOLD,
)
from .scale import _apply_outlier_scale_controls
from .stat import _plotly_binned_histogram, _plotly_box_from_stats
from .validate import _numeric_columns

#: 散点矩阵（SPLOM）降采样上限：N 维 × M 行产生 N² 个子图，每个 trace 含
#: 全量 M 行。8 维 10 万行 = 64 个 trace × 10 万点 = 640 万点负载，浏览器
#: 会卡死。与 ECharts SPLOM 的 400 行抽样保持一致，超过则均匀抽样。
_SPLOM_MAX_ROWS = 1000


def build_figure(spec: Any) -> go.Figure:
    """按图型构造 Plotly figure（散点先解析是否走密度视图）。"""
    # Plotly title 支持 HTML 子集，需 escape 防止 LLM 输出注入 <b>/<i> 等标签
    # 改变样式。与 ECharts 分支的 escape(title) 保持一致。
    common = {
        "data_frame": spec.df,
        "title": escape(spec.display_title),
        "template": "plotly_white",
        "labels": spec.labels,
        "color_discrete_sequence": _CHART_COLORS,
    }
    # 大数据散点 → 密度视图（见 data_agent/density.py）。聚合后的散点行数
    # 通常很小，只有"每行一个点"的原始散点才会触发。
    if spec.chart_type == "scatter" and spec.x and spec.y:
        spec.density_view, spec.density_scale = _density_view_for(
            spec.df,
            x=spec.x,
            y=spec.y,
            color=spec.color,
            scale_mode=spec.scale_mode,
        )
    return _figure_for_type(spec, common)


def _figure_for_type(spec: Any, common: dict[str, Any]) -> go.Figure:
    """按 ``chart_type`` 分派到具体 figure 构造器（未知类型落 treemap，同原实现）。"""
    chart_type = spec.chart_type
    if chart_type == "bar":
        return _bar_figure(spec, common)
    if chart_type == "line":
        return px.line(**common, x=spec.x, y=spec.y, color=spec.color, markers=True)
    if chart_type == "area":
        return px.area(**common, x=spec.x, y=spec.y, color=spec.color)
    if chart_type == "scatter":
        return _scatter_figure(spec, common)
    if chart_type == "scatter_3d":
        return _scatter_3d_figure(spec, common)
    if chart_type == "histogram":
        return _histogram_figure(spec, common)
    if chart_type == "box":
        return _box_figure(spec, common)
    if chart_type == "violin":
        return _violin_figure(spec, common)
    if chart_type == "pie":
        return _pie_figure(spec, common)
    if chart_type == "heatmap":
        return _heatmap_figure(spec, common)
    if chart_type == "correlation_heatmap":
        return _correlation_heatmap_figure(spec, common)
    if chart_type == "scatter_matrix":
        return _scatter_matrix_figure(spec, common)
    return _hierarchy_figure(spec, common)


def _bar_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    """柱图：聚合后预计算 hover 文本，无记录组合显示"无样本/无记录"。"""
    custom_data: list[str] | None = None
    # 预计算 hover 文本：对无记录 bar（reindex 产生的空白组合）显示
    # "无样本/无记录"，而非让 Plotly 把 y=NaN 格式化成 "nan"。
    # 聚合后行数通常很少（x_levels × color_levels），iterrows 开销可忽略。
    if spec.aggregation != "none" and _SAMPLE_COUNT_COLUMN in spec.df.columns:
        x_label_hover = _human_column_label(spec.x)
        y_label_hover = _human_column_label(spec.y) if spec.y else ""
        hover_lines: list[str] = []
        for _, row in spec.df.iterrows():
            if row[_HAS_RECORDS_COLUMN]:
                lines = [f"{x_label_hover}：{row[spec.x]}"]
                if spec.y:
                    y_val = row[spec.y]
                    y_text = f"{float(y_val):,.2f}" if pd.notna(y_val) else "—"
                    lines.append(f"{y_label_hover}：{y_text}")
                lines.append(f"样本数：{int(row[_SAMPLE_COUNT_COLUMN])}")
                hover_lines.append("<br>".join(lines))
            else:
                missing_label = (
                    "无样本" if spec.aggregation in {"mean", "median", "min", "max"} else "无记录"
                )
                hover_lines.append(f"{x_label_hover}：{row[spec.x]}<br>{missing_label}")
        spec.df[_HOVER_TEXT_COLUMN] = hover_lines
        custom_data = [_SAMPLE_COUNT_COLUMN, _HAS_RECORDS_COLUMN, _HOVER_TEXT_COLUMN]
    return px.bar(
        **common,
        x=spec.x,
        y=spec.y,
        color=spec.color,
        barmode="group",
        custom_data=custom_data,
    )


def _scatter_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    """散点图：大数据走密度视图，否则逐点 scatter。"""
    if spec.density_view is not None:
        return density_figure(
            spec.df,
            x=spec.x,
            y=spec.y,
            color=spec.color,
            view=spec.density_view,
            x_label=spec.labels.get(spec.x, _human_column_label(spec.x)),
            y_label=spec.labels.get(spec.y, _human_column_label(spec.y)),
            title=common["title"],
            colors=_CHART_COLORS,
        )
    return px.scatter(
        **common, x=spec.x, y=spec.y, color=spec.color, size=spec.size, trendline=None
    )


def _scatter_3d_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    if not spec.x or not spec.y or not spec.z:
        raise ValueError("scatter_3d 需要 x、y、z。")
    fig = px.scatter_3d(**common, x=spec.x, y=spec.y, z=spec.z, color=spec.color, size=spec.size)
    _refine_scatter_3d(fig, rows=len(spec.df))
    return fig


def _histogram_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    """直方图：大数据服务端分箱（纵轴仍是真实记录数，不是抽样估计）。"""
    fig: go.Figure | None = None
    if len(spec.df) > STAT_AGG_THRESHOLD:
        binned = _plotly_binned_histogram(
            spec.df,
            x=spec.x,
            bins=spec.bins,
            colors=_CHART_COLORS,
            color=spec.color,
            labels=spec.labels,
            title=common["title"],
        )
        if binned is not None:
            fig, stat_agg = binned
            stat_agg["kind"] = "histogram"
            spec.stat_agg = stat_agg
    if fig is None:
        fig = px.histogram(
            **common,
            x=spec.x,
            color=spec.color,
            nbins=max(2, min(spec.bins, 200)),
            marginal="box",
        )
    return fig


def _box_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    """箱线图：大数据用全量五数概括绘制，箱体与逐点绘制一致。"""
    fig: go.Figure | None = None
    if len(spec.df) > STAT_AGG_THRESHOLD and spec.y:
        stats_fig = _plotly_box_from_stats(
            spec.df,
            x=spec.x,
            y=spec.y,
            colors=_CHART_COLORS,
            color=spec.color,
            labels=spec.labels,
            title=common["title"],
        )
        if stats_fig is not None:
            fig, stat_agg = stats_fig
            stat_agg["kind"] = "box"
            spec.stat_agg = stat_agg
    if fig is None:
        fig = px.box(**common, x=spec.x, y=spec.y, color=spec.color, points="outliers")
    return fig


def _violin_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    violin_common = common
    if len(spec.df) > STAT_AGG_THRESHOLD:
        # 小提琴的形状由核密度决定，分层抽样两万行与全量肉眼无差别；
        # 逐样本点云才是 MB 级载荷的来源。
        violin_common = {**common, "data_frame": _stratified_sample(spec.df, spec.color)}
    return px.violin(
        **violin_common,
        x=spec.x,
        y=spec.y,
        color=spec.color,
        box=True,
        points="outliers",
    )


def _pie_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    if not spec.x:
        raise ValueError("pie 需要 x 作为名称列。")
    return px.pie(**common, names=spec.x, values=spec.values or spec.y, hole=0.42)


def _heatmap_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    if not spec.x or not spec.y or not spec.values:
        raise ValueError("heatmap 需要 x、y、values。")
    pivot = spec.df.pivot_table(index=spec.y, columns=spec.x, values=spec.values, aggfunc="mean")
    # 与原实现一致：heatmap 使用未 escape 的原始 title（px.imshow 的标题）。
    return px.imshow(
        pivot,
        text_auto=True,
        aspect="auto",
        title=spec.title,
        color_continuous_scale="RdBu_r",
    )


def _correlation_heatmap_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    numeric = _numeric_columns(spec.df, spec.dimensions)
    corr = spec.df[numeric].corr()
    return px.imshow(
        corr,
        text_auto=".2f",
        aspect="auto",
        title=spec.title or "Correlation heatmap",
        color_continuous_scale="RdBu_r",
        zmin=-1,
        zmax=1,
    )


def _scatter_matrix_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    numeric = _numeric_columns(spec.df, spec.dimensions)
    if len(numeric) > _SCATTER_MATRIX_MAX_DIMENSIONS:
        raise ValueError(
            f"scatter_matrix 最多支持 {_SCATTER_MATRIX_MAX_DIMENSIONS} 个维度，请缩小 dimensions。"
        )
    # SPLOM 降采样：N 维 × M 行产生 N² 个子图，每个 trace 含全量 M 行。
    # 8 维 10 万行 = 64 个 trace × 10 万点 = 640 万点负载，浏览器会卡死。
    if len(spec.df) > _SPLOM_MAX_ROWS:
        matrix_common = {
            **common,
            "data_frame": spec.df.sample(n=_SPLOM_MAX_ROWS, random_state=42),
        }
    else:
        matrix_common = common
    fig = px.scatter_matrix(**matrix_common, dimensions=numeric, color=spec.color)
    fig.update_traces(diagonal_visible=False)
    return fig


def _hierarchy_figure(spec: Any, common: dict[str, Any]) -> go.Figure:
    """sunburst / treemap（未知图型按 treemap 处理，与原实现一致）。"""
    if spec.chart_type == "sunburst":
        if not spec.path_columns:
            raise ValueError("sunburst 需要 path_columns。")
        return px.sunburst(**common, path=spec.path_columns, values=spec.values)
    if not spec.path_columns:
        raise ValueError("treemap 需要 path_columns。")
    return px.treemap(**common, path=spec.path_columns, values=spec.values, color=spec.color)


def apply_scale_and_overlays(fig: go.Figure, spec: Any) -> tuple[go.Figure, dict[str, Any]]:
    """尺度控制、WebGL 切换、缺失组合标记与极值标注。

    返回 ``(fig, scale_details)``：``_plotly_webgl_if_large`` 可能替换成新的
    WebGL figure，调用方必须使用返回值。
    """
    if spec.density_view is not None:
        # 密度图：视口范围在聚合前已按"主体尺度"确定，网格只覆盖该范围，
        # 不能再交给 _apply_outlier_scale_controls 追加"全量视图"按钮
        # （网格没有覆盖范围外的数据，切过去只会看到角落一小块）。
        scale_details = spec.density_scale
    else:
        scale_details = _apply_outlier_scale_controls(fig, spec.chart_type, spec.scale_mode)
        # 大数据切换 WebGL：SVG 下 30 万行散点/折线的完整预览渲染以分钟计
        fig = _plotly_webgl_if_large(fig, spec.df, spec.chart_type)
    if spec.chart_type == "bar" and spec.aggregation != "none":
        _add_missing_combination_markers(
            fig,
            spec.coverage,
            x=spec.x,
            color=spec.color,
            aggregation=spec.aggregation,
        )
    # 自动标注关键统计量（最大值/最小值），仅用于柱状图和折线图
    if spec.chart_type in {"bar", "line"}:
        _annotate_extreme_values(fig)
    return fig, scale_details


def apply_base_layout(fig: go.Figure, spec: Any, scale_details: dict[str, Any]) -> None:
    """统一底版布局（字体、页边距、图例、坐标轴网格）。"""
    fig.update_layout(
        font={"family": "IBM Plex Sans, Noto Sans SC, sans-serif", "color": "#102a2a"},
        paper_bgcolor="#fbfaf5",
        plot_bgcolor="#fbfaf5",
        colorway=_CHART_COLORS,
        margin=(
            # 3D 场景本身是正方形；通用页边距会在四周留下大片空白。
            {"l": 0, "r": 0, "t": 56, "b": 70}
            if spec.chart_type == "scatter_3d"
            else {
                "l": 64,
                "r": 36,
                "t": 108
                if scale_details["scale_mode"] == "robust"
                or spec.coverage.get("missing_combinations")
                else 76,
                # 底部为横向图例预留空间（图例高约 30px + 刻度空间）
                "b": 120,
            }
        ),
        hoverlabel={"bgcolor": "#102a2a", "font_color": "white"},
        title={"x": 0.01, "xanchor": "left", "font": {"size": 22, "color": "#102a2a"}},
        legend={
            # 图例放绘图区下方居中横向排布：竖排图例（含绘图区内
            # overlay）会挤压绘图区或在窄容器下溢出 SVG 被裁、或
            # 遮挡数据点。横向图例不占绘图区宽度、不遮挡数据，
            # 任何容器宽度都不溢出（放不下时 plotly 自动换行）。
            # margin.b 需容纳图例高度（约 30px）+ 原底部刻度空间。
            "orientation": "h",
            "x": 0.5,
            "xanchor": "center",
            "y": -0.15,
            "yanchor": "top",
            "bgcolor": "rgba(255,255,255,0.82)",
            "bordercolor": "#D9E1DE",
            "borderwidth": 1,
            "font": {"size": 12},
        },
        autosize=True,
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor="#E5ECE9",
        zerolinecolor="#C9D5D1",
        linecolor="#BFCBC7",
        tickfont={"size": 12},
        title_font={"size": 14},
        automargin=True,
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="#E5ECE9",
        zerolinecolor="#C9D5D1",
        linecolor="#BFCBC7",
        tickfont={"size": 12},
        title_font={"size": 14},
        automargin=True,
        separatethousands=True,
    )


def apply_trace_style(fig: go.Figure, spec: Any) -> None:
    """图型相关 trace 样式（柱 hover、散点标记/结构锚点、图例、密度收尾）。"""
    if spec.chart_type == "bar":
        fig.update_traces(
            marker_line_color="rgba(16,42,42,0.16)",
            marker_line_width=0.8,
            selector={"type": "bar"},
        )
        if spec.aggregation != "none" and _HOVER_TEXT_COLUMN in spec.df.columns:
            # 用预计算的 hover 文本（customdata[2]），避免无记录 bar 显示 nan。
            # customdata 结构：[sample_count, has_records, hover_text]
            fig.update_traces(
                hovertemplate="%{customdata[2]}<extra>%{fullData.name}</extra>",
                selector={"type": "bar"},
            )
        if spec.x and not pd.api.types.is_numeric_dtype(spec.df[spec.x]):
            fig.update_xaxes(categoryorder="total descending")
        # 堆叠柱（stacked=True）与分组柱：Plotly 用 barmode 表达，
        # 与 ECharts 分支的 series.stack 语义一致。
        fig.update_layout(
            bargap=0.26, bargroupgap=0.08, barmode="stack" if spec.stacked else "group"
        )
    elif spec.chart_type == "scatter" and spec.density_view is None:
        # 点径与透明度随点数分档（与 ECharts 分支同一张表，见 scatter_point_style）：
        # 固定 9px/0.82 在几千点以上就会糊成一团实心色块，分组色互相覆盖，
        # 看起来"只有一个颜色"；分档缩小并降低透明度后，密集区自然混色、
        # 稀疏区与放大后仍能分辨单点。
        marker_style = scatter_point_style(len(spec.df))
        fig.update_traces(marker=marker_style, selector={"type": "scatter"})
        fig.update_traces(marker=marker_style, selector={"type": "scattergl"})
        # 结构注记（Tableau 式视觉锚点）：OLS 趋势线 + 双轴均值象限线。
        # 大点云没有锚点就是一堵"点墙"（实测用户反馈眼花缭乱）。
        if spec.x and spec.y and spec.x in spec.df.columns and spec.y in spec.df.columns:
            _add_scatter_structure(fig, spec)
    if spec.color:
        fig.update_layout(legend_title_text=_human_column_label(spec.color))
    if spec.density_view is not None:
        # 密度图收尾：清掉边缘直方图的网格/刻度，并为档位色标留出右边距
        # （必须在上面通用布局样式之后调用，否则会被覆盖）。
        finalize_density_layout(fig, spec.density_view)


def _add_scatter_structure(fig: go.Figure, spec: Any) -> None:
    """在散点图上叠加 OLS 趋势线与均值参考线（基于全量数据）。"""
    pair_df = spec.df[[spec.x, spec.y]].apply(pd.to_numeric, errors="coerce").dropna()
    structure = (
        # 按位置取列：x 与 y 同名时 pair_df[x] 是两列的 DataFrame
        _scatter_structure(
            pair_df.iloc[:, 0].astype(float).tolist(),
            pair_df.iloc[:, 1].astype(float).tolist(),
        )
        if len(pair_df) >= 3
        else None
    )
    if not structure:
        return
    trend = structure.get("trend")
    r_value = structure.get("r")
    if trend:
        trend_name = f"趋势线 r={r_value:.2f}" if r_value is not None else "趋势线"
        fig.add_trace(
            go.Scatter(
                x=[trend[0], trend[2]],
                y=[trend[1], trend[3]],
                mode="lines",
                name=trend_name,
                line={"color": "#E15759", "width": 2.5},
                hoverinfo="skip",
            )
        )
    if structure.get("mean_y") is not None:
        fig.add_hline(
            y=structure["mean_y"],
            line_dash="dash",
            line_color="#9aa0a6",
            line_width=1.2,
            annotation_text=f"y 均值 {structure['mean_y']:,.1f}",
            annotation_position="top right",
            annotation_font={"size": 10, "color": "#6b7280"},
        )
    if structure.get("mean_x") is not None:
        fig.add_vline(
            x=structure["mean_x"],
            line_dash="dash",
            line_color="#9aa0a6",
            line_width=1.2,
            annotation_text=f"x 均值 {structure['mean_x']:,.1f}",
            annotation_position="top right",
            annotation_font={"size": 10, "color": "#6b7280"},
        )
