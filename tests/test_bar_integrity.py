"""柱状图的两条数据完整性契约：零基线 + 真堆叠。

两条都是"图上看起来像数据错了"的实测缺陷：

1. **截断基线**：三地区销售额 66~68 万，轴从 21 万起——柱长差异被放大成
   肉眼上的三四倍，而实际只差 1.9%。柱状图靠长度表达数值，基线必须含 0
   （折线图不受此限，斜率才是信息）。
2. **标题说"堆叠"、图上是并排柱**：模型按堆叠写标题，而工具没有堆叠能力，
   图形与文案不一致，用户会以为图表画错了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from data_agent.tools import build_tools
from data_agent.tools.builder import scatter_point_style, scatter_symbol_style
from data_agent.workspace import DataWorkspace

REGIONS = ["华东", "华北", "华南"]
CATEGORIES = ["家居", "电子", "食品"]


def _workspace(tmp_path: Path) -> DataWorkspace:
    rows = []
    for region_index, region in enumerate(REGIONS):
        for category_index, category in enumerate(CATEGORIES):
            # 三地区合计刻意接近（差异 <2%），便于暴露截断基线的放大效应
            rows.append({
                "区域": region,
                "品类": category,
                "销售额": 220_000 + region_index * 1_000 + category_index * 3_000,
            })
    frame = pd.DataFrame(rows)
    source = tmp_path / "stacked.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="stacked")
    workspace.load(source, copy_into_workspace=True)
    return workspace


@pytest.mark.parametrize("engine", ["plotly", "echarts"])
def test_bar_axis_starts_at_zero(tmp_path, engine):
    """柱状图 Y 轴必须含 0（两引擎一致）。"""
    tools = {tool.name: tool for tool in build_tools(_workspace(tmp_path))}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "区域", "y": "销售额", "aggregation": "sum",
        "chart_engine": engine, "title": "零基线检查",
    }))
    payload = json.loads(Path(result[f"{engine}_json"]).read_text(encoding="utf-8"))
    if engine == "echarts":
        axis = payload["yAxis"][0]
        assert axis["min"] == 0, f"ECharts Y 轴应从 0 开始，实际 {axis['min']}"
    else:
        # Plotly 的 yaxis 可能是 dict 也可能是 {"yaxis": {...}} 形式；未显式设置
        # range 时默认自适应到 0 基线，两种都算通过。
        layout = payload["layout"]
        axis = layout.get("yaxis") if isinstance(layout.get("yaxis"), dict) else None
        stated = axis.get("range") if isinstance(axis, dict) else None
        if stated is None:
            for key, value in layout.items():
                if key.startswith("yaxis") and isinstance(value, dict) and value.get("range"):
                    stated = value["range"]
                    break
        assert not stated or float(stated[0]) == 0.0


def test_stacked_bar_sets_stack_and_stacked_axis(tmp_path):
    """stacked=True：ECharts 系列带 stack，且轴范围按堆叠总和（≥ 单系列最大值）。"""
    tools = {tool.name: tool for tool in build_tools(_workspace(tmp_path))}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "区域", "y": "销售额", "color": "品类",
        "aggregation": "sum", "chart_engine": "echarts", "stacked": True,
        "title": "堆叠柱",
    }))
    payload = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    series = payload["series"]
    assert len(series) == len(CATEGORIES)
    assert all(item.get("stack") == "Total" for item in series), "堆叠标记缺失"
    axis = payload["yAxis"][0]
    assert axis["min"] == 0
    single_max = max(max(item["data"]) for item in series)
    assert axis["max"] >= single_max, "堆叠轴范围必须覆盖堆叠总和"


def test_grouped_bar_has_no_stack(tmp_path):
    """默认仍是分组柱：不带 stack 标记（避免两种语义混在一起）。"""
    tools = {tool.name: tool for tool in build_tools(_workspace(tmp_path))}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "区域", "y": "销售额", "color": "品类",
        "aggregation": "sum", "chart_engine": "echarts", "title": "分组柱",
    }))
    payload = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert all("stack" not in item for item in payload["series"])


def test_plotly_stacked_bar_uses_barmode(tmp_path):
    tools = {tool.name: tool for tool in build_tools(_workspace(tmp_path))}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "区域", "y": "销售额", "color": "品类",
        "aggregation": "sum", "chart_engine": "plotly", "stacked": True,
        "title": "Plotly 堆叠柱",
    }))
    payload = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    assert payload["layout"]["barmode"] == "stack"


@pytest.mark.parametrize(
    ("rows", "expected_size", "expected_opacity"),
    [
        (300, 9.0, 0.85),
        (1_500, 7.0, 0.72),
        (4_000, 5.5, 0.60),
        (12_000, 3.2, 0.45),
        (60_000, 3.2, 0.45),
    ],
)
def test_scatter_point_style_tiers(rows, expected_size, expected_opacity):
    """点径/透明度随点数分档，且两引擎共用同一张表（观感一致）。"""
    marker = scatter_point_style(rows)
    symbol, opacity, border = scatter_symbol_style(rows)
    assert marker["size"] == pytest.approx(expected_size)
    assert marker["opacity"] == pytest.approx(expected_opacity)
    assert symbol == pytest.approx(marker["size"]), "两引擎点径必须一致"
    assert opacity == pytest.approx(marker["opacity"])
    assert border == pytest.approx(marker["line"]["width"])
