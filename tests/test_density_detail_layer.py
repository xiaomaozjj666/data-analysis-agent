"""密度视图的"放大看具体记录"图层与图内按钮。

密度图深度放大是栅格拉伸（格子变大），看不出单条记录；真正的动态重分箱要按
视口回服务端重算，下载下来的单文件 HTML 就失效了。替代方案是随图带一份
**分层抽样的原始点**：默认隐藏、一键叠加、名字里写明是抽样与条数。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from data_agent.density import sample_detail_points
from data_agent.tools import build_tools
from data_agent.tools.density_plotly import DENSITY_DETAIL_POINTS
from data_agent.workspace import DataWorkspace


def _workspace(tmp_path: Path, rows: int = 25_000) -> DataWorkspace:
    rng = np.random.default_rng(606)
    frame = pd.DataFrame({
        "sales": rng.gamma(2.4, 200, rows),
        "profit": rng.gamma(2.0, 160, rows),
        "group": np.where(rng.random(rows) < 0.995, "大组", "小组"),
    })
    source = tmp_path / "detail_points.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="detail_points")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def test_sample_detail_points_keeps_small_groups_and_range():
    rng = np.random.default_rng(7)
    big = rng.normal(0, 1, 20_000)
    small = rng.normal(0, 1, 30)  # 0.15% 的小组
    x = np.concatenate([big, small, [99.0]])  # 最后一个在视口外
    y = np.concatenate([big, small, [99.0]])
    groups = ["大组"] * 20_000 + ["小组"] * 30 + ["大组"]

    sx, sy = sample_detail_points(x, y, x_range=(-5, 5), y_range=(-5, 5),
                                  groups=groups, top=1_000)
    assert sx.size <= 1_000 + 30  # 小组全额保留时总额度可能略超下限配额
    assert not np.any(np.isclose(sx, 99.0)), "视口外的记录不应进入抽样"
    assert sx.size == sy.size

    # 无分组：等距抽样，不超过上限
    sx2, _ = sample_detail_points(x, y, x_range=(-5, 5), y_range=(-5, 5), top=500)
    assert sx2.size == 500

    # 点数本来就少于上限：原样返回
    sx3, _ = sample_detail_points(x[:100], y[:100], x_range=(-5, 5), y_range=(-5, 5), top=500)
    assert sx3.size == 100

    # 退化输入不抛异常
    assert sample_detail_points(np.array([]), np.array([]), x_range=(0, 1), y_range=(0, 1))[0].size == 0
    assert sample_detail_points(np.array([1.0]), np.array([1.0, 2.0]),
                                x_range=(0, 2), y_range=(0, 2))[0].size == 0
    # 分组长度与数据不符时退回等距（不静默错位）
    sx4, _ = sample_detail_points(x, y, x_range=(-5, 5), y_range=(-5, 5),
                                  groups=["只有一条"], top=200)
    assert sx4.size == 200


def test_density_chart_ships_hidden_detail_layer_with_toggle(tmp_path):
    """单面板密度图带"抽样原始点"图层（有分组时是分面，见下一个用例）。"""
    workspace = _workspace(tmp_path)
    tools = {tool.name: tool for tool in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit",
        "title": "细节图层",
    }))
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    detail = [trace for trace in figure["data"]
              if trace["type"] == "scattergl" and "抽样原始点" in str(trace.get("name"))]
    assert len(detail) == 1
    trace = detail[0]
    assert trace["visible"] is False, "默认必须隐藏：否则密度读数被点云盖住"
    assert "抽样" in trace["name"], "trace 名字要写明是抽样，不能被当成全量"

    menus = figure["layout"]["updatemenus"]
    labels = [button["label"] for button in menus[0]["buttons"]]
    assert labels == ["显示抽样原始点", "只看密度图"]
    index = figure["data"].index(trace)
    assert menus[0]["buttons"][0]["args"][1] == [index]
    assert menus[0]["buttons"][1]["args"][0] == {"visible": False}

    # 解读里要告诉用户这个按钮存在（否则没人会发现）
    html = Path(result["html"]).read_text(encoding="utf-8")
    assert "显示抽样原始点" in html


def test_faceted_density_has_no_single_detail_layer(tmp_path):
    """分面图不加全局细节图层：一层点铺到多个面板上会落错面板。"""
    workspace = _workspace(tmp_path)
    tools = {tool.name: tool for tool in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit", "color": "group",
        "title": "单组密度",
    }))
    figure = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    names = [str(trace.get("name")) for trace in figure["data"]]
    assert not any("抽样原始点" in name for name in names)
    assert "updatemenus" not in figure["layout"]


def test_detail_point_budget_is_bounded():
    assert 1_000 <= DENSITY_DETAIL_POINTS <= 20_000
