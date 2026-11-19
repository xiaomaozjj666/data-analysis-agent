"""3D 图表的数据摘要与可访问数据表（Gemini 审计第 5 条）。

读屏软件与无法交互的场景（打印、纯文本审阅、自动化核对）拿不到 3D 点云
里的任何数值。这里钉住两条产物的契约：

- 响应 JSON 增加 ``data_summary``（列名 / 点数 / 抽样标记 / ≤20 行实际绘图值）；
- HTML 里追加默认折叠的 ``<details>`` 数据表，带 ``caption`` 与 ``scope="col"``，
  且所有单元格都经过转义（分类名里带 ``<script>`` 也不能变成可执行标签）。

Plotly 与 ECharts 两条引擎都要覆盖。25 行数据用于钉住"最多 20 行"的截断。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

_MALICIOUS_GROUP = "<script>alert(1)</script>"


def _frame(rows: int) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "x": rng.normal(0, 1, rows),
        "y": rng.normal(0, 1, rows),
        "z": rng.normal(0, 1, rows),
        "group": [_MALICIOUS_GROUP if index % 2 else "甲组" for index in range(rows)],
    })


def _workspace(tmp_path: Path, frame: pd.DataFrame) -> DataWorkspace:
    source = tmp_path / "three_d.csv"
    frame.to_csv(source, index=False, encoding="utf-8")
    workspace = DataWorkspace(tmp_path / "runs", session_id="chart_summary")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def _mock_echarts_bundles(tmp_path: Path) -> tuple[Path, Path]:
    """伪造 echarts / echarts-gl bundle，避免测试触网。"""
    artifacts = tmp_path / "runs" / "chart_summary" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    base = artifacts / "echarts.min.js"
    base.write_text("/* mock echarts */", encoding="utf-8")
    gl = artifacts / "echarts-gl.min.js"
    gl.write_text("/* mock echarts-gl */", encoding="utf-8")
    return base, gl


def _assert_html_table(html: str) -> None:
    assert '<details class="chart-data-table">' in html
    assert "<caption>图表数据表（前 20 行）</caption>" in html
    assert '<th scope="col">x</th>' in html
    # 转义后的恶意分类名：可读文本仍在，但单元格里不会出现可执行标签
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<td><script>" not in html


def test_plotly_scatter3d_summary_and_accessible_table(tmp_path):
    workspace = _workspace(tmp_path, _frame(25))
    tools = {tool.name: tool for tool in build_tools(workspace)}

    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "x", "y": "y", "z": "z", "color": "group",
    }))

    summary = result["data_summary"]
    assert summary["columns"] == ["x", "y", "z", "group"]
    assert summary["points"] == 25
    assert summary["sampled"] is False
    assert len(summary["rows"]) == 20
    assert {row[3] for row in summary["rows"]} <= {"甲组", _MALICIOUS_GROUP}
    # 行内坐标确实是数值，不是占位符
    assert all(isinstance(value, (int, float)) for row in summary["rows"] for value in row[:3])

    html = Path(result["html"]).read_text(encoding="utf-8")
    _assert_html_table(html)


def test_echarts_scatter3d_summary_and_accessible_table(tmp_path):
    workspace = _workspace(tmp_path, _frame(25))
    tools = {tool.name: tool for tool in build_tools(workspace)}
    base, gl = _mock_echarts_bundles(tmp_path)

    with (
        patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=base),
        patch.object(DataWorkspace, "ensure_echarts_gl_bundle", return_value=gl),
    ):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter_3d",
            "x": "x",
            "y": "y",
            "z": "z",
            "color": "group",
            "chart_engine": "echarts",
        }))

    assert result["chart_engine"] == "echarts"
    summary = result["data_summary"]
    assert summary["columns"] == ["x", "y", "z", "group"]
    assert summary["points"] == 25
    assert summary["sampled"] is False
    assert len(summary["rows"]) == 20
    assert {row[3] for row in summary["rows"]} <= {"甲组", _MALICIOUS_GROUP}

    html = Path(result["html"]).read_text(encoding="utf-8")
    _assert_html_table(html)


def test_echarts_scatter3d_summary_reports_sampling(tmp_path):
    """3D 内部 3000 点上限触发时，data_summary 必须如实标记 sampled。"""
    workspace = _workspace(tmp_path, _frame(4_000))
    tools = {tool.name: tool for tool in build_tools(workspace)}
    base, gl = _mock_echarts_bundles(tmp_path)

    with (
        patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=base),
        patch.object(DataWorkspace, "ensure_echarts_gl_bundle", return_value=gl),
    ):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter_3d",
            "x": "x",
            "y": "y",
            "z": "z",
            "chart_engine": "echarts",
        }))

    summary = result["data_summary"]
    assert summary["sampled"] is True
    assert summary["points"] <= 3_000
    assert len(summary["rows"]) == 20
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    plotted = sum(len(series["data"]) for series in option["series"])
    assert plotted == summary["points"]
