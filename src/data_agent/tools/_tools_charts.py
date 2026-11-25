"""可视化工具：``create_visualization`` 与 Plotly nice ticks 辅助。

从 ``builder.build_tools`` 拆出的图表工具定义。``create_visualization`` 本体只
保留参数签名、工具文档与编排步骤；具体实现按阶段拆到 ``_charts`` 子包：

- ``_charts.spec``: 列校验、自动选图/自动配色、数据准备（聚合/top_n）、轴标签；
- ``_charts.render``: Plotly figure 构建、尺度控制、底版布局与 trace 样式；
- ``_charts.deliver``: 白话解读、嵌入降采样、HTML/JSON 原子写入与响应组装。

双引擎分派、聚合、布尔本地化、top_n、抽样降采样、密度视图与 PNG 导出逻辑均
保持原实现不变；``_apply_plotly_nice_ticks`` 仅被本工具的 Plotly 分支使用。
"""

from __future__ import annotations

from html import unescape
from typing import Any, Literal

import plotly.express as px
from langchain_core.tools import BaseTool, tool

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace

from ._charts.deliver import (
    build_response,
    dispatch_echarts,
    finalize_plotly_artifacts,
)
from ._charts.render import (
    apply_base_layout,
    apply_scale_and_overlays,
    apply_trace_style,
    build_figure,
)
from ._charts.spec import ChartSpec, prepare_chart_spec
from ._helpers import _nice_ticks, _plotly_axis_tickformat
from ._plotly_render import _collect_trace_values
from .charts import _chart_filename_stem, _humanize_chart_title


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
        raw_df = workspace.dataframe
        spec = ChartSpec(
            chart_type=chart_type,
            df=raw_df,
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
            chart_engine=chart_engine,
            bins=bins,
            top_n=top_n,
            scale_mode=scale_mode,
            export_png=export_png,
            stacked=stacked,
        )
        # 列校验 → 解析 auto 图型 → 按需 copy/聚合 → 布尔本地化 → 轴标签。
        prepare_chart_spec(raw_df, spec)
        # 文件名用"类型_序号"格式（如 柱状图_01），不再把 LLM 给的整段标题
        # 塞进文件名——以前会出现"客户评分按产品分布_ANOVA_p_0_0012_η²_..."
        # 这种看不懂的乱码文件名。display_title 才是 UI 上展示的人话标题。
        # 序号由 workspace 加锁原子分配：ToolNode 会并行执行同一轮的多个
        # create_visualization，若先读 count_artifacts 再拼名会竞态出重号，
        # 同类型图表算出相同文件名后写覆盖先写，导致会话历史里图表丢失。
        spec.stem = _chart_filename_stem(spec.chart_type, workspace.allocate_chart_index())
        spec.display_title = _humanize_chart_title(title, spec.chart_type)
        # === 双引擎分派（先于 Plotly fig 构建）：echarts 请求直接走独立渲染分支，
        # 避免白跑一次 Plotly 构建（scatter_3d 缺 z 时还会被 Plotly 校验误拦）===
        if chart_engine == "echarts":
            return dispatch_echarts(workspace, spec)
        # === Plotly 原有渲染逻辑（默认分支，保持不变）===
        fig = build_figure(spec)
        fig, scale_details = apply_scale_and_overlays(fig, spec)
        apply_base_layout(fig, spec, scale_details)
        # === nice ticks：对数值轴应用圆数刻度（1/2/5/10 倍数）===
        # 仅对 value 类型的轴生效，category/time 轴跳过。
        # 若 _apply_outlier_scale_controls 已设置范围，优先用其范围计算 nice ticks。
        _apply_plotly_nice_ticks(fig, spec.chart_type, spec.x, spec.y, scale_details)
        apply_trace_style(fig, spec)
        artifacts = finalize_plotly_artifacts(fig, spec, workspace)
        return json_text(build_response(spec, workspace, artifacts, scale_details, fig))

    return [create_visualization]
