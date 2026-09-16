"""大数据散点 → 密度视图的 Plotly 渲染（与 ECharts 分支共享同一份网格）。

为什么存在
----------
几十万行的散点按"每点一个标记"渲染时，同一像素上叠加几十个半透明点，
alpha 迅速饱和成一团灰噪声——品类色互相覆盖、密度差异被抹平，用户实测
反馈"所有的点和数据都堆在一起，根本看不出来什么"。这里改为业界通行做法
（datashader 光栅化、2D 直方图密度图、seaborn ``jointplot``）：把点聚合
进网格、用**分档颜色**表达每格记录数，再补上结构锚点。聚合在服务端
numpy 完成，浏览器只收到几千个格子——30 万行散点的 HTML 从 12MB 降到
几百 KB，且密度是**精确**的（不像抽样那样只是近似）。

两种布局
--------
- 无颜色分组：主密度面板 + 顶部/右侧边缘直方图（jointplot 布局），
  叠加整体趋势线、均值参考线与最外围原始记录；
- 有颜色分组（≤6 类）：每类一个密度面板、共享同一档位色标（分面密度图，
  Tableau / datashader 对"类别 + 大数据"的推荐做法："颜色只表达密度，
  类别用分面表达"），面板标题给出记录数、占比与相关系数。

ECharts 分支（``echarts_engine._echarts_scatter_density``）渲染同一份
``DensityView`` 与同一套档位色板，保证双引擎视觉与语义一致。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from ..density import (
    DensityView,
    band_indexes,
    band_label,
    clip_segment,
    colorscale_from_bands,
    extreme_points,
    format_number,
)
from ._helpers import (
    _compact_number,
    _plotly_axis_tickformat,
    _scatter_structure,
)
from ._helpers import (
    _human_column_label as _hl,
)

#: 每个面板叠加的"最外围原始记录"数量上限。密度图按定义丢掉单点身份，
#: 叠加少数真实极端记录既保住"个别记录长什么样"，又不会重新糊住画面
#: （datashader 的 inspection reductions / Power BI 高密度抽样同理）。
DENSITY_EXTREME_POINTS = 120

#: 分面趋势线的显示门槛：弱相关（|r| < 0.25）时线没有解读价值，反成噪声。
DENSITY_TREND_MIN_R = 0.25


def _round_sig(values: np.ndarray, sig: int = 6) -> np.ndarray:
    """按有效位数取整（压缩 hover 用 customdata 的 JSON 体积）。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        exponent = np.floor(np.log10(np.abs(values)))
    factor = np.power(10.0, sig - 1 - exponent)
    with np.errstate(invalid="ignore"):
        rounded = np.round(values * factor) / factor
    return np.where(np.isfinite(rounded), rounded, values)


def _density_customdata(grid: Any) -> np.ndarray:
    """密度格子的 hover 数据：(ny, nx, 6) = 边界 x0/x1/y0/y1、记录数、占比%。

    Plotly 的 heatmap hover 只给格中心（``%{x}``/``%{y}``），没有分箱区间
    字段，所以边界必须以 customdata 显式带上，hover 才能说清"这一格覆盖
    哪一段数值、装了多少条记录"。
    """
    counts = grid.counts
    ny, nx = counts.shape
    x0 = np.tile(grid.x_edges[:-1], (ny, 1))
    x1 = np.tile(grid.x_edges[1:], (ny, 1))
    y0 = np.tile(grid.y_edges[:-1, None], (1, nx))
    y1 = np.tile(grid.y_edges[1:, None], (1, nx))
    share = counts / grid.total * 100 if grid.total else np.zeros(counts.shape, dtype=float)
    return np.stack([
        _round_sig(x0), _round_sig(x1), _round_sig(y0), _round_sig(y1),
        counts.astype(float), np.round(share, 3),
    ], axis=-1)


def _hover_template(x_label: str, y_label: str, x_format: str, y_format: str,
                    extra: str) -> str:
    return (
        f"{x_label} %{{customdata[0]:{x_format}}} ~ %{{customdata[1]:{x_format}}}"
        f"<br>{y_label} %{{customdata[2]:{y_format}}} ~ %{{customdata[3]:{y_format}}}"
        "<br>记录数 %{customdata[4]:,.0f}（占 %{customdata[5]:.2f}%）"
        f"<extra>{extra}</extra>"
    )


def panel_structure(df: pd.DataFrame, *, x: str, y: str, color: str | None,
                    panel: Any) -> dict[str, Any] | None:
    """某个密度面板对应原始记录的结构注记（趋势线端点、均值、皮尔逊 r）。"""
    if x not in df.columns or y not in df.columns:
        return None
    subset = df
    if color and panel.levels:
        subset = df[df[color].astype(str).isin(list(panel.levels))]
    pair = subset[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(pair) < 3:
        return None
    return _scatter_structure(pair[x].astype(float).tolist(), pair[y].astype(float).tolist())


def _panel_title(panel: Any, structure: dict[str, Any] | None) -> str:
    label = f"{panel.name} · {panel.grid.total:,} 条（{panel.share * 100:.0f}%）"
    if structure and structure.get("r") is not None and abs(structure["r"]) >= DENSITY_TREND_MIN_R:
        label += f" · r={structure['r']:.2f}"
    return label


def density_figure(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    color: str | None,
    view: DensityView,
    x_label: str,
    y_label: str,
    title: str,
    colors: Sequence[str] | None = None,
) -> go.Figure:
    """把密度视图渲染成 Plotly 图（分面密度热力图 / 整体 + 边缘直方图）。"""
    from plotly.subplots import make_subplots

    panels = view.panels
    bands = view.bands
    band_names = [band_label(band) for band in bands]
    scale_light = colorscale_from_bands(view.band_colors())
    scale_dark = colorscale_from_bands(view.band_colors(dark=True))
    faceted = view.is_faceted
    x_format = _plotly_axis_tickformat(view.x_range)
    y_format = _plotly_axis_tickformat(view.y_range)
    structures = [panel_structure(df, x=x, y=y, color=color, panel=panel) for panel in panels]

    if faceted:
        count = len(panels)
        cols = 2 if count in (2, 4) else 3
        rows = int(math.ceil(count / cols))
        titles = [_panel_title(panel, structure)
                  for panel, structure in zip(panels, structures, strict=True)]
        fig = make_subplots(
            rows=rows, cols=cols, shared_xaxes=True, shared_yaxes=True,
            subplot_titles=titles + [""] * (rows * cols - len(titles)),
            horizontal_spacing=0.055, vertical_spacing=0.135,
        )
        positions = [(index // cols + 1, index % cols + 1) for index in range(count)]
    else:
        fig = make_subplots(
            rows=2, cols=2, shared_xaxes=True, shared_yaxes=True,
            column_widths=[0.885, 0.115], row_heights=[0.115, 0.885],
            horizontal_spacing=0.012, vertical_spacing=0.022,
        )
        positions = [(2, 1)]

    for index, (panel, (row, col)) in enumerate(zip(panels, positions, strict=True)):
        grid = panel.grid
        fig.add_trace(
            go.Heatmap(
                x=grid.x_centers,
                y=grid.y_centers,
                z=band_indexes(grid.counts, bands),
                customdata=_density_customdata(grid),
                hovertemplate=_hover_template(
                    x_label, y_label, x_format, y_format, panel.name or y_label
                ),
                # 档位色标写在 trace 上（而不是共享 coloraxis）：plotly.py 默认
                # 模板的 layout.colorscale/layout.coloraxis 会在 coloraxis 被
                # 显式引用时按"是否跨零"自动挑 sequential/diverging 色板，
                # 反而盖掉档位色板。z 是档位序号，zmin/zmax 取 ±0.5 让每档
                # 正好落在 colorscale 等分段的正中。
                colorscale=scale_light,
                zmin=-0.5,
                zmax=len(bands) - 0.5,
                showscale=index == 0,
                colorbar={
                    "title": {"text": "记录数/格", "side": "right", "font": {"size": 12}},
                    "tickvals": list(range(len(bands))),
                    "ticktext": band_names,
                    "thickness": 13,
                    "len": 0.66 if faceted else 0.86,
                    "outlinewidth": 0,
                    "ticks": "",
                    "x": 1.006,
                    "xanchor": "left",
                    "y": 0.5,
                    "yanchor": "middle",
                },
                # 零记录格完全透明（datashader 同样规定 0 值不参与着色）：
                # 点云的真实覆盖范围才是信息，不能拿背景色冒充。
                hoverongaps=False,
                name=panel.name or y_label,
                showlegend=False,
            ),
            row=row, col=col,
        )
        _add_anchors(
            fig, df, panel=panel, structure=structures[index], view=view,
            x=x, y=y, color=color, row=row, col=col, faceted=faceted,
            x_format=x_format, y_format=y_format,
        )

    if not faceted:
        _add_marginals(fig, panels[0].grid, colors=colors)
        _add_structure(fig, structures[0], row=2, col=1)

    _apply_axis_titles(fig, positions=positions, faceted=faceted,
                       x_label=x_label, y_label=y_label)

    fig.update_layout(meta={
        "density_view": {
            "bins": view.bin_label,
            "panels": len(panels),
            "bands": band_names,
            "total": view.total,
            # 主题切换时由注入脚本换用暗色档位色板：同一颜色在两种主题下
            # 必须代表同一档记录数（Tableau 明/暗色板同理），所以两套色标
            # 都随图带过去，而不是在暗色下重新分档。
            "colorscales": {"light": scale_light, "dark": scale_dark},
        }
    })
    fig.update_layout(title={"text": title, "x": 0.01, "xanchor": "left",
                             "font": {"size": 22}})
    if view.dropped_rows:
        # 极端值把量程拉大时，网格按"主体尺度"聚合——被排除的记录必须在
        # 画面上说清楚，否则用户会以为图表漏了数据（文字解读里也有，但
        # 图表本身才是第一现场）。
        fig.add_annotation(
            # 右下角：底部中央是 x 轴标题、左侧是 y 轴标题，只有右下角是空的
            # （放左下角时实测与"销售额"轴标题叠在一起）。
            xref="paper", yref="paper", x=1.0, y=-0.045,
            xanchor="right", yanchor="top", showarrow=False, align="right",
            text=(f"视图外 {view.dropped_rows:,} 条记录超出主体尺度（未计入密度网格）；"
                  "悬浮/下载 JSON 可查看全量数据"),
            font={"size": 11, "color": "#B4762E"},
            bgcolor="rgba(217,160,91,0.16)", bordercolor="#D9A05B",
            borderwidth=1, borderpad=5,
        )
    fig.update_xaxes(range=list(view.x_range))
    fig.update_yaxes(range=list(view.y_range))
    return fig


def _add_anchors(fig: go.Figure, df: pd.DataFrame, *, panel: Any,
                 structure: dict[str, Any] | None, view: DensityView,
                 x: str, y: str, color: str | None, row: int, col: int,
                 faceted: bool, x_format: str, y_format: str) -> None:
    """面板内结构锚点：分面趋势线、密度峰值框、整体布局下的最外围原始点。"""
    grid = panel.grid
    if structure and structure.get("trend"):
        r_value = structure.get("r")
        if r_value is not None and abs(r_value) >= DENSITY_TREND_MIN_R:
            # 趋势线按面板数据的 min/max 算端点，但坐标轴是共享范围，
            # 必须裁剪到可视范围（否则线会从画面上方穿出去）。
            trend = clip_segment(structure["trend"], x_range=view.x_range,
                                 y_range=view.y_range)
            if trend:
                fig.add_shape(
                    type="line", x0=trend[0], y0=trend[1], x1=trend[2], y1=trend[3],
                    line={"color": "#E15759", "width": 2}, row=row, col=col,
                )

    peak = grid.peak()
    if peak and grid.has_clusters:
        # 密度峰值框：纯色块图上最缺的锚点就是"最挤的一格在哪"。
        fig.add_shape(
            type="rect", x0=peak["x0"], x1=peak["x1"], y0=peak["y0"], y1=peak["y1"],
            line={"color": "#E15759", "width": 1.6}, fillcolor="rgba(0,0,0,0)",
            row=row, col=col,
        )
        fig.add_annotation(
            x=(peak["x0"] + peak["x1"]) / 2, y=(peak["y0"] + peak["y1"]) / 2,
            text=f"最密 {format_number(peak['count'])} 条",
            showarrow=True, arrowhead=2, arrowsize=1, arrowwidth=1,
            ax=0, ay=-30, font={"size": 10, "color": "#B23A3C"},
            bgcolor="rgba(255,255,255,0.82)", bordercolor="#E15759",
            borderwidth=1, borderpad=2, row=row, col=col,
        )

    if faceted:
        return
    subset = df
    if color and panel.levels:
        subset = df[df[color].astype(str).isin(list(panel.levels))]
    pair = subset[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(pair) < 10:
        return
    xs, ys = extreme_points(
        pair[x].to_numpy(dtype=float), pair[y].to_numpy(dtype=float),
        x_range=view.x_range, y_range=view.y_range, top=DENSITY_EXTREME_POINTS,
    )
    if len(xs) == 0:  # pragma: no cover - 网格存在即至少有一个视口内记录
        return
    fig.add_trace(
        go.Scattergl(
            x=xs, y=ys, mode="markers", name="最外围记录",
            marker={"size": 5, "color": "rgba(225,87,89,0.85)",
                    "line": {"width": 0.8, "color": "rgba(255,255,255,0.9)"}},
            hovertemplate=(
                f"{_hl(x)}: %{{x:{x_format}}}<br>{_hl(y)}: %{{y:{y_format}}}"
                "<extra>最外围记录（原始点）</extra>"
            ),
        ),
        row=row, col=col,
    )


def _add_marginals(fig: go.Figure, grid: Any, *, colors: Sequence[str] | None) -> None:
    """整体密度图的边缘分布（顶部 x 直方图 + 右侧 y 直方图）。

    分箱与主面板完全一致（直接复用网格的行列求和），因此边缘直方图的每根
    柱子与主面板的每一列/行严格对齐——用户能把"分布形状"与"密度图"对上，
    这是 jointplot 布局最核心的价值。
    """
    # 边缘直方图用中性的浅蓝灰：它是配角，用品牌色板里的饱和色（绿/蓝）会与
    # 主面板的密度色阶抢注意力，读者会误以为那是另一组数据。
    del colors  # 保留参数以兼容调用方签名（颜色不再取自品牌色板）
    bar_color = "#B4C6D4"
    bin_width_x = float(grid.x_edges[1] - grid.x_edges[0])
    bin_width_y = float(grid.y_edges[1] - grid.y_edges[0])
    fig.add_trace(
        go.Bar(
            x=grid.x_centers, y=grid.marginal_x(), width=bin_width_x,
            marker={"color": bar_color, "line": {"width": 0}}, opacity=0.9,
            hovertemplate="记录数 %{y:,.0f}<extra>x 分布</extra>",
            name="x 分布", showlegend=False,
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(
            x=grid.marginal_y(), y=grid.y_centers, width=bin_width_y,
            orientation="h",
            marker={"color": bar_color, "line": {"width": 0}}, opacity=0.9,
            hovertemplate="记录数 %{x:,.0f}<extra>y 分布</extra>",
            name="y 分布", showlegend=False,
        ),
        row=2, col=2,
    )


def _add_structure(fig: go.Figure, structure: dict[str, Any] | None,
                   *, row: int, col: int) -> None:
    """整体密度图的趋势线与均值参考线（Tableau 式视觉锚点）。"""
    if not structure:
        return
    trend = structure.get("trend")
    r_value = structure.get("r")
    if trend:
        name = f"趋势线 r={r_value:.2f}" if r_value is not None else "趋势线"
        fig.add_trace(
            go.Scatter(
                x=[trend[0], trend[2]], y=[trend[1], trend[3]], mode="lines",
                name=name, line={"color": "#E15759", "width": 2.5}, hoverinfo="skip",
            ),
            row=row, col=col,
        )
    if structure.get("mean_y") is not None:
        fig.add_hline(
            y=structure["mean_y"], line_dash="dash", line_color="#9aa0a6", line_width=1.2,
            annotation_text=f"y 均值 {_compact_number(structure['mean_y'])}",
            annotation_position="top right",
            annotation_font={"size": 10, "color": "#6b7280"},
            row=row, col=col,
        )
    if structure.get("mean_x") is not None:
        fig.add_vline(
            x=structure["mean_x"], line_dash="dash", line_color="#9aa0a6", line_width=1.2,
            annotation_text=f"x 均值 {_compact_number(structure['mean_x'])}",
            annotation_position="top right",
            annotation_font={"size": 10, "color": "#6b7280"},
            row=row, col=col,
        )


def _apply_axis_titles(fig: go.Figure, *, positions: list[tuple[int, int]],
                       faceted: bool, x_label: str, y_label: str) -> None:
    """只在最外圈面板上写轴标题（共享轴的内圈重复标题是噪声）。"""
    if not faceted:
        fig.update_xaxes(title_text=x_label, row=2, col=1)
        fig.update_yaxes(title_text=y_label, row=2, col=1)
        return
    rows = max(row for row, _ in positions)
    for row, col in positions:
        if col == 1:
            fig.update_yaxes(title_text=y_label, row=row, col=col)
        if row == rows:
            fig.update_xaxes(title_text=x_label, row=row, col=col)


def density_interpretation(
    df: pd.DataFrame,
    *,
    view: DensityView,
    x: str,
    y: str,
    color: str | None,
    title: str,
    x_label: str,
    y_label: str,
) -> str:
    """密度视图的白话解读：讲清"颜色读什么"＋把算出来的锚点写成业务句子。

    点云图原本的解读（"滚轮缩放可查看密集区域，框选可隔离离群点"）对密度
    图不成立——没有可框选的离散点，颜色也不表示数值大小。这里改成：相关性
    一句话 + 密度编码说明 + 最密集区域 + 分面差异 + 正确的交互提示。
    """
    from ..density import density_note, hotspot_sentence, panels_sentence

    color_label = _hl(color) if color else None
    parts: list[str] = []
    structure = panel_structure(df, x=x, y=y, color=None, panel=view.panels[0])
    r_value = structure.get("r") if structure else None
    if r_value is not None:
        direction = "正向" if r_value > 0 else "反向"
        strength = "强" if abs(r_value) > 0.7 else "中等" if abs(r_value) > 0.4 else "弱"
        # 记录条数交给 density_note 统一交代（含"视图外 N 条"），这里不重复。
        parts.append(f"「{title}」呈{direction}{strength}相关（r={r_value:.2f}）。")
    parts.append(density_note(view, color_label=color_label))
    hotspot = hotspot_sentence(view, x_label, y_label)
    if hotspot:
        parts.append(hotspot)
    if color_label:
        panels = panels_sentence(view, color_label)
        if panels:
            parts.append(panels)
    parts.append("颜色深浅表示每格记录数的多少（色标给出各档区间，格子越深越密），"
                 "空格表示该范围没有记录；滚轮可放大局部，鼠标悬浮查看每格覆盖的"
                 "数值范围与记录数。")
    return "".join(parts)


def finalize_density_layout(fig: go.Figure, view: DensityView) -> None:
    """密度图收尾样式：必须在 builder 的通用布局样式之后调用。

    通用样式会把网格/刻度应用到所有子图，边缘直方图带网格线会显得脏；
    这里清干净边缘面板，并按档位图例的实际宽度留出右边距（否则色标
    会被裁掉一半）。
    """
    fig.update_layout(margin={"l": 64, "r": 128, "t": 108, "b": 96})
    if view.is_faceted:
        for annotation in fig.layout.annotations:
            text = str(annotation.text or "")
            if "·" in text or ("条" in text and "（" in text):
                # 分面标题加粗，让它读起来像"面板标题"而不是数据标注。
                # 颜色不写死：暗色主题下标题若保留浅色主题的深色，会看不见
                # （注入脚本只改布局字体色，不逐个改标注）。
                annotation.font = {"size": 12.5}
        return
    fig.update_layout(bargap=0.02)
    fig.update_yaxes(showticklabels=False, showgrid=False, title_text="", row=1, col=1)
    fig.update_xaxes(showticklabels=False, showgrid=False, title_text="", row=2, col=2)
    fig.update_xaxes(visible=False, showgrid=False, row=1, col=2)
    fig.update_yaxes(visible=False, showgrid=False, row=1, col=2)
