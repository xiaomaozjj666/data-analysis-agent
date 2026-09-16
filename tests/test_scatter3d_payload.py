"""大数据 3D 散点的载荷控制（此前是漏掉的口子）。

背景：HTML 嵌入降采样只覆盖 ``scatter`` / ``line``，``scatter_3d``（走 gl3d
的 scatter3d）不在名单里——30 万行时交互 HTML 要带 90 万个数字。

注意：不能用"HTML 比 .plotly.json 小"当断言——.plotly.json 里 numpy 数组被
序列化成 base64 typed array，本身就比 HTML 里的明文 JSON 数组紧凑，抽样后
HTML 仍可能更大。真正要钉住的是"抽样函数对 scatter3d 生效且各数组同步"。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from data_agent.chart_sampling import _decode_plotly_typed_arrays, sample_plotly_figure_for_embed
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _workspace(tmp_path: Path, rows: int) -> DataWorkspace:
    rng = np.random.default_rng(31)
    frame = pd.DataFrame({
        "x": rng.normal(0, 1, rows),
        "y": rng.normal(0, 1, rows),
        "z": rng.normal(0, 1, rows),
        "group": rng.choice(["甲", "乙"], rows),
    })
    source = tmp_path / f"three_d_{rows}.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id=f"three_d_{rows}")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def test_sample_plotly_figure_for_embed_covers_scatter3d_and_stays_aligned():
    """scatter3d 的 x/y/z/颜色/文本必须按同一步长抽样（错位会画出假图）。"""
    rows = 60_000
    figure = {
        "data": [{
            "type": "scatter3d",
            "x": list(range(rows)),
            "y": [value * 2 for value in range(rows)],
            "z": [value * 3 for value in range(rows)],
            "text": [f"第{value}行" for value in range(rows)],
            "marker": {"color": list(range(rows))},
        }]
    }
    sampled, before, after = sample_plotly_figure_for_embed(figure, 10_000)
    assert before == rows
    assert after <= 10_000
    trace = sampled["data"][0]
    length = len(trace["x"])
    assert length == len(trace["y"]) == len(trace["z"]) == len(trace["text"]) == len(trace["marker"]["color"])
    assert trace["y"][0] == trace["x"][0] * 2      # 同一下标规则，未错位
    assert trace["text"][0] == f"第{trace['x'][0]}行"
    # 原始 figure 不被就地修改
    assert len(figure["data"][0]["x"]) == rows


def test_plotly_huge_scatter3d_reports_sampling_and_keeps_full_json(tmp_path):
    rows = 60_000
    workspace = _workspace(tmp_path, rows)
    tools = {tool.name: tool for tool in build_tools(workspace)}

    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "x", "y": "y", "z": "z", "color": "group",
    }))

    assert result["sampling"]["applied"] is True
    assert result["sampling"]["original_points"] >= rows
    assert result["sampling"]["embedded_points"] <= 50_000

    json_path = Path(result["plotly_json"])
    # plotly.py 把 numpy 数组序列化成 base64 typed array，比较长度前先解码
    figure = _decode_plotly_typed_arrays(json.loads(json_path.read_text(encoding="utf-8")))
    assert {trace["type"] for trace in figure["data"]} == {"scatter3d"}
    # 完整数据未被裁剪：JSON 产物仍是全量
    assert sum(len(trace["x"]) for trace in figure["data"]) == rows
    assert "抽样" in Path(result["html"]).read_text(encoding="utf-8")


def test_small_scatter3d_is_not_sampled(tmp_path):
    rows = 500
    workspace = _workspace(tmp_path, rows)
    tools = {tool.name: tool for tool in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "x", "y": "y", "z": "z",
    }))
    assert "sampling" not in result
