"""产物去重键：括号里的限定词必须参与区分。

实测缺陷：去重键把描述在第一个括号处切断，"销售额与利润分布（整体）"与
"销售额与利润分布（按品类）"撞成同一个键 + 同引擎，于是后一张把前一张顶掉——
演示会话里那张 30 万行分面密度图在界面上直接消失（磁盘上还在），用户只会
觉得"我生成的图少了几张、数据显示有问题"。
"""

from __future__ import annotations

from pathlib import Path

from data_agent.registry import _curate_artifacts


def _viz(tmp_path: Path, name: str, description: str, engine: str = "plotly") -> dict[str, str]:
    """造一个"真实存在"的可视化产物条目：引擎靠同目录的姊妹 JSON 判定。"""
    html = tmp_path / name
    html.write_text("<html></html>", encoding="utf-8")
    stem = name.removesuffix(".html")
    sibling = tmp_path / f"{stem}.{engine}.json"
    sibling.write_text("{}", encoding="utf-8")
    return {
        "kind": "visualization",
        "name": name,
        "description": description,
        "path": str(html),
    }


def test_parenthetical_qualifier_keeps_charts_distinct(tmp_path):
    charts = [
        _viz(tmp_path, "散点图_1.html", "销售额与利润分布（整体）"),
        _viz(tmp_path, "散点图_2.html", "销售额与利润分布（按品类）"),
    ]
    kept = _curate_artifacts(charts)
    assert {item["name"] for item in kept} == {"散点图_1.html", "散点图_2.html"}


def test_identical_description_still_collapses(tmp_path):
    """同一张图重画（描述完全一致）仍只保留最新的一张。"""
    charts = [
        _viz(tmp_path, "散点图_1.html", "各地区销售额合计"),
        _viz(tmp_path, "散点图_2.html", "各地区销售额合计"),
    ]
    kept = _curate_artifacts(charts)
    assert [item["name"] for item in kept] == ["散点图_2.html"]


def test_same_title_different_engine_kept(tmp_path):
    """双引擎同标题是两张独立图（历史缺陷：互相顶掉只剩一张）。"""
    charts = [
        _viz(tmp_path, "散点图_1.html", "销售额与利润分布（整体）", engine="plotly"),
        _viz(tmp_path, "散点图_2.html", "销售额与利润分布（整体）", engine="echarts"),
    ]
    kept = _curate_artifacts(charts)
    assert {item["name"] for item in kept} == {"散点图_1.html", "散点图_2.html"}
