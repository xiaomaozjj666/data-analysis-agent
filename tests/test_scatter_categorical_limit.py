"""横轴为类别列的超大散点：无法聚合密度，但必须把"糊成一团"讲清楚。

密度视图是"数值 × 数值"的聚合方案。横轴是类别时（几十万行、每类几万个点）
仍走逐点渲染，视觉上会重新变成一堵点墙；本项目不擅自替换用户点名的图型，
因此约定：保留散点 + 在解读里说明原因并给出更合适的图型建议。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _workspace(tmp_path: Path) -> DataWorkspace:
    rng = np.random.default_rng(101)
    rows = 25_000
    frame = pd.DataFrame({
        "region": rng.choice(["华东", "华南", "华北", "西南", "东北"], rows),
        "sales": rng.gamma(2.5, 200, rows),
        "category": rng.choice(["甲", "乙", "丙"], rows),
    })
    source = tmp_path / "categorical_scatter.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="categorical_scatter")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def test_categorical_x_big_scatter_keeps_points_and_explains_limit(tmp_path):
    workspace = _workspace(tmp_path)
    tools = {tool.name: tool for tool in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "region", "y": "sales", "color": "category",
    }))

    # 不适用密度视图：没有 density_view 字段，轨迹仍是逐点散点
    assert "density_view" not in result
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    types = {trace["type"] for trace in figure["data"]}
    assert types <= {"scatter", "scattergl", "box"} and types
    assert "heatmap" not in types

    html = Path(result["html"]).read_text(encoding="utf-8")
    assert "无法聚合成密度网格" in html
    assert "箱线图" in html


def test_numeric_x_big_scatter_still_uses_density(tmp_path):
    """对照：把数值列放到横轴上就应当自动走密度视图（提示不能误伤）。"""
    workspace = _workspace(tmp_path)
    tools = {tool.name: tool for tool in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "sales", "color": "category",
    }))
    assert result["density_view"]["applied"] is True
    assert "无法聚合成密度网格" not in Path(result["html"]).read_text(encoding="utf-8")
