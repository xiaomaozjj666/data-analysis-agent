"""可视化工具：``create_visualization`` 与 Plotly nice ticks 辅助。

从 ``builder.build_tools`` 拆出的图表工具定义（原实现约 822 行）。双引擎
分派、聚合、布尔本地化、top_n、抽样降采样、密度视图与 PNG 导出逻辑均
保持原实现不变；``_apply_plotly_nice_ticks`` 仅被本工具的 Plotly 分支使用。
"""

from __future__ import annotations

import json
from html import escape, unescape
from typing import Any, Literal

import pandas as pd
import plotly.express as px
from langchain_core.tools import BaseTool, tool

from data_agent import _kaleido_compat
from data_agent.chart_sampling import (
    _EMBED_MAX_POINTS,
    sample_plotly_figure_for_embed,
    sampling_note,
)
from data_agent.density import band_label, should_use_density
from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace, _atomic_write_text

from ._helpers import _human_column_label, _nice_ticks, _plotly_axis_tickformat, _scatter_structure
from ._limits import _SCATTER_MATRIX_MAX_DIMENSIONS
from ._plotly_render import (
    _AGGREGATION_LABELS,
    _CHART_COLORS,
    _PLOTLY_DARK_MODE_SCRIPT,
    _collect_trace_values,
    _density_view_for,
    _plotly_webgl_if_large,
    _refine_scatter_3d,
    _render_plotly_html,
    _stat_agg_note,
    _stratified_sample,
    append_html_body_block,
    render_data_table_html,
    scatter3d_data_summary,
    scatter_point_style,
)
from .charts import (
    _BOOLEAN_VALUE_LABELS,
    _HAS_RECORDS_COLUMN,
    _HOVER_TEXT_COLUMN,
    _SAMPLE_COUNT_COLUMN,
    STAT_AGG_THRESHOLD,
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
from .density_plotly import density_figure, density_interpretation, finalize_density_layout


def _apply_plotly_nice_ticks(
    fig: px.Figure,
    chart_type: str,
    x: str | None,
    y: str | None,
    scale_details: dict[str, Any],
) -> None:
    """对 Plotly 图表的数值轴应用 nice ticks（圆数刻度）。

    遍历所有 trace 收集 x/y 数值范围，据此设置 tickformat（千分位/缩写）。
    注意：不固定 dtick/tick0——plotly 的框选/滚轮缩放在放大后按新范围
    自动重算刻度；若固定步长，深度缩小的区间里可能一个刻度都没有，
    无法读取坐标轴数据做比较。category 轴跳过。
    """
    try:
        axis_ranges = scale_details.get("axis_ranges", {}) or {}
        # Y 轴（数值型图表都有，scatter_3d 的 y 轴也需要 nice ticks）
        if y and chart_type in {
            "bar",
            "line",
            "area",
            "scatter",
            "scatter_3d",
            "histogram",
            "box",
            "violin",
        }:
            y_range = axis_ranges.get("y")
            if y_range:
                vmin, vmax = float(y_range[0]), float(y_range[1])
            else:
                values = _collect_trace_values(fig, "y")
                if not values:
                    return
                vmin, vmax = float(min(values)), float(max(values))
            nice_min, nice_max, step = _nice_ticks(vmin, vmax, n=5)
            if step > 0:
                fig.update_yaxes(tickformat=_plotly_axis_tickformat((nice_min, nice_max)))
        # X 轴（仅散点图等数值 X 轴）
        if x and chart_type in {"scatter", "scatter_3d"}:
            x_range = axis_ranges.get("x")
            if x_range:
                vmin, vmax = float(x_range[0]), float(x_range[1])
            else:
                values = _collect_trace_values(fig, "x")
                if not values:
                    return
                vmin, vmax = float(min(values)), float(max(values))
            nice_min, nice_max, step = _nice_ticks(vmin, vmax, n=5)
            if step > 0:
                fig.update_xaxes(tickformat=_plotly_axis_tickformat((nice_min, nice_max)))
        # Z 轴（仅 scatter_3d）
        if chart_type == "scatter_3d":
            z_range = axis_ranges.get("z")
            if z_range:
                vmin, vmax = float(z_range[0]), float(z_range[1])
            else:
                values = _collect_trace_values(fig, "z")
                if not values:
                    return
                vmin, vmax = float(min(values)), float(max(values))
            nice_min, nice_max, step = _nice_ticks(vmin, vmax, n=5)
            if step > 0:
                fig.update_scenes(
                    zaxis=dict(tickformat=_plotly_axis_tickformat((nice_min, nice_max)))
                )
    except Exception:
        # nice ticks 是 best-effort，不能影响图表生成
        pass


def build_chart_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建可视化类工具（create_visualization）。"""


    @tool
    def create_visualization(
        chart_type: Literal[
            "auto",
            "bar",
            "line",
            "area",
            "scatter",
            "scatter_3d",
            "histogram",
            "box",
            "violin",
            "pie",
            "heatmap",
            "correlation_heatmap",
            "scatter_matrix",
            "sunburst",
            "treemap",
        ] = "auto",
        x: str | None = None,
        y: str | None = None,
        color: str | None = None,
        z: str | None = None,
        size: str | None = None,
        values: str | None = None,
        path_columns: list[str] | None = None,
        dimensions: list[str] | None = None,
        aggregation: Literal["none", "mean", "median", "sum", "count", "min", "max"] = "none",
        title: str | None = None,
        chart_engine: Literal["plotly", "echarts"] = "plotly",
        bins: int = 30,
        top_n: int | None = None,
        scale_mode: Literal["auto", "full"] = "auto",
        export_png: bool = False,
        stacked: bool = False,
    ) -> str:
        """Create an interactive chart and save a standalone HTML artifact.

        双引擎：``chart_engine="plotly"``（默认）走原 Plotly 渲染分支；
        ``chart_engine="echarts"`` 走 ECharts 渲染分支，适合正式报告，
        自带学术级交互（区间缩放、图例多选、平滑动画、高清导出）和
        数据驱动的白话解读。两个引擎复用同一套数据准备逻辑（聚合、
        布尔值本地化、top_n、_checked_columns），上层无感知切换。

        ``chart_type="auto"``（默认）会按数据列的类型与格式自动选择最合适
        的图型：时间序列（datetime x）→ 折线图；两数值列 → 散点图；
        分类列 + 数值列 → 柱状图（类别少且无 color 时退化成饼图做构成占比）；
        单数值列分布 → 直方图；层级 path_columns → 旭日图；多数值列
        （无 x/y）→ 相关性热力图；dimensions ≥3 → 散点矩阵。调用方仍可用
        显式 chart_type 覆盖自动选择。

        Supports standard and complex plots including 3D scatter, correlation heatmap, scatter
        matrix, sunburst and treemap. For bar/line/area, aggregation groups by x and optional
        color. heatmap expects x, y and numeric values. scatter_matrix uses dimensions.
        sunburst/treemap use path_columns and values. scale_mode="auto" detects scale-destroying
        extreme values and adds readable main/full viewport controls without changing the data;
        use scale_mode="full" only when the user explicitly wants the uncompressed raw scale.
        export_png is best-effort and needs Chrome.
        stacked=True stacks bars of the same category (ECharts series.stack /
        Plotly barmode="stack") and scales the Y axis to the stacked totals;
        omit it for grouped bars. Use stacked only when the user asks for
        堆叠/构成占比——标题写"堆叠"而图上是并排柱会让用户以为数据画错了。

        散点图可读性增强（双引擎一致）：自动叠加 OLS 趋势线（标注 r 值）与
        双轴均值象限参考线，给大点云提供视觉锚点；chart_type="auto" 选出
        散点且未指定 color 时，自动挑选一个低基数分类列（2~8 个取值）作为
        颜色维度（响应中 auto_color 字段回显）。
        """
        # LLM 偶发在 title 里输出 HTML 实体（如 p&lt;0.001），先还原成
        # 纯文本；模板层渲染时会统一再做一次 escape，不会引入 XSS，
        # 否则图表标题与产物描述会把转义残留直接展示给用户。
        if title:
            title = unescape(title)
        # 性能优化：只在确实需要修改数据时才 copy（聚合、布尔值本地化、
        # top_n 筛选）。对大 DataFrame（100K+ 行）避免无谓的深拷贝。
        # correlation_heatmap / scatter_matrix 等只读图表不需要 copy。
        _raw_df = workspace.dataframe
        requested = [value for value in [x, y, color, z, size, values] if value]
        requested.extend(path_columns or [])
        requested.extend(dimensions or [])
        # 先校验列存在性，确保后续自动选图能安全读取 df[x]/df[y]。
        _checked_columns(_raw_df, requested)

        # 自动选图：必须在“是否需要 copy”的判断之前解析出具体 chart_type，
        # 否则 needs_mutation 会因 chart_type=='auto' 误判为无需改动（bar/line/
        # area/pie/sunburst/treemap 需要 copy 做聚合/布尔本地化）。
        was_auto = chart_type == "auto"
        if was_auto:
            chart_type = _infer_chart_type(
                _raw_df,
                x=x,
                y=y,
                color=color,
                z=z,
                size=size,
                values=values,
                path_columns=path_columns,
                dimensions=dimensions,
                aggregation=aggregation,
                top_n=top_n,
            )
            # 纯分类计数场景（仅给 x、无 y）：自动用 count 聚合，避免空 series。
            if chart_type == "bar" and y is None and aggregation == "none":
                aggregation = "count"
            # 没有 color 分组、且 x 含重复类别（如 product×channel 但没传 color）
            # 时，柱图会出现重复类别导致错乱，自动按 x 求和聚合对齐到每类一行。
            # 注：aggregation 仅对 bar/line/area 生效，饼图的重复聚合在下方单独处理。
            if chart_type == "bar" and y is not None and aggregation == "none" and not color:
                non_null = _raw_df[x].dropna() if x else pd.Series(dtype=object)
                if len(non_null) > non_null.nunique():
                    aggregation = "sum"

        _auto_color_applied = False
        # 自动配色（Tableau 式默认行为）：auto 选出散点图且未指定 color 时，
        # 自动挑一个低基数分类列（2~8 个取值）作为颜色维度。大点云单一
        # 颜色毫无区分度（实测用户反馈"不能就一种颜色"），数据里有现成
        # 的分组维度时必须利用；显式传 chart_type/color 时不干预。
        if was_auto and chart_type == "scatter" and not color and not z and x and y:
            for candidate in _raw_df.columns:
                if candidate in (x, y):
                    continue
                candidate_col = _raw_df[candidate]
                if pd.api.types.is_numeric_dtype(
                    candidate_col
                ) or pd.api.types.is_datetime64_any_dtype(candidate_col):
                    continue
                try:
                    n_unique = int(candidate_col.nunique(dropna=True))
                except Exception:
                    continue
                if 2 <= n_unique <= 8:
                    color = candidate
                    _auto_color_applied = True
                    break

        _has_bool = any(
            col and col in _raw_df.columns and pd.api.types.is_bool_dtype(_raw_df[col])
            for col in (x, color)
        )
        needs_mutation = (
            aggregation != "none"
            or chart_type in {"bar", "line", "area", "pie", "sunburst", "treemap"}
            or top_n is not None
            or _has_bool
        )
        df = _raw_df.copy() if needs_mutation else _raw_df
        # 自动选图推断为饼图、且 x 含重复类别（如 product×channel 但没传 color）
        # 时，饼图生成器按原始行会产生同名扇区错乱；这里预先按 x 聚合求和，
        # 两引擎饼图均按单行绘制（ECharts 生成器还会再 groupby，幂等）。
        if was_auto and chart_type == "pie" and y is not None and not color:
            non_null = df[x].dropna()
            if len(non_null) > non_null.nunique():
                df = df.groupby(x, dropna=False)[y].sum().reset_index()
        # 意义性防护：拦截 ID 列分布图、常量列图、高基数不可读图表，
        # 报错文案带修正建议（换列/传 top_n/换图型）供模型下一轮自行纠正。
        _validate_chart_semantics(
            df,
            chart_type=chart_type,
            x=x,
            y=y,
            color=color,
            values=values,
            path_columns=path_columns,
            dimensions=dimensions,
            top_n=top_n,
        )
        if top_n is not None:
            if not x or not 1 <= top_n <= 500:
                raise ValueError("top_n 需要 x，且必须在 1 到 500 之间。")
            keep = df[x].value_counts(dropna=False).head(top_n).index
            df = df[df[x].isin(keep)].copy()

        groupable = chart_type in {"bar", "line", "area"}
        coverage: dict[str, Any] = {
            "complete": True,
            "observed_combinations": 0,
            "total_combinations": 0,
            "missing_combinations": [],
        }
        if aggregation != "none":
            if not groupable or not x:
                raise ValueError("aggregation 仅用于带 x 的 bar/line/area。")
            df, y, coverage = _aggregate_for_chart(
                df,
                x=x,
                y=y,
                color=color,
                aggregation=aggregation,
            )

        _localize_boolean_categories(df, [x, color])
        for item in coverage.get("missing_combinations", []):
            for column in (x, color):
                if column and item.get(column) in _BOOLEAN_VALUE_LABELS:
                    item[column] = _BOOLEAN_VALUE_LABELS[item[column]]
        if coverage.get("color_levels"):
            coverage["color_levels"] = [
                _BOOLEAN_VALUE_LABELS.get(value, value) for value in coverage["color_levels"]
            ]

        labels = {column: _human_column_label(str(column)) for column in df.columns}
        if y and aggregation in _AGGREGATION_LABELS:
            labels[y] = f"{_human_column_label(y)}（{_AGGREGATION_LABELS[aggregation]}）"
        # 文件名用"类型_序号"格式（如 柱状图_01），不再把 LLM 给的整段标题
        # 塞进文件名——以前会出现"客户评分按产品分布_ANOVA_p_0_0012_η²_..."
        # 这种看不懂的乱码文件名。display_title 才是 UI 上展示的人话标题。
        # 序号由 workspace 加锁原子分配：ToolNode 会并行执行同一轮的多个
        # create_visualization，若先读 count_artifacts 再拼名会竞态出重号，
        # 同类型图表算出相同文件名后写覆盖先写，导致会话历史里图表丢失。
        stem = _chart_filename_stem(chart_type, workspace.allocate_chart_index())
        display_title = _humanize_chart_title(title, chart_type)
        # === 双引擎分派（先于 Plotly fig 构建）：echarts 请求直接走独立渲染分支，
        # 避免白跑一次 Plotly 构建（scatter_3d 缺 z 时还会被 Plotly 校验误拦）===
        if chart_engine == "echarts":
            from data_agent.echarts_engine import _render_echarts

            return json_text(
                _render_echarts(
                    workspace,
                    df,
                    chart_type=chart_type,
                    x=x,
                    y=y,
                    color=color,
                    z=z,
                    size=size,
                    values=values,
                    path_columns=path_columns,
                    dimensions=dimensions,
                    aggregation=aggregation,
                    title=title,
                    bins=bins,
                    display_title=display_title,
                    stem=stem,
                    chart_type_source="auto" if was_auto else "explicit",
                    export_png=export_png,
                    stacked=stacked,
                )
            )
        # === Plotly 原有渲染逻辑（默认分支，保持不变）===
        # Plotly title 支持 HTML 子集，需 escape 防止 LLM 输出注入 <b>/<i> 等标签
        # 改变样式。与 ECharts 分支的 escape(title) 保持一致。
        from html import escape as _html_escape

        common = {
            "data_frame": df,
            "title": _html_escape(display_title),
            "template": "plotly_white",
            "labels": labels,
            "color_discrete_sequence": _CHART_COLORS,
        }
        # 大数据散点 → 密度视图（见 data_agent/density.py）。聚合后的散点行数
        # 通常很小，只有"每行一个点"的原始散点才会触发。
        density_view = None
        density_scale: dict[str, Any] | None = None
        if chart_type == "scatter" and x and y:
            density_view, density_scale = _density_view_for(
                df,
                x=x,
                y=y,
                color=color,
                scale_mode=scale_mode,
            )
        # 统计图（直方图/箱线图）服务端聚合的元信息：只有这两个分支会赋值，
        # 其余图型保持 None（下游据此决定是否追加说明与响应字段）。
        stat_agg: dict[str, Any] | None = None
        if chart_type == "bar":
            # 预计算 hover 文本：对无记录 bar（reindex 产生的空白组合）显示
            # "无样本/无记录"，而非让 Plotly 把 y=NaN 格式化成 "nan"。
            # 聚合后行数通常很少（x_levels × color_levels），iterrows 开销可忽略。
            if aggregation != "none" and _SAMPLE_COUNT_COLUMN in df.columns:
                x_label_hover = _human_column_label(x)
                y_label_hover = _human_column_label(y) if y else ""
                hover_lines: list[str] = []
                for _, row in df.iterrows():
                    if row[_HAS_RECORDS_COLUMN]:
                        lines = [f"{x_label_hover}：{row[x]}"]
                        if y:
                            y_val = row[y]
                            y_text = f"{float(y_val):,.2f}" if pd.notna(y_val) else "—"
                            lines.append(f"{y_label_hover}：{y_text}")
                        lines.append(f"样本数：{int(row[_SAMPLE_COUNT_COLUMN])}")
                        hover_lines.append("<br>".join(lines))
                    else:
                        missing_label = (
                            "无样本"
                            if aggregation in {"mean", "median", "min", "max"}
                            else "无记录"
                        )
                        hover_lines.append(f"{x_label_hover}：{row[x]}<br>{missing_label}")
                df[_HOVER_TEXT_COLUMN] = hover_lines
                custom_data = [_SAMPLE_COUNT_COLUMN, _HAS_RECORDS_COLUMN, _HOVER_TEXT_COLUMN]
            else:
                custom_data = None
            fig = px.bar(**common, x=x, y=y, color=color, barmode="group", custom_data=custom_data)
        elif chart_type == "line":
            fig = px.line(**common, x=x, y=y, color=color, markers=True)
        elif chart_type == "area":
            fig = px.area(**common, x=x, y=y, color=color)
        elif chart_type == "scatter":
            if density_view is not None:
                fig = density_figure(
                    df,
                    x=x,
                    y=y,
                    color=color,
                    view=density_view,
                    x_label=labels.get(x, _human_column_label(x)),
                    y_label=labels.get(y, _human_column_label(y)),
                    title=common["title"],
                    colors=_CHART_COLORS,
                )
            else:
                fig = px.scatter(**common, x=x, y=y, color=color, size=size, trendline=None)
        elif chart_type == "scatter_3d":
            if not x or not y or not z:
                raise ValueError("scatter_3d 需要 x、y、z。")
            fig = px.scatter_3d(**common, x=x, y=y, z=z, color=color, size=size)
            _refine_scatter_3d(fig, rows=len(df))
        elif chart_type == "histogram":
            if len(df) > STAT_AGG_THRESHOLD:
                # 大数据：服务端分箱，纵轴仍是真实记录数（不是抽样估计）
                binned = _plotly_binned_histogram(
                    df,
                    x=x,
                    bins=bins,
                    colors=_CHART_COLORS,
                    color=color,
                    labels=labels,
                    title=common["title"],
                )
                if binned is not None:
                    fig, stat_agg = binned
                    stat_agg["kind"] = "histogram"
            if stat_agg is None:
                fig = px.histogram(
                    **common, x=x, color=color, nbins=max(2, min(bins, 200)), marginal="box"
                )
        elif chart_type == "box":
            if len(df) > STAT_AGG_THRESHOLD and y:
                stats_fig = _plotly_box_from_stats(
                    df,
                    x=x,
                    y=y,
                    colors=_CHART_COLORS,
                    color=color,
                    labels=labels,
                    title=common["title"],
                )
                if stats_fig is not None:
                    fig, stat_agg = stats_fig
                    stat_agg["kind"] = "box"
            if stat_agg is None:
                fig = px.box(**common, x=x, y=y, color=color, points="outliers")
        elif chart_type == "violin":
            violin_common = common
            if len(df) > STAT_AGG_THRESHOLD:
                # 小提琴的形状由核密度决定，分层抽样两万行与全量肉眼无差别；
                # 逐样本点云才是 MB 级载荷的来源。
                violin_common = {**common, "data_frame": _stratified_sample(df, color)}
            fig = px.violin(**violin_common, x=x, y=y, color=color, box=True, points="outliers")
        elif chart_type == "pie":
            if not x:
                raise ValueError("pie 需要 x 作为名称列。")
            fig = px.pie(**common, names=x, values=values or y, hole=0.42)
        elif chart_type == "heatmap":
            if not x or not y or not values:
                raise ValueError("heatmap 需要 x、y、values。")
            pivot = df.pivot_table(index=y, columns=x, values=values, aggfunc="mean")
            fig = px.imshow(
                pivot, text_auto=True, aspect="auto", title=title, color_continuous_scale="RdBu_r"
            )
        elif chart_type == "correlation_heatmap":
            numeric = _numeric_columns(df, dimensions)
            corr = df[numeric].corr()
            fig = px.imshow(
                corr,
                text_auto=".2f",
                aspect="auto",
                title=title or "Correlation heatmap",
                color_continuous_scale="RdBu_r",
                zmin=-1,
                zmax=1,
            )
        elif chart_type == "scatter_matrix":
            numeric = _numeric_columns(df, dimensions)
            if len(numeric) > _SCATTER_MATRIX_MAX_DIMENSIONS:
                raise ValueError(
                    f"scatter_matrix 最多支持 {_SCATTER_MATRIX_MAX_DIMENSIONS} 个维度，请缩小 dimensions。"
                )
            # SPLOM 降采样：N 维 × M 行产生 N² 个子图，每个 trace 含全量 M 行。
            # 8 维 10 万行 = 64 个 trace × 10 万点 = 640 万点负载，浏览器会卡死。
            # 与 ECharts SPLOM 的 400 行抽样保持一致，超过则均匀抽样。
            _SPLOM_MAX_ROWS = 1000
            spom_df = df
            if len(df) > _SPLOM_MAX_ROWS:
                spom_df = df.sample(n=_SPLOM_MAX_ROWS, random_state=42)
                spom_common = {**common, "data_frame": spom_df}
            else:
                spom_common = common
            fig = px.scatter_matrix(**spom_common, dimensions=numeric, color=color)
            fig.update_traces(diagonal_visible=False)
        elif chart_type == "sunburst":
            if not path_columns:
                raise ValueError("sunburst 需要 path_columns。")
            fig = px.sunburst(**common, path=path_columns, values=values)
        else:
            if not path_columns:
                raise ValueError("treemap 需要 path_columns。")
            fig = px.treemap(**common, path=path_columns, values=values, color=color)

        if density_view is not None:
            # 密度图：视口范围在聚合前已按"主体尺度"确定，网格只覆盖该范围，
            # 不能再交给 _apply_outlier_scale_controls 追加"全量视图"按钮
            # （网格没有覆盖范围外的数据，切过去只会看到角落一小块）。
            scale_details = density_scale
        else:
            scale_details = _apply_outlier_scale_controls(fig, chart_type, scale_mode)
            # 大数据切换 WebGL：SVG 下 30 万行散点/折线的完整预览渲染以分钟计
            fig = _plotly_webgl_if_large(fig, df, chart_type)
        if chart_type == "bar" and aggregation != "none":
            _add_missing_combination_markers(
                fig,
                coverage,
                x=x,
                color=color,
                aggregation=aggregation,
            )
        # 自动标注关键统计量（最大值/最小值），仅用于柱状图和折线图
        if chart_type in {"bar", "line"}:
            _annotate_extreme_values(fig)
        fig.update_layout(
            font={"family": "IBM Plex Sans, Noto Sans SC, sans-serif", "color": "#102a2a"},
            paper_bgcolor="#fbfaf5",
            plot_bgcolor="#fbfaf5",
            colorway=_CHART_COLORS,
            margin=(
                # 3D 场景本身是正方形；通用页边距会在四周留下大片空白。
                {"l": 0, "r": 0, "t": 56, "b": 70}
                if chart_type == "scatter_3d"
                else {
                    "l": 64,
                    "r": 36,
                    "t": 108
                    if scale_details["scale_mode"] == "robust" or coverage.get("missing_combinations")
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
        # === nice ticks：对数值轴应用圆数刻度（1/2/5/10 倍数）===
        # 仅对 value 类型的轴生效，category/time 轴跳过。
        # 若 _apply_outlier_scale_controls 已设置范围，优先用其范围计算 nice ticks。
        _apply_plotly_nice_ticks(fig, chart_type, x, y, scale_details)
        if chart_type == "bar":
            fig.update_traces(
                marker_line_color="rgba(16,42,42,0.16)",
                marker_line_width=0.8,
                selector={"type": "bar"},
            )
            if aggregation != "none" and _HOVER_TEXT_COLUMN in df.columns:
                # 用预计算的 hover 文本（customdata[2]），避免无记录 bar 显示 nan。
                # customdata 结构：[sample_count, has_records, hover_text]
                fig.update_traces(
                    hovertemplate="%{customdata[2]}<extra>%{fullData.name}</extra>",
                    selector={"type": "bar"},
                )
            if x and not pd.api.types.is_numeric_dtype(df[x]):
                fig.update_xaxes(categoryorder="total descending")
            # 堆叠柱（stacked=True）与分组柱：Plotly 用 barmode 表达，
            # 与 ECharts 分支的 series.stack 语义一致。
            fig.update_layout(
                bargap=0.26, bargroupgap=0.08, barmode="stack" if stacked else "group"
            )
        elif chart_type == "scatter" and density_view is None:
            # 点径与透明度随点数分档（与 ECharts 分支同一张表，见 scatter_point_style）：
            # 固定 9px/0.82 在几千点以上就会糊成一团实心色块，分组色互相覆盖，
            # 看起来"只有一个颜色"；分档缩小并降低透明度后，密集区自然混色、
            # 稀疏区与放大后仍能分辨单点。
            marker_style = scatter_point_style(len(df))
            fig.update_traces(marker=marker_style, selector={"type": "scatter"})
            fig.update_traces(marker=marker_style, selector={"type": "scattergl"})
            # 结构注记（Tableau 式视觉锚点）：OLS 趋势线 + 双轴均值象限线。
            # 大点云没有锚点就是一堵"点墙"（实测用户反馈眼花缭乱）。
            # 趋势线用 SVG Scatter 追加（与 scattergl 混合渲染，线在点上层
            # 保持清晰）；坐标基于全量数据，比嵌入抽样更精确。
            if x and y and x in df.columns and y in df.columns:
                pair_df = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
                structure = (
                    # 按位置取列：x 与 y 同名时 pair_df[x] 是两列的 DataFrame
                    _scatter_structure(
                        pair_df.iloc[:, 0].astype(float).tolist(),
                        pair_df.iloc[:, 1].astype(float).tolist(),
                    )
                    if len(pair_df) >= 3
                    else None
                )
                if structure:
                    import plotly.graph_objects as go

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
        if color:
            fig.update_layout(legend_title_text=_human_column_label(color))
        if density_view is not None:
            # 密度图收尾：清掉边缘直方图的网格/刻度，并为档位色标留出右边距
            # （必须在上面通用布局样式之后调用，否则会被覆盖）。
            finalize_density_layout(fig, density_view)
        # 空图兜底：所有序列都没有数据点时直接报错让模型重试，不产出
        # "能打开但一片空白"的 HTML（轴写反/聚合列全空时曾静默出空白图）。
        _ensure_plotly_fig_has_data(fig, chart_type)
        html_path = workspace.artifacts_dir / f"{stem}.html"
        shared_plotly = workspace.ensure_plotly_bundle()
        relative_script = (
            shared_plotly.relative_to(workspace.artifacts_dir).as_posix() if shared_plotly else None
        )
        # Plotly 白话解读：与 ECharts _auto_interpret 语义对齐。密度图的解读
        # 换了编码语义（颜色读记录数、没有可框选的离散点），单独生成。
        if density_view is not None:
            interpretation = density_interpretation(
                df,
                view=density_view,
                x=x,
                y=y,
                color=color,
                title=display_title,
                x_label=labels.get(x, _human_column_label(x)),
                y_label=labels.get(y, _human_column_label(y)),
            )
            if size:
                # 密度视图用颜色表达每格记录数，逐点大小这个编码通道没有位置可放；
                # 与其静默丢掉用户要求的维度，不如把这件事说清楚。
                interpretation += (
                    f"\n\n说明：密度视图按记录数着色，因此未使用「{_human_column_label(size)}」"
                    "作为点的大小——几十万点上逐点大小无法辨认；该列仍保留在数据与 JSON 产物中。"
                )
        else:
            interpretation = _plotly_auto_interpret(
                df,
                chart_type=chart_type,
                x=x,
                y=y,
                color=color,
                aggregation=aggregation,
                title=display_title,
            )
            if stat_agg is not None:
                interpretation += _stat_agg_note(stat_agg)
            if (
                chart_type == "scatter"
                and density_view is None
                and x
                and y
                and should_use_density(len(df))
            ):
                # 密度视图只适用于"数值 × 数值"（阈值与密度视图一致：本该走
                # 密度的那批数据，因为横轴是类别而退回逐点渲染）。这里不擅自
                # 换图型（用户明确要的是散点），但必须说清楚并给出建议。
                interpretation += (
                    "\n\n提示：这张散点的横轴不是数值列，无法聚合成密度网格，"
                    "几十万个点会互相覆盖成一团深色。查看各类别的分布建议改用"
                    "箱线图 / 小提琴图，或把数值列放到横轴上重新生成（会自动切换密度视图）。"
                )
        # 全量 figure 序列化一次复用：完整数据写入 .plotly.json 产物，
        # 大数据时也作为嵌入降采样的输入源。抽样决策必须在解读块构建
        # 之前完成——抽样声明要追加进解读文本一起渲染。
        fig_json_text = fig.to_json()
        html_fig = fig
        sampling_info: dict[str, Any] | None = None
        # 大数据 HTML 嵌入降采样：散点/折线/3D 散点超过嵌入上限时，交互 HTML
        # 按抽样渲染（5 万点以上视觉上已是密度饱和，HTML 体积、传输、iframe
        # 解析、tab 内存却随点数线性增长）。完整数据不丢：全量 figure 仍写入
        # .plotly.json 产物。3D 散点（scatter3d 走 gl3d）此前不在名单里，
        # 30 万行时 HTML 要带 90 万个数字，是个漏掉的口子。
        if (
            chart_type in {"scatter", "scatter_3d", "line"}
            and len(df) > _EMBED_MAX_POINTS
            and density_view is None
        ):
            sampled_dict, original_pts, embedded_pts = sample_plotly_figure_for_embed(
                json.loads(fig_json_text), _EMBED_MAX_POINTS
            )
            if embedded_pts < original_pts:
                import plotly.io as pio

                html_fig = pio.from_json(json.dumps(sampled_dict))
                interpretation += sampling_note("plotly", original_pts, embedded_pts)
                sampling_info = {
                    "applied": True,
                    "original_points": original_pts,
                    "embedded_points": embedded_pts,
                    "full_data_json": str(workspace.artifacts_dir / f"{stem}.plotly.json"),
                }
        # 3D 散点的可访问数据摘要：取抽样后真正写进 HTML 的 figure 点，
        # 保证表格数值与画面一致（Gemini 审计第 5 条：读屏/无法交互场景
        # 也必须能拿到数据）。points 即嵌入点数，sampled 标记抽样事实。
        data_summary: dict[str, Any] | None = None
        if chart_type == "scatter_3d" and x and y and z:
            data_summary = scatter3d_data_summary(
                html_fig,
                x=x,
                y=y,
                z=z,
                color=color,
                sampled=sampling_info is not None,
            )
        data_table = render_data_table_html(data_summary)
        interpretation_block = ""
        if interpretation:
            interpretation_block = (
                '<div class="plotly-interpretation">'
                '<div class="plotly-interpretation-title">数据解读</div>'
                f"{escape(interpretation)}"
                "</div>"
            )
        if relative_script is not None:
            div = html_fig.to_html(
                full_html=False,
                include_plotlyjs=False,
                default_width="100%",
                default_height="100%",
                config={
                    "responsive": True,
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                    # 滚轮缩放：预览模态是独立 iframe，页面本身不滚动，
                    # 滚轮交给图表做缩放是用户预期（此前滚轮无效）。
                    "scrollZoom": True,
                },
            )
            # XSS 纵深防御：Plotly 的 JSON 编码器会把数据中的 < 转义为
            # \u003c，正常情况 div 中只有 to_html 自身脚本块的那一个
            # </script>。但若未来版本或数据路径出现未转义的 </script>
            # （浏览器解析时它会提前关闭 script 块、剩余 JS 在 iframe
            # 上下文执行），这里统一转义 <\/script> 兜底。
            # 注意：必须保留最后一个 </script> —— 它是 to_html 自身脚本
            # 块的闭合标签。若一并转义（旧版 bug），该 script 元素无法
            # 闭合，会与后续注入的暗色脚本合并成同一 script 块（内含
            # 字面 "<script>"），产生 "Unexpected token '<'" 语法错误，
            # 图表预览直接空白。数据中的 </script> 均位于闭合标签之前，
            # 因此"转义全部但保留最后一个"同时满足防护与结构正确。
            close_idx = div.rfind("</script>")
            if close_idx != -1:
                div = div[:close_idx].replace("</script>", "<\\/script>") + div[close_idx:]
            # 原子写：进程被 kill / 磁盘满时不会留下半截 HTML 让预览 iframe
            # 加载到损坏页面。tmp + os.replace 对同目录文件是原子的。
            _atomic_write_text(
                html_path,
                _render_plotly_html(
                    title=escape(display_title),
                    script_src=relative_script,
                    div=div,
                    interpretation_block=interpretation_block,
                    data_table=data_table,
                    dark_script=_PLOTLY_DARK_MODE_SCRIPT,
                ),
            )
        else:
            # 内联 Plotly 的离线分支同样走 tmp + replace：进程中断时不会
            # 留下半截 HTML 让预览 iframe 加载到损坏页面。write_html 直接
            # 产出完整文档，数据表在 </body> 前补入（与共享模板同一段 HTML）。
            temp_html = html_path.with_name(html_path.name + ".tmp")
            try:
                fig.write_html(temp_html, include_plotlyjs=True, full_html=True)
                inline_html = temp_html.read_text(encoding="utf-8")
                _atomic_write_text(
                    html_path, append_html_body_block(inline_html, data_table)
                )
            finally:
                temp_html.unlink(missing_ok=True)
        workspace.register_artifact(html_path, "visualization", display_title)
        json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
        # plotly write_json 不带 encoding 时在 Windows 上按 locale(cp936/GBK) 写文件，
        # 必须显式 UTF-8，否则后续 UTF-8 读取会 UnicodeDecodeError。
        # fig_json_text 在 div 生成前已序列化（完整 figure，非抽样副本）。
        _atomic_write_text(json_path, fig_json_text)
        workspace.register_artifact(json_path, "chart_data", "Plotly figure JSON")

        response: dict[str, Any] = {
            "status": "ok",
            "chart_type": chart_type,
            "chart_type_source": "auto" if was_auto else "explicit",
            "rows_plotted": len(df),
            **scale_details,
            "category_coverage": {
                "complete": coverage["complete"],
                "observed_combinations": coverage["observed_combinations"],
                "total_combinations": coverage["total_combinations"],
                "missing_count": len(coverage.get("missing_combinations", [])),
            },
            "html": html_path,
            "plotly_json": json_path,
        }
        if data_summary is not None:
            # 读屏/无交互场景的数据兜底：columns + 前 20 行实际绘图值 +
            # 嵌入点数与抽样标记（与 HTML 里的折叠数据表同源）。
            response["data_summary"] = data_summary
        # 图表与来源绑定：导出物可追溯到那一版 CSV，而不是只靠会话目录。
        if workspace.source_path and workspace.source_path.is_file():
            from data_agent.provenance import sha256_file

            response["source"] = {
                "filename": workspace.source_path.name,
                "sha256": sha256_file(workspace.source_path),
            }
        if sampling_info is not None:
            response["sampling"] = sampling_info
        if density_view is not None:
            # 渲染方式对调用方（Agent/前端）可见：密度视图不是抽样，是精确
            # 聚合，因此既没有 sampling 字段，也不需要"完整数据在 JSON 里"
            # 的免责声明；网格覆盖范围与面板构成在这里说清楚。
            response["density_view"] = {
                "applied": True,
                "bins": density_view.bin_label,
                "panels": [panel.name for panel in density_view.panels],
                "bands": [band_label(band) for band in density_view.bands],
                "rows_used": density_view.rows_used,
                "rows_outside_view": density_view.dropped_rows,
                "full_data_json": str(workspace.artifacts_dir / f"{stem}.plotly.json"),
            }
        if stat_agg is not None:
            response["stat_aggregation"] = {
                "kind": stat_agg.get("kind"),
                "rows_aggregated": int(stat_agg.get("total", 0)),
            }
            if stat_agg.get("kind") == "box":
                response["stat_aggregation"]["outliers"] = int(stat_agg.get("outliers", 0))
            else:
                response["stat_aggregation"]["bins"] = int(stat_agg.get("bins", 0))
        if _auto_color_applied and color:
            response["auto_color"] = color
        if export_png:
            png_path = workspace.artifacts_dir / f"{stem}.png"
            try:
                # 惰性安装 kaleido 1.5.1 的就绪等待补丁（绕 kaleido_scopes 竞态）；
                # 不在模块顶部装，避免导入时就加载 kaleido。
                _kaleido_compat.install()
                png_width = 980 if chart_type == "scatter_3d" else 1400
                fig.write_image(png_path, width=png_width, height=850, scale=1.5)
                workspace.register_artifact(png_path, "image", title or chart_type)
                response["png"] = png_path
            except Exception as exc:
                # 区分"服务器环境缺失"（没装 kaleido / 没有 Chrome）与"渲染失败"：
                # 前者给出可操作的安装提示，后者保留原始报错便于排查。
                hint = _kaleido_compat.render_env_problem(exc)
                if hint is not None:
                    response["png_warning"] = (
                        f"PNG 导出不可用（环境缺失），但 HTML 图表已生成：{hint}"
                    )
                else:
                    response["png_warning"] = f"PNG 渲染失败，但 HTML 图表已生成：{exc}"
        return json_text(response)

    return [create_visualization]
