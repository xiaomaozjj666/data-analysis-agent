"""层级类图型：旭日图 / 矩形树图。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_BASE_TITLE,
    _ECHARTS_BASE_TOOLTIP,
    _ECHARTS_PALETTE,
    _JsFunction,
)
from data_agent.render.primitives import _hex_to_rgba, _safe_value, _series_color


def _echarts_sunburst(
    df: pd.DataFrame,
    *,
    path_columns: list[str],
    values: str | None,
    title: str,
    is_treemap: bool = False,
) -> dict[str, Any]:
    """旭日图 / 矩形树图：层级结构可视化。"""
    if not path_columns:
        return {"title": {"text": title}}

    # 预聚合：重复路径先按 groupby 合并（有 values 列求和，否则计数），
    # 把建树规模从 O(行数) 压到 O(唯一路径数)；旧实现逐行线性扫描子节点
    # 大数据下是 O(n²)，且重复路径叶子值会被最后一行覆盖而非累加。
    if values and values in df.columns:
        agg_df = df.groupby(path_columns, dropna=False, sort=False)[values].sum().reset_index()
    else:
        agg_df = (
            df.groupby(path_columns, dropna=False, sort=False).size().reset_index(name="__count__")
        )

    # 构建层级树：路径元组→节点字典索引，避免逐子节点线性查找
    tree: dict[str, Any] = {"name": "全部", "children": []}
    node_index: dict[tuple[str, ...], dict[str, Any]] = {}
    last_depth = len(path_columns) - 1
    for row in agg_df.itertuples(index=False, name=None):
        current = tree
        path: tuple[str, ...] = ()
        for depth in range(len(path_columns)):
            name = str(row[depth])
            path = (*path, name)
            node = node_index.get(path)
            if node is None:
                node = {"name": name, "children": []}
                node_index[path] = node
                current["children"].append(node)
            current = node
            if depth == last_depth:
                current["value"] = _safe_value(row[len(path_columns)])

    # 聚合叶子值到父节点
    def aggregate(node: dict[str, Any]) -> float:
        if not node.get("children"):
            return float(node.get("value", 0) or 0)
        total = sum(aggregate(c) for c in node["children"])
        node["value"] = total
        return total

    aggregate(tree)

    # 按顶级类目定色：同一父系继承同色相（子级降透明度区分层次），
    # 颜色表达"属于哪个大类"；若按层级深度定色，同层类目全部同色，
    # 颜色失去区分意义。rgba 形态可被暗色脚本的前缀映射同步提亮。
    def paint(node: dict[str, Any], color: str, depth: int) -> None:
        alpha = max(1.0 - depth * 0.18, 0.5)
        node.setdefault("itemStyle", {})["color"] = (
            color if depth == 0 else _hex_to_rgba(color, round(alpha, 2))
        )
        for child in node.get("children", []):
            paint(child, color, depth + 1)

    for i, top in enumerate(tree["children"]):
        paint(top, _series_color(i), 0)

    chart_type = "treemap" if is_treemap else "sunburst"
    series: dict[str, Any] = {
        "name": title,
        "type": chart_type,
        "data": tree["children"],
        # 标签直接落在色块上：亮色板明度下白字对比最佳；暗色下映射表
        # 会把 #fff 翻成暗底色，恰好落在提亮色块上仍保持可读
        "label": {"color": "#ffffff", "fontSize": 12},
        "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "gapWidth": 2},
        "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.15)"}},
    }
    if not is_treemap:
        series["radius"] = ["20%", "90%"]
        series["nodeClick"] = "zoomToNode"

    return {
        "title": {**_ECHARTS_BASE_TITLE, "text": title, "subtext": "层级结构 · 点击下钻"},
        "tooltip": {
            **_ECHARTS_BASE_TOOLTIP,
            "trigger": "item",
            "formatter": _JsFunction(
                "function(p){return '<b>'+p.name+'</b><br/>值：<b>'+(p.value||0).toLocaleString()+'</b>';}"
            ),
        },
        "toolbox": {
            "right": 70,
            "top": 24,
            "feature": {"saveAsImage": {"title": "导出 PNG", "pixelRatio": 2}},
        },
        "color": _ECHARTS_PALETTE,
        "series": [series],
    }
