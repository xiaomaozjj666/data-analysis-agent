"""3D 散点（echarts-gl）：分层抽样、option 构建与可访问数据摘要。"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_BASE_LEGEND,
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLBOX,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_BG_COLOR,
    _ECHARTS_BORDER_COLOR,
    _ECHARTS_GRID_COLOR,
    _ECHARTS_PALETTE,
    _ECHARTS_TEXT_SECONDARY,
    _JsFunction,
)
from data_agent.render.primitives import _rows_data, _series_color
from data_agent.tools._helpers import _human_column_label as _build_axis_label


def _stratified_3d_sample(
    df: pd.DataFrame,
    color: str | None,
    rows: int,
) -> tuple[pd.DataFrame, int]:
    """3D 散点的分层抽样：按分组配额抽样，返回 (抽样结果, 抽样后行数)。

    分配策略（保证总数不超过 ``rows``，且小组不被抹掉）：
    1. 每组先拿"下限 100 与组大小中的较小者"——分组极不平衡时整体
       ``df.sample`` 会让小组合并成零星几个点，"这类没有数据"的错觉比抽样
       本身更糟，因此小组合部保留；
    2. 剩余额度按各组"还能贡献多少"的比例分配，整取余数不补，因此总和
       不会超过预算（3D 云超预算会直接卡渲染）。
    """
    if len(df) <= rows:
        return df, len(df)
    if not color or color not in df.columns:
        sampled = df.sample(n=rows, random_state=42)
        return sampled, len(sampled)
    groups = [(level, chunk) for level, chunk in df.groupby(color, observed=True, dropna=True)]
    if not groups:
        sampled = df.sample(n=rows, random_state=42)
        return sampled, len(sampled)

    floor = 100
    chunks = [chunk for _, chunk in groups]
    quotas = [min(len(chunk), floor) for chunk in chunks]
    remaining = rows - sum(quotas)
    if remaining > 0:
        space = [len(chunk) - quota for chunk, quota in zip(chunks, quotas, strict=True)]
        total_space = sum(space)
        if total_space > 0:
            for index, spare in enumerate(space):
                quotas[index] += int(remaining * spare / total_space)

    pieces = [
        chunk.sample(n=min(quota, len(chunk)), random_state=42)
        for chunk, quota in zip(chunks, quotas, strict=True)
        if quota > 0
    ]
    sampled = pd.concat(pieces) if pieces else df.head(rows)
    return sampled, len(sampled)


def _scatter3d_tooltip(
    x_label: str,
    y_label: str,
    z_label: str,
    size: str | None,
    has_size: bool,
) -> _JsFunction:
    """3D 散点 tooltip：三轴 + 可选 size 维度，列名 json.dumps 转义防注入。"""
    labels_js = [json.dumps(lbl, ensure_ascii=False) for lbl in (x_label, y_label, z_label)]
    # 与 2D 图一致：tooltip 数值带千分位、最多保留 2 位小数，避免裸值长尾巴
    lines = "".join(
        f"html+='<span style=\"color:var(--tt-muted,#6b7280);\">'+{lbl}+'：</span><b>'+fmt(val[{idx}])+'</b><br/>';"
        for idx, lbl in enumerate(labels_js)
    )
    if has_size:
        size_label_js = json.dumps(_build_axis_label(size), ensure_ascii=False)
        lines += f"html+='<span style=\"color:var(--tt-muted,#6b7280);\">'+{size_label_js}+'：</span><b>'+fmt(val[3])+'</b><br/>';"
    return _JsFunction(
        "function(params){var val=params.value;"
        "var fmt=function(v){return (v===null||v===undefined||isNaN(v))?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:2});};"
        "var html='<div style=\"font-weight:600;margin-bottom:6px;\">'+params.seriesName+'</div>';"
        + lines
        + "return html;}"
    )


def _scatter3d_symbol_size(df: pd.DataFrame, size: str | None, has_size: bool) -> Any:
    """size 维度在 3D 数据里位于 val[3]，归一化到 [6, 22]（3D 点过大会互相遮挡）。"""
    size_vals = pd.to_numeric(df[size], errors="coerce").dropna().tolist() if has_size else []
    if size_vals:
        size_min, size_max = float(min(size_vals)), float(max(size_vals))
        return _JsFunction(
            "function(val){var v=val[3];if(v==null||isNaN(v))return 8;"
            f"var min={size_min},max={size_max};"
            "if(max<=min)return 12;return 6+(v-min)/(max-min)*16;}"
        )
    return 9


def _echarts_scatter3d(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    z: str,
    color: str | None,
    size: str | None,
    title: str,
) -> dict[str, Any]:
    """真 3D 散点图（echarts-gl）：拖拽旋转、滚轮缩放、颜色分组、大小维度。"""
    x_label, y_label, z_label = _build_axis_label(x), _build_axis_label(y), _build_axis_label(z)

    def axis3d(name: str) -> dict[str, Any]:
        return {
            "type": "value",
            "name": name,
            "scale": True,
            "nameTextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
            "axisLine": {"lineStyle": {"color": _ECHARTS_GRID_COLOR}},
            "axisLabel": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 10},
            "splitLine": {"lineStyle": {"color": _ECHARTS_BORDER_COLOR}},
        }

    has_size = bool(size) and size in df.columns
    tooltip_fmt = _scatter3d_tooltip(x_label, y_label, z_label, size, has_size)
    symbol_size = _scatter3d_symbol_size(df, size, has_size)

    def row_data(sub: pd.DataFrame) -> list[list[Any]]:
        return _rows_data(sub, [x, y, z] + ([size] if has_size else []))

    levels = list(pd.unique(df[color].dropna())) if color and color in df.columns else []
    # 3D 散点降采样：echarts-gl 的 scatter3D 在万点以上明显卡顿，5 万点几乎
    # 不可用。抽样到 _SCATTER3D_MAX_POINTS 行，保留分布特征同时保证交互流畅。
    # 按分组分层分配配额（而不是整体 df.sample）：着色分组若极不平衡，
    # 整体抽样会把小组合并成几个点，观众会误以为"这类没有数据"。
    _SCATTER3D_MAX_POINTS = 3000
    sampled_df, sampled_rows = _stratified_3d_sample(
        df, color if levels else None, _SCATTER3D_MAX_POINTS
    )
    groups = (
        [
            (str(lv), sampled_df[sampled_df[color] == lv], _series_color(idx))
            for idx, lv in enumerate(levels)
        ]
        if levels
        else [("样本", sampled_df, _ECHARTS_PALETTE[0])]
    )
    series = [
        {
            "name": gname,
            "type": "scatter3D",
            "data": row_data(sub),
            "symbolSize": symbol_size,
            "itemStyle": {"color": gcolor, "opacity": 0.82},
            "emphasis": {"itemStyle": {"opacity": 1}},
        }
        for gname, sub, gcolor in groups
    ]

    base: dict[str, Any] = {
        "title": {
            **_ECHARTS_BASE_TITLE,
            "text": title,
            # 抽样事实写在副标题上：3D 云从 3000 个点看不出"这只是全量的
            # 百分之一"，不写清楚用户会以为看的是全部记录。
            "subtext": f"{x_label} × {y_label} × {z_label}（拖拽旋转 · 滚轮缩放）"
            + (f" · 已抽样 {sampled_rows:,}/{len(df):,} 点" if sampled_rows < len(df) else ""),
        },
        "tooltip": {**_ECHARTS_BASE_TOOLTIP, "trigger": "item", "formatter": tooltip_fmt},
        # 3D 场景下 dataZoom 框选/还原/重置只对直角坐标系生效，保留会变成
        # 点了没反应的死按钮，故工具栏仅保留 PNG 导出。
        "toolbox": {
            **{k: v for k, v in _ECHARTS_BASE_TOOLBOX.items() if k != "feature"},
            "feature": {
                "saveAsImage": {
                    "title": "导出 PNG",
                    "pixelRatio": 2,
                    "backgroundColor": _ECHARTS_BG_COLOR,
                }
            },
        },
        "color": _ECHARTS_PALETTE,
        "grid3D": {
            # 盒体加大 + 视距拉近，让立体场景填满画布（默认 230 时四周留白过多）；
            # 不开自动旋转，旋转完全由用户拖拽控制。
            "boxWidth": 130,
            "boxDepth": 130,
            "boxHeight": 100,
            # 限制滚轮缩放范围：防止缩到看不见或推进盒体内部迷失视角
            "viewControl": {
                "projection": "perspective",
                "autoRotate": False,
                "rotateSensitivity": 1.6,
                "distance": 210,
                "minDistance": 100,
                "maxDistance": 420,
            },
            "light": {"main": {"intensity": 1.1, "shadow": False}, "ambient": {"intensity": 0.5}},
        },
        "xAxis3D": axis3d(x_label),
        "yAxis3D": axis3d(y_label),
        "zAxis3D": axis3d(z_label),
        "series": series,
    }
    if levels:
        base["legend"] = {**_ECHARTS_BASE_LEGEND, "data": [str(item) for item in levels]}
    return base


def _scatter3d_data_summary(
    option: dict[str, Any],
    *,
    x: str,
    y: str,
    z: str,
    color: str | None,
    total_rows: int,
) -> dict[str, Any] | None:
    """从 ECharts option 的实际 series 数据构造 ``data_summary``。

    与 Plotly 分支同结构：``columns`` 是 x/y/z（有分组时加颜色列），
    ``rows`` 是最多 20 行实际送入浏览器的坐标值，``points`` 是嵌入点数，
    ``sampled`` 是"是否发生抽样"（3D 散点内部有 3000 点上限，HTML 嵌入
    抽样还可能再降一次；两者任一触发都为 True）。

    数据直接从 ``option["series"][].data`` 读取而不是回读 DataFrame：
    抽样后的 option 才是用户看到的点。3D 降级为 2D（缺 z）时没有
    ``scatter3D`` 系列，返回 ``None``，不生成误导性的数据表。
    """
    from data_agent.tools._plotly_render import build_data_summary

    series = [
        item
        for item in (option.get("series") or [])
        if isinstance(item, dict) and item.get("type") == "scatter3D"
    ]
    if not series:
        return None
    columns = [x, y, z] + ([color] if color else [])
    rows: list[list[Any]] = []
    points = 0
    for item in series:
        data = item.get("data") or []
        if not isinstance(data, list):
            continue
        points += len(data)
        group = item.get("name") if color else None
        for record in data:
            if not isinstance(record, (list, tuple)) or len(record) < 3:
                continue
            coordinates = [record[0], record[1], record[2]]
            rows.append([*coordinates, group] if color else coordinates)
    return build_data_summary(columns, rows, points=points, sampled=points < total_rows)
