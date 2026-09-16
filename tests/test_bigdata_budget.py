"""大数据图表的性能与体积预算。

这些数字此前只是"实测记录"（写在做过的报告与工程笔记里），没有任何东西拦得住
回归：一次不小心的 `.tolist()` 或丢掉抽样就能让 30 万行的图表从 1 秒变 30 秒、
从 200KB 变 10MB。这里把它们固化成断言。

预算怎么定的：取实测值的**数量级余量**（不是精确值）。CI runner 比本机慢，
所以时间预算给 3~5 倍；体积与点数预算是确定性的，可以卡得紧一些。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace

ROWS = 300_000


@pytest.fixture(scope="module")
def big_workspace(tmp_path_factory) -> DataWorkspace:
    """30 万行有结构的数据（分类别不同分布 + 少量极端值），模块内共用。"""
    directory = tmp_path_factory.mktemp("budget")
    rng = np.random.default_rng(2026)
    rows = ROWS
    categories = rng.choice(["食品", "电子产品", "办公用品", "家具"], rows)
    sales = rng.gamma(2.6, 220, rows) * np.where(categories == "电子产品", 1.4, 1.0)
    profit = sales * rng.normal(0.28, 0.12, rows)
    region = rng.choice(["华东", "华南", "华北", "西南"], rows)
    frame = pd.DataFrame({
        "销售额": np.round(sales, 2),
        "利润": np.round(profit, 2),
        "类别": categories,
        "地区": region,
    })
    # 少量极端值：触发"主体尺度 + 视图外记录"这条最复杂的路径
    frame.loc[:40, "销售额"] = frame.loc[:40, "销售额"] * 24
    frame.loc[41:80, "利润"] = frame.loc[41:80, "利润"] * 30
    source = directory / "budget.csv"
    frame.to_csv(source, index=False)
    workspace = DataWorkspace(directory / "runs", session_id="budget")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def _tools(workspace: DataWorkspace) -> dict:
    return {tool.name: tool for tool in build_tools(workspace)}


def _timed(tool, payload: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    result = json.loads(tool.invoke(payload))
    return result, time.perf_counter() - started


@pytest.mark.parametrize(
    ("engine", "label"),
    [("plotly", "Plotly"), ("echarts", "ECharts")],
)
def test_density_scatter_stays_within_budget(big_workspace, engine, label):
    """30 万行散点（密度视图）：生成 <12s、HTML <2MB、JSON <3MB。

    时间预算为什么给这么松：本机实测 2.4~2.5s（两引擎），但 CI runner 在同等工作
    上实测慢 4~5 倍（LTTB 那组用例：本机 0.18s / CI 0.94s），12s 是"灾难性回归"
    护栏而不是性能指标。**真正防回归的是体积与点数上限**（确定性、与机器无关）
    以及 LTTB 的同机规模比例用例。
    """
    tools = _tools(big_workspace)
    result, elapsed = _timed(tools["create_visualization"], {
        "chart_type": "scatter", "x": "销售额", "y": "利润", "color": "类别",
        "title": f"预算检查 {label}", "chart_engine": engine,
    })
    assert result.get("density_view", {}).get("applied") is True, result
    html_kb = Path(result["html"]).stat().st_size / 1024
    json_kb = Path(result[f"{engine}_json"]).stat().st_size / 1024
    print(f"\n{label}: {elapsed:.2f}s · HTML {html_kb:.0f}KB · JSON {json_kb:.0f}KB")
    assert elapsed < 12.0, f"{label} 密度图生成耗时 {elapsed:.2f}s（护栏 12s）"
    assert html_kb < 2048, f"{label} HTML {html_kb:.0f}KB（预算 2048KB）"
    assert json_kb < 3072, f"{label} JSON {json_kb:.0f}KB（预算 3072KB）"


def test_large_line_chart_stays_within_budget(big_workspace):
    """30 万行折线：LTTB 之后 HTML 不应随行数线性膨胀。"""
    tools = _tools(big_workspace)
    result, elapsed = _timed(tools["create_visualization"], {
        "chart_type": "line", "x": "地区", "y": "销售额", "aggregation": "mean",
        "title": "预算检查 折线",
    })
    html_kb = Path(result["html"]).stat().st_size / 1024
    print(f"\n折线(聚合后): {elapsed:.2f}s · HTML {html_kb:.0f}KB")
    assert elapsed < 10.0
    assert html_kb < 1024


def test_histogram_and_box_use_server_side_stats(big_workspace):
    """直方图/箱线图在 5 万行以上走服务端统计：耗时与体积都不随行数膨胀。"""
    tools = _tools(big_workspace)
    for chart_type, payload in (
        ("histogram", {"chart_type": "histogram", "x": "销售额", "title": "预算 直方图"}),
        ("box", {"chart_type": "box", "x": "类别", "y": "销售额", "title": "预算 箱线图"}),
    ):
        result, elapsed = _timed(tools["create_visualization"], payload)
        html_kb = Path(result["html"]).stat().st_size / 1024
        print(f"\n{chart_type}: {elapsed:.2f}s · HTML {html_kb:.0f}KB")
        assert elapsed < 8.0, f"{chart_type} 耗时 {elapsed:.2f}s"
        assert html_kb < 512, f"{chart_type} HTML {html_kb:.0f}KB"


def test_thumbnails_json_is_sampled_not_full(big_workspace):
    """缩略图用的 JSON 必须抽样到 2500 点级，否则产物页会卡死。"""
    from data_agent.chart_sampling import _THUMB_MAX_POINTS, _sample_plotly_figure_for_thumb

    figure = {"data": [{
        "type": "scattergl", "mode": "markers",
        "x": list(range(ROWS)), "y": [float(i % 100) for i in range(ROWS)],
    }]}
    _sample_plotly_figure_for_thumb(figure)
    assert len(figure["data"][0]["x"]) <= _THUMB_MAX_POINTS
    assert len(figure["data"][0]["y"]) == len(figure["data"][0]["x"])
