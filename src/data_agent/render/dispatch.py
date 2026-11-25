"""ECharts 分派与编排：chart_type → 各图表族生成器，渲染产物落盘。

调用入口 ``_render_echarts`` 的返回结构与 Plotly 分支保持一致；
``_build_echarts_option`` 可脱离 workspace 单独生成 option。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from data_agent.density import DensityView, band_label
from data_agent.render.density_option import _echarts_scatter_density
from data_agent.render.density_view import _density_view_for
from data_agent.render.output import _build_echarts_html, _json_default
from data_agent.render.series_3d import _echarts_scatter3d, _scatter3d_data_summary
from data_agent.render.series_cartesian import _echarts_bar, _echarts_line
from data_agent.render.series_distribution import (
    _echarts_box,
    _echarts_heatmap,
    _echarts_histogram,
    _echarts_pie,
)
from data_agent.render.series_hierarchy import _echarts_sunburst
from data_agent.render.series_matrix import _echarts_scatter_matrix
from data_agent.render.series_scatter import _echarts_scatter
from data_agent.workspace import (
    ECHARTS_CDN_URL,
    ECHARTS_GL_CDN_URL,
    DataWorkspace,
    _atomic_write_text,
)


def _build_echarts_option(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    z: str | None,
    size: str | None,
    values: str | None,
    path_columns: list[str] | None,
    dimensions: list[str] | None,
    aggregation: str,
    title: str,
    bins: int,
    density_view: DensityView | None = None,
    scale_mode: str = "auto",
    stacked: bool = False,
) -> dict[str, Any]:
    """根据 chart_type 分派到对应的 ECharts option 生成器。

    ``density_view`` 由 ``_render_echarts`` 预先算好（解读文案也要用同一份
    视图，避免重复聚合）；直接调用本函数时按同一条件自行判定，保证
    "超大数据散点走密度视图"这一分派在两条入口上完全一致。
    """
    if chart_type == "bar":
        return _echarts_bar(
            df, x=x, y=y, color=color, aggregation=aggregation, title=title, stacked=stacked
        )
    if chart_type == "line":
        return _echarts_line(
            df, x=x, y=y, color=color, aggregation=aggregation, title=title, area=False
        )
    if chart_type == "area":
        return _echarts_line(
            df, x=x, y=y, color=color, aggregation=aggregation, title=title, area=True
        )
    if chart_type == "scatter":
        view = density_view
        if view is None:
            view, _ = _density_view_for(
                df, chart_type=chart_type, x=x, y=y, color=color, scale_mode=scale_mode
            )
        if view is not None:
            return _echarts_scatter_density(df, x=x, y=y, color=color, title=title, view=view)
        return _echarts_scatter(df, x=x, y=y, color=color, size=size, title=title)
    if chart_type == "scatter_3d":
        if x and y and z and z in df.columns and pd.api.types.is_numeric_dtype(df[z]):
            return _echarts_scatter3d(df, x=x, y=y, z=z, color=color, size=size, title=title)
        # 缺 z 轴或 z 非数值时无法构建 3D 坐标系，降级为 2D 散点并标注
        opt = _echarts_scatter(df, x=x, y=y, color=color, size=size, title=title + "（2D 视图）")
        opt["title"]["subtext"] = "缺少数值型 z 轴，3D 散点降级为 2D"
        return opt
    if chart_type == "histogram":
        return _echarts_histogram(df, x=x, color=color, bins=bins, title=title)
    if chart_type == "box":
        return _echarts_box(df, x=x, y=y, color=color, title=title, violin=False)
    if chart_type == "violin":
        # ECharts 无原生 violin，用 boxplot 近似
        opt = _echarts_box(df, x=x, y=y, color=color, title=title, violin=True)
        opt["title"]["subtext"] = "小提琴图用箱线图近似（ECharts 无原生 violin）"
        return opt
    if chart_type == "pie":
        return _echarts_pie(df, x=x, values=values, y=y, title=title)
    if chart_type == "heatmap":
        return _echarts_heatmap(df, x=x, y=y, values=values, title=title, is_correlation=False)
    if chart_type == "correlation_heatmap":
        return _echarts_heatmap(
            df, x=x, y=y, values=values, title=title or "相关性矩阵", is_correlation=True
        )
    if chart_type == "scatter_matrix":
        return _echarts_scatter_matrix(df, dimensions=dimensions or [], color=color, title=title)
    if chart_type == "sunburst":
        return _echarts_sunburst(
            df, path_columns=path_columns or [], values=values, title=title, is_treemap=False
        )
    if chart_type == "treemap":
        return _echarts_sunburst(
            df, path_columns=path_columns or [], values=values, title=title, is_treemap=True
        )
    raise ValueError(f"ECharts 引擎暂不支持图表类型：{chart_type}")


def _embed_option_for_html(
    option: dict[str, Any],
    *,
    df: pd.DataFrame,
    chart_type: str,
    density_view: DensityView | None,
    workspace: DataWorkspace,
    stem: str,
    interpretation: str,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """大数据 HTML 嵌入降采样：返回 ``(html_option, 追加后的解读, sampling 信息)``。

    # 大数据 HTML 嵌入降采样：散点/折线超过嵌入上限时，交互 HTML 按等距
    # 抽样渲染（5 万点以上视觉密度已饱和，HTML 体积/传输/解析/内存随点数
    # 线性增长——30 万行 ECharts 散点 HTML 约 12MB）。完整 option 不丢：
    # .echarts.json 产物仍写全量数据（也是损坏恢复链的数据源）。
    # 密度图跳过降采样：它已经没有逐点数据（聚合后只有一万多个格子），
    # 抽样器只会按类目轴长度误判成"抽样了 154 个点"并破坏热力图。
    """
    from data_agent.chart_sampling import (
        _EMBED_MAX_POINTS,
        sample_echarts_option_for_embed,
        sampling_note,
    )

    html_option = option
    sampling_info: dict[str, Any] | None = None
    if (
        density_view is None
        and chart_type in {"scatter", "scatter_3d", "line", "area"}
        and len(df) > _EMBED_MAX_POINTS
    ):
        html_option, original_pts, embedded_pts = sample_echarts_option_for_embed(
            option, _EMBED_MAX_POINTS
        )
        if embedded_pts < original_pts:
            note = sampling_note("echarts", original_pts, embedded_pts)
            interpretation += note
            sampling_info = {
                "applied": True,
                "original_points": original_pts,
                "embedded_points": embedded_pts,
                "full_data_json": str(workspace.artifacts_dir / f"{stem}.echarts.json"),
            }
    return html_option, interpretation, sampling_info


def _scatter3d_table(
    option: dict[str, Any],
    *,
    df: pd.DataFrame,
    chart_type: str,
    x: str | None,
    y: str | None,
    z: str | None,
    color: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """3D 散点的可访问数据摘要与折叠表格（其它图型返回 ``(None, "")``）。

    # 3D 散点的可访问数据摘要：从实际写入 HTML 的 option 读取（内部 3000
    # 点上限与嵌入抽样都已生效），表格数值与画面一致。
    """
    if chart_type == "scatter_3d" and x and y and z:
        from data_agent.tools._plotly_render import render_data_table_html

        data_summary = _scatter3d_data_summary(
            option, x=x, y=y, z=z, color=color, total_rows=len(df)
        )
        return data_summary, render_data_table_html(data_summary)
    return None, ""


def _resolve_script_sources(
    workspace: DataWorkspace,
    option: dict[str, Any],
) -> tuple[str, str | None]:
    """解析 echarts / echarts-gl bundle 的 script src（缺失时回退 CDN）。"""
    # 获取 echarts bundle（首次从 CDN 下载，失败时用 CDN URL 直引）
    bundle = workspace.ensure_echarts_bundle()
    if bundle is not None:
        relative_script = bundle.relative_to(workspace.artifacts_dir).as_posix()
    else:
        # 离线 fallback：直接引用 CDN（在线场景可用）
        relative_script = ECHARTS_CDN_URL

    # 3D 系列需要 echarts-gl 扩展 bundle，按需下载 / CDN 直引
    extra_script_src: str | None = None
    if any(s.get("type") == "scatter3D" for s in option.get("series", [])):
        gl_bundle = workspace.ensure_echarts_gl_bundle()
        if gl_bundle is not None:
            extra_script_src = gl_bundle.relative_to(workspace.artifacts_dir).as_posix()
        else:
            extra_script_src = ECHARTS_GL_CDN_URL
    return relative_script, extra_script_src


def _write_echarts_artifacts(
    workspace: DataWorkspace,
    *,
    stem: str,
    display_title: str,
    html_content: str,
    option: dict[str, Any],
) -> tuple[Path, Path]:
    """写入 HTML 与全量 option JSON，并注册为产物。"""
    html_path = workspace.artifacts_dir / f"{stem}.html"
    _atomic_write_text(html_path, html_content)
    workspace.register_artifact(html_path, "visualization", display_title)

    # ECharts option JSON：供前端动态渲染 / 调试。始终写全量 option
    # （HTML 可能是抽样副本），损坏恢复链依赖这里的完整数据。
    json_path = workspace.artifacts_dir / f"{stem}.echarts.json"
    _atomic_write_text(json_path, json.dumps(option, ensure_ascii=False, default=_json_default))
    workspace.register_artifact(json_path, "chart_data", "ECharts option JSON")
    return html_path, json_path


def _prepare_echarts_option(
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    z: str | None,
    size: str | None,
    values: str | None,
    path_columns: list[str] | None,
    dimensions: list[str] | None,
    aggregation: str,
    display_title: str,
    bins: int,
    scale_mode: str,
    stacked: bool,
) -> tuple[dict[str, Any], DensityView | None, dict[str, Any] | None]:
    """聚合密度视图 → 生成 option → 空图兜底；返回 ``(option, view, scale_details)``。"""
    # 超大数据散点先聚合成分面密度视图：option 与解读文案共用同一份聚合结果
    # （重复聚合 30 万行是白跑）。scale_mode="auto" 时对极端值给"主体尺度"。
    density_view, density_scale = _density_view_for(
        df,
        chart_type=chart_type,
        x=x,
        y=y,
        color=color,
        scale_mode=scale_mode,
    )

    # 生成 ECharts option
    option = _build_echarts_option(
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
        title=display_title,
        bins=bins,
        density_view=density_view,
        scale_mode=scale_mode,
        stacked=stacked,
    )

    # 空图兜底：所有 series 都没有数据时直接报错让模型重试，不产出
    # "能打开但一片空白"的 HTML（轴写反/聚合列全空时曾静默出空白图）。
    from data_agent.tools._charts.validate import _ensure_echarts_series_has_data

    _ensure_echarts_series_has_data(option, chart_type)

    return (
        option,
        density_view,
        density_scale,
    )


def _render_echarts(
    workspace: DataWorkspace,
    df: pd.DataFrame,
    *,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    z: str | None,
    size: str | None,
    values: str | None,
    path_columns: list[str] | None,
    dimensions: list[str] | None,
    aggregation: str,
    title: str | None,
    bins: int,
    display_title: str,
    stem: str,
    chart_type_source: str = "explicit",
    scale_mode: str = "auto",
    stacked: bool = False,
    export_png: bool = False,
) -> dict[str, Any]:
    """ECharts 渲染主入口：生成 option、HTML、解读文本，返回 response dict。"""
    option, density_view, density_scale = _prepare_echarts_option(
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
        display_title=display_title,
        bins=bins,
        scale_mode=scale_mode,
        stacked=stacked,
    )

    # 自动白话解读（密度图走密度编码说明，见 _density_interpretation）。
    # 延迟从门面模块取：``_auto_interpret`` 定义在 ``echarts_engine``（其
    # 异常兜底与可替换的 ``_interpret_impl`` 是既有测试契约），此处复用同一
    # 实现，避免两份行为分叉。
    from data_agent.echarts_engine import _auto_interpret

    interpretation = _auto_interpret(
        df,
        chart_type=chart_type,
        x=x,
        y=y,
        color=color,
        aggregation=aggregation,
        title=display_title,
        density_view=density_view,
        stacked=stacked,
    )

    html_option, interpretation, sampling_info = _embed_option_for_html(
        option,
        df=df,
        chart_type=chart_type,
        density_view=density_view,
        workspace=workspace,
        stem=stem,
        interpretation=interpretation,
    )
    data_summary, data_table = _scatter3d_table(
        html_option,
        df=df,
        chart_type=chart_type,
        x=x,
        y=y,
        z=z,
        color=color,
    )

    relative_script, extra_script_src = _resolve_script_sources(workspace, option)
    html_content = _build_echarts_html(
        title=display_title,
        option=html_option,
        script_src=relative_script,
        interpretation=interpretation,
        extra_script_src=extra_script_src,
        data_table=data_table,
    )
    html_path, json_path = _write_echarts_artifacts(
        workspace,
        stem=stem,
        display_title=display_title,
        html_content=html_content,
        option=option,
    )

    return _echarts_response(
        chart_type=chart_type,
        chart_type_source=chart_type_source,
        rows_plotted=len(df),
        html_path=html_path,
        json_path=json_path,
        interpretation=interpretation,
        density_scale=density_scale,
        data_summary=data_summary,
        density_view=density_view,
        sampling_info=sampling_info,
        export_png=export_png,
    )


def _echarts_response(
    *,
    chart_type: str,
    chart_type_source: str,
    rows_plotted: int,
    html_path: Path,
    json_path: Path,
    interpretation: str,
    density_scale: dict[str, Any] | None,
    data_summary: dict[str, Any] | None,
    density_view: DensityView | None,
    sampling_info: dict[str, Any] | None,
    export_png: bool,
) -> dict[str, Any]:
    """组装与 Plotly 分支同结构的 response dict（键顺序与拆分前一致）。"""
    result = {
        "status": "ok",
        "chart_engine": "echarts",
        "chart_type": chart_type,
        "chart_type_source": chart_type_source,
        "rows_plotted": rows_plotted,
        "html": str(html_path),
        "echarts_json": str(json_path),
        "interpretation": interpretation,
        "category_coverage": {
            "complete": True,
            "observed_combinations": rows_plotted,
            "total_combinations": rows_plotted,
            "missing_count": 0,
        },
        "scale_mode": density_scale["scale_mode"] if density_scale else "auto",
        # 字段与 Plotly 分支的 _apply_outlier_scale_controls 完全一致：
        # 极端值走"主体尺度"时，坐标轴范围随响应一起返回，调用方不必猜。
        "scale_details": density_scale or {"scale_mode": "auto", "extreme_points": []},
    }
    if data_summary is not None:
        # 与 Plotly 分支同一结构：columns/points/sampled/rows（≤20 行）。
        result["data_summary"] = data_summary
    if density_view is not None:
        # 渲染方式对调用方（Agent/前端）可见：密度视图不是抽样，是精确聚合，
        # 因此既没有 sampling 字段，也不需要"完整数据在 JSON 里"的免责声明；
        # 网格覆盖范围与面板构成在这里说清楚（与 Plotly 分支同结构）。
        result["density_view"] = {
            "applied": True,
            "bins": density_view.bin_label,
            "panels": [panel.name for panel in density_view.panels],
            "bands": [band_label(band) for band in density_view.bands],
            "rows_used": density_view.rows_used,
            "rows_outside_view": density_view.dropped_rows,
            "full_data_json": str(json_path),
        }
    if sampling_info is not None:
        result["sampling"] = sampling_info
    if export_png:
        # ECharts 的服务端 PNG 需要 Node + 无头浏览器渲染 canvas，本项目的
        # 服务端只有 kaleido（Plotly 专用）。此前 export_png 在这条分支上被
        # 静默丢弃——调用方以为拿到了 PNG，实际什么都没有。改为显式告知，
        # 并指出浏览器内的两条导出路径（图内工具栏 / 预览模态的 PNG 按钮）。
        result["png_warning"] = (
            "ECharts 图表不支持服务端 PNG 导出（服务端 PNG 渲染依赖 kaleido，仅 Plotly 可用）；"
            "请在图表内使用「导出 PNG」工具栏按钮，或在预览模态点「PNG」按钮（均为浏览器内导出，"
            "分辨率与当前主题一致）。HTML 与 JSON 产物已正常生成。"
        )
    return result
