"""ECharts 请求 PNG 导出时必须显式告知，不能静默什么都不做。

服务端 PNG 渲染依赖 kaleido（Plotly 专用），ECharts 这条路本来拿不到 PNG。
此前 `export_png=True` 在 ECharts 分支被**静默丢弃**——调用方（模型/前端）
以为产物里有 PNG，实际没有，也没有任何提示。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _workspace(tmp_path: Path) -> DataWorkspace:
    frame = pd.DataFrame({
        "地区": ["华东", "华南", "华北", "西南"] * 25,
        "销售额": [float(i * 3 % 97 + 10) for i in range(100)],
        "利润": [float(i % 23 + 1) for i in range(100)],
    })
    source = tmp_path / "png.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="png")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def test_echarts_export_png_reports_warning(tmp_path):
    tools = {tool.name: tool for tool in build_tools(_workspace(tmp_path))}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "地区", "y": "销售额",
        "chart_engine": "echarts", "export_png": True, "title": "PNG 语义检查",
    }))
    assert result.get("png") is None
    warning = result.get("png_warning") or ""
    assert "ECharts" in warning and "PNG" in warning
    # 必须给出可用的替代路径，而不是只说"不行"
    assert "PNG" in warning and ("工具栏" in warning or "预览" in warning)
    # HTML 与 JSON 产物照常生成
    assert Path(result["html"]).is_file()
    assert Path(result["echarts_json"]).is_file()


def test_echarts_without_export_png_has_no_warning(tmp_path):
    tools = {tool.name: tool for tool in build_tools(_workspace(tmp_path))}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "地区", "y": "销售额",
        "chart_engine": "echarts", "title": "未请求 PNG",
    }))
    assert "png_warning" not in result
