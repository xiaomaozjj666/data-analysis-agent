"""图表编辑端点对"按数值着色"图型（密度图/热力图）的处理。

回归背景：编辑端点原本给每条 trace 盲写 ``marker.color``，而 Plotly 的
heatmap 没有 marker 属性 → ``go.Figure`` 校验抛 ValueError → 用户点"编辑→
改色"得到 500 和一句看不懂的报错。密度视图（≥2 万行散点的分档热力图）正是
这种图型，因此这是新功能带出来的真实缺陷。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import _create_chart_session, _first_chart_artifact, _isolate_runtime

from data_agent import api
from data_agent.routers.artifacts import _MARKER_TRACE_TYPES, _VALUE_SCALED_TRACE_TYPES


def _density_like_session(tmp_path, monkeypatch, client) -> tuple[str, dict]:
    """把一个密度视图风格的热力图产物写进会话，返回 (session_id, artifact)。"""
    session_id = _create_chart_session(client)
    record = api.registry.get(session_id)
    artifacts = record.workspace.artifacts_dir
    stem = "密度图_9"
    figure = {
        "data": [
            {
                "type": "heatmap",
                "x": [0.5, 1.5],
                "y": [0.5, 1.5],
                "z": [[1, 2], [3, 4]],
                "colorscale": [[0.0, "#DCE8F2"], [0.5, "#4E79A7"], [1.0, "#1E4468"]],
                "zmin": -0.5,
                "zmax": 2.5,
                "showscale": True,
            },
            {"type": "scattergl", "x": [1.0], "y": [1.0], "mode": "markers",
             "marker": {"size": 5, "color": "rgba(225,87,89,0.85)"}},
        ],
        "layout": {
            "title": {"text": "销售额密度", "x": 0.01, "xanchor": "left",
                      "font": {"size": 22, "color": "#102a2a"}},
            "meta": {"density_view": {"bins": "2×2", "panels": 1,
                                      "colorscales": {"light": [[0.0, "#DCE8F2"]],
                                                      "dark": [[0.0, "#2C3B4D"]]}}},
        },
    }
    (artifacts / f"{stem}.plotly.json").write_text(
        json.dumps(figure, ensure_ascii=False), encoding="utf-8")
    record.workspace.register_artifact(artifacts / f"{stem}.html", "visualization", "销售额密度")
    (artifacts / f"{stem}.html").write_text("<html><title>销售额密度</title></body></html>",
                                            encoding="utf-8")
    session = client.get(f"/api/sessions/{session_id}").json()
    chart = next(item for item in session["artifacts"] if item["name"] == f"{stem}.html")
    return session_id, chart


def test_trace_type_whitelists_are_consistent():
    """按数值着色的图型不在 marker 白名单里，且被识别为"颜色即数据"。"""
    for trace_type in ("heatmap", "histogram2d", "histogram2dcontour", "contour",
                       "image", "splom", "densitymapbox", "heatmapgl"):
        assert trace_type not in _MARKER_TRACE_TYPES
        assert trace_type in _VALUE_SCALED_TRACE_TYPES
    for trace_type in ("scatter", "scattergl", "bar", "box", "violin", "histogram", "pie"):
        assert trace_type in _MARKER_TRACE_TYPES
        assert trace_type not in _VALUE_SCALED_TRACE_TYPES


def test_edit_density_chart_color_returns_422_with_explanation(tmp_path, monkeypatch):
    """密度图改色：返回 422 并解释原因，而不是 500 + 看不懂的校验错误。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id, chart = _density_like_session(tmp_path, monkeypatch, client)
    before = (Path(api.registry.get(session_id).workspace.artifacts_dir) /
              f"{chart['name']}").read_text(encoding="utf-8")

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"color": "#245C55"},
    )
    assert response.status_code == 422
    assert "分档着色" in response.json()["detail"]
    # 图型没有被写坏：JSON 里不应出现 marker 属性
    figure = json.loads((Path(api.registry.get(session_id).workspace.artifacts_dir) /
                         "密度图_9.plotly.json").read_text(encoding="utf-8"))
    heatmap = next(trace for trace in figure["data"] if trace["type"] == "heatmap")
    assert "marker" not in heatmap
    # HTML 未被覆盖（失败不落盘）
    after = (Path(api.registry.get(session_id).workspace.artifacts_dir) /
             f"{chart['name']}").read_text(encoding="utf-8")
    assert after == before


def test_edit_density_chart_title_keeps_style_and_updates_text(tmp_path, monkeypatch):
    """只改标题仍然可用，且标题原有样式字段不被抹掉。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id, chart = _density_like_session(tmp_path, monkeypatch, client)

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"title": "利润密度（重命名）"},
    )
    assert response.status_code == 200
    figure = json.loads((Path(api.registry.get(session_id).workspace.artifacts_dir) /
                         "密度图_9.plotly.json").read_text(encoding="utf-8"))
    title = figure["layout"]["title"]
    assert title["text"] == "利润密度（重命名）"
    assert title["x"] == 0.01 and title["xanchor"] == "left"
    assert title["font"]["size"] == 22


def test_edit_scatter_chart_still_recolors_every_marker_trace(tmp_path, monkeypatch):
    """普通散点图的改色行为保持不变（本次修复不得回归）。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = _create_chart_session(client)
    chart = _first_chart_artifact(client, session_id)

    response = client.put(
        f"/api/sessions/{session_id}/artifacts/{chart['name']}/edit",
        json={"color": "#123456"},
    )
    assert response.status_code == 200
    stem = chart["name"][: -len(".html")]
    figure = json.loads((Path(api.registry.get(session_id).workspace.artifacts_dir) /
                         f"{stem}.plotly.json").read_text(encoding="utf-8"))
    colored = [trace for trace in figure["data"]
               if str(trace.get("type", "scatter")) in _MARKER_TRACE_TYPES]
    assert colored, "会话里应当有可着色的轨迹"
    for trace in colored:
        assert trace["marker"]["color"] == "#123456"
