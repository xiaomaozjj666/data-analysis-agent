"""数据画像仪表盘模块测试：KPI 计算 / 质量剖析 / HTML 组装 / 导出端点。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from data_agent.dashboard import (
    _collect_charts,
    _is_wide_chart,
    _rehydrate_js,
    build_dashboard_html,
    compute_kpis,
    profile_quality,
)
from data_agent.echarts_engine import _JsFunction
from data_agent.workspace import DataWorkspace


def _make_workspace(tmp_path: Path, df: pd.DataFrame) -> DataWorkspace:
    workspace = DataWorkspace(tmp_path / "runs", session_id="dash_test")
    workspace.save_upload("data.csv", b"placeholder")
    workspace._df = df  # 直接注入测试数据，绕过文件加载
    workspace._profile_cache.clear()
    return workspace


@pytest.fixture
def dirty_df() -> pd.DataFrame:
    """带全套质量问题的数据：缺失/重复行/主键冲突/极端离群/负值/常量列。"""
    rng = np.random.default_rng(7)
    n = 60
    df = pd.DataFrame({
        "订单编号": [f"A{i:03d}" for i in range(n)],
        "地区": rng.choice(["华东", "华南"], n),
        "销量": rng.integers(1, 10, n).astype(float),
        "营收": rng.normal(1500, 100, n).round(2),
    })
    df.loc[5, "营收"] = 999_999.0  # 极端离群（远超 3 倍 IQR）
    df.loc[6, "营收"] = -50.0  # 少量负值
    df.loc[10:14, "销量"] = np.nan  # 缺失
    df.loc[8, "订单编号"] = df.loc[7, "订单编号"]  # 主键冲突
    df = pd.concat([df, df.iloc[[3]]], ignore_index=True)  # 完全重复行
    df["数据源"] = "CSV"  # 常量列
    return df


def test_profile_quality_detects_all_issue_types(dirty_df):
    issues = profile_quality(dirty_df)
    tags = {i["tag"] for i in issues}
    assert {"缺失", "重复行", "主键冲突", "极端离群", "负值疑点", "常量列"} <= tags
    # 每张卡结构完整且带修复建议
    for issue in issues:
        assert issue["level"] in {"high", "mid", "low"}
        assert issue["target"] and issue["desc"] and issue["fix"]
    # 主键冲突定位到编号列，极端离群定位到营收
    assert any(i["tag"] == "主键冲突" and i["target"] == "订单编号" for i in issues)
    assert any(i["tag"] == "极端离群" and i["target"] == "营收" for i in issues)


def test_profile_quality_clean_data_returns_empty():
    df = pd.DataFrame({
        "city": ["北京", "上海", "广州", "深圳"] * 5,
        "amount": [float(i * 7 % 13 + 1) for i in range(20)],
    })
    assert profile_quality(df) == []
    assert profile_quality(pd.DataFrame()) == []


def test_profile_quality_caps_issue_cards():
    # 15 个疑似主键列全部存在重复键，触发告警卡上限聚合
    df = pd.DataFrame({f"key{i}_id": ["a", "a", "b", "c", "d", "e", "f", "g"] for i in range(15)})
    issues = profile_quality(df)
    assert len(issues) == 12
    assert issues[-1]["tag"] == "更多"


def test_compute_kpis_semantic_colors(dirty_df):
    kpis = compute_kpis(dirty_df, chart_count=3, issue_count=6)
    assert len(kpis) == 6
    by_label = {k["label"]: k for k in kpis}
    assert by_label["记录数"]["val"] == "61"
    assert by_label["重复行"]["cls"] == "red"
    assert by_label["数据质量问题"]["cls"] == "red"
    # 干净数据：绿色通过态
    clean = compute_kpis(dirty_df.drop_duplicates().dropna(), chart_count=0, issue_count=0)
    clean_by_label = {k["label"]: k for k in clean}
    assert clean_by_label["重复行"]["cls"] == "green"
    assert clean_by_label["数据质量问题"]["cls"] == "green"


def test_rehydrate_js_restores_functions():
    node = {
        "formatter": "function(p){return p.name;}",
        "plain": "普通文本",
        "nested": [{"fn": "function(x) { return x * 2; }"}],
    }
    out = _rehydrate_js(node)
    assert isinstance(out["formatter"], _JsFunction)
    assert out["plain"] == "普通文本"
    assert isinstance(out["nested"][0]["fn"], _JsFunction)


def test_collect_charts_skips_non_visualization_and_non_html(tmp_path, dirty_df):
    """_collect_charts 应跳过非 visualization 与非 .html 的产物（205 分支）。"""
    ws = _make_workspace(tmp_path, dirty_df)
    (ws.artifacts_dir / "notes.txt").write_text("文本", encoding="utf-8")
    ws.register_artifact(ws.artifacts_dir / "notes.txt", "document", "说明")
    (ws.artifacts_dir / "chart.png").write_bytes(b"\x89PNG")
    ws.register_artifact(ws.artifacts_dir / "chart.png", "image", "截图")
    (ws.artifacts_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    ws.register_artifact(ws.artifacts_dir / "data.csv", "dataset", "数据")

    charts = _collect_charts(ws)
    assert charts == []


def test_collect_charts_prefers_echarts_and_skips_broken(tmp_path, dirty_df):
    ws = _make_workspace(tmp_path, dirty_df)
    # echarts 图
    (ws.artifacts_dir / "chart_a.html").write_text("<html></html>", encoding="utf-8")
    (ws.artifacts_dir / "chart_a.echarts.json").write_text(
        json.dumps({"series": [{"type": "bar"}]}), encoding="utf-8"
    )
    ws.register_artifact(ws.artifacts_dir / "chart_a.html", "visualization", "柱状图A")
    # plotly 图
    (ws.artifacts_dir / "chart_b.html").write_text("<html></html>", encoding="utf-8")
    (ws.artifacts_dir / "chart_b.plotly.json").write_text(
        json.dumps({"data": [], "layout": {}}), encoding="utf-8"
    )
    ws.register_artifact(ws.artifacts_dir / "chart_b.html", "visualization", "柱状图B")
    # 损坏的 sidecar：跳过不报错
    (ws.artifacts_dir / "chart_c.html").write_text("<html></html>", encoding="utf-8")
    (ws.artifacts_dir / "chart_c.echarts.json").write_text("{broken", encoding="utf-8")
    ws.register_artifact(ws.artifacts_dir / "chart_c.html", "visualization", "损坏图C")

    charts = _collect_charts(ws)
    assert [c["engine"] for c in charts] == ["echarts", "plotly"]
    assert charts[0]["title"] == "柱状图A"


def test_build_dashboard_html_full_document(tmp_path, dirty_df):
    ws = _make_workspace(tmp_path, dirty_df)
    (ws.artifacts_dir / "chart_a.html").write_text("<html></html>", encoding="utf-8")
    (ws.artifacts_dir / "chart_a.echarts.json").write_text(
        json.dumps({
            "series": [{"type": "bar"}],
            "tooltip": {"formatter": "function(p){return p.name;}"},
        }),
        encoding="utf-8",
    )
    ws.register_artifact(ws.artifacts_dir / "chart_a.html", "visualization", "销量柱状图")

    mock_bundle = ws.artifacts_dir / "echarts.min.js"
    mock_bundle.write_text("/* mock echarts */", encoding="utf-8")
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=mock_bundle):
        html = build_dashboard_html(ws)

    # 结构：标题 / 主题按钮 / KPI / 图表 / 质量告警 / 口径说明
    assert "数据画像仪表盘" in html
    assert 'id="theme-toggle"' in html
    assert "记录数" in html and "数据质量问题" in html
    assert "销量柱状图" in html and 'id="chart-0"' in html
    assert "主键冲突" in html and "极端离群" in html
    assert "口径说明" in html
    # 脚本：bundle 内联、多实例数组、暗色主题脚本、函数已去引号内联
    assert "/* mock echarts */" in html
    assert "window.__echartsInstances" in html
    assert "applyTheme" in html
    assert '"function(p){return p.name;}"' not in html
    assert "function(p){return p.name;}" in html


def test_build_dashboard_html_without_charts(tmp_path, dirty_df):
    ws = _make_workspace(tmp_path, dirty_df)
    html = build_dashboard_html(ws)
    assert "尚无图表产物" in html
    # 无图表时不内联任何引擎 bundle，也无 Plotly 适配脚本
    assert "echarts.min.js" not in html
    assert "Plotly.relayout" not in html


def test_dashboard_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent import api
    from data_agent.config import AgentSettings
    from data_agent.storage import LocalSessionStorage

    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(api_key="not-used", provider="deepseek", runs_dir=runs_dir)
    registry = api.SessionRegistry(
        runs_dir, settings.max_active_sessions, settings.session_ttl_hours,
        storage=LocalSessionStorage(),
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(api, "registry", registry)
    client = TestClient(api.app)

    uploaded = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    ).json()

    response = client.get(f"/api/sessions/{uploaded['id']}/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" in response.headers["content-disposition"]
    assert "数据画像仪表盘" in response.text

    missing = client.get("/api/sessions/does-not-exist/dashboard")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 补充分支：缺失列聚合 / IQR 边界 / 宽图判定 / bundle 脚本标签
# ---------------------------------------------------------------------------


def test_profile_quality_aggregates_many_missing_columns():
    """缺失列超过 4 个时应聚合为一张"其余缺失"卡。"""
    df = pd.DataFrame(
        {f"col_{i}": [None, None, "x", "y"] for i in range(6)} | {"keep": [1, 2, 3, 4]}
    )
    issues = profile_quality(df)
    tags = [(i["tag"], i["target"]) for i in issues]
    assert any(tag == "缺失" and "另" in target for tag, target in tags)


def test_profile_quality_skips_zero_iqr_numeric_columns():
    """数值列四分位距为 0（常量）时不应触发离群检测。"""
    df = pd.DataFrame({"const": [5, 5, 5, 5, 5, 5, 5, 5, 5, 5], "v": range(10)})
    issues = profile_quality(df)
    # 常量列应触发"常量列"告警，但不应有"极端离群"
    assert not any(i["tag"] == "极端离群" for i in issues)
    assert any(i["tag"] == "常量列" for i in issues)


def test_is_wide_chart_variants():
    """宽图判定：plotly 恒 False；echarts SPLOM（grid>4）与 3D 散点 True。"""
    assert _is_wide_chart({"engine": "plotly", "fig": {}}) is False
    assert (
        _is_wide_chart(
            {"engine": "echarts", "option": {"grid": [{}] * 6, "series": [{"type": "scatter"}]}}
        )
        is True
    )
    assert (
        _is_wide_chart(
            {"engine": "echarts", "option": {"grid": [{}], "series": [{"type": "scatter3D"}]}}
        )
        is True
    )
    assert _is_wide_chart({"engine": "echarts", "option": {"series": [{"type": "bar"}]}}) is False


def test_bundle_script_tag_falls_back_to_cdn_on_read_error(tmp_path, monkeypatch):
    """bundle 文件存在但读取失败时应回退 CDN 直引。"""
    from pathlib import Path as RealPath

    from data_agent.dashboard import _bundle_script_tag

    class FakeWorkspace:
        def __init__(self):
            self.artifacts_dir = tmp_path

        def ensure_echarts_bundle(self):
            bundle = self.artifacts_dir / "echarts.min.js"
            bundle.write_text("/* echarts */", encoding="utf-8")
            return bundle

        def ensure_echarts_gl_bundle(self):
            bundle = self.artifacts_dir / "echarts-gl.min.js"
            bundle.write_text("/* gl */", encoding="utf-8")
            return bundle

        def ensure_plotly_bundle(self):
            bundle = self.artifacts_dir / "plotly.min.js"
            bundle.write_text("/* plotly */", encoding="utf-8")
            return bundle

    def failing_read(self, *a, **k):
        raise OSError("io error")

    monkeypatch.setattr(RealPath, "read_text", failing_read)
    ws = FakeWorkspace()
    assert "cdn.jsdelivr.net/npm/echarts" in _bundle_script_tag(ws, "echarts")
    assert "echarts-gl" in _bundle_script_tag(ws, "echarts-gl")
    assert "cdn.plot.ly" in _bundle_script_tag(ws, "plotly")


def test_build_dashboard_html_with_plotly_and_3d_charts(tmp_path, dirty_df):
    """同时包含 Plotly 图与 ECharts 3D 图时：bundle 按需内联、脚本正确组装。"""
    ws = _make_workspace(tmp_path, dirty_df)
    # ECharts 3D 图（触发 echarts-gl bundle + 宽图）
    (ws.artifacts_dir / "chart_3d.html").write_text("<html></html>", encoding="utf-8")
    (ws.artifacts_dir / "chart_3d.echarts.json").write_text(
        json.dumps({"series": [{"type": "scatter3D", "data": [[1, 2, 3]]}], "grid": [{}] * 5}),
        encoding="utf-8",
    )
    ws.register_artifact(ws.artifacts_dir / "chart_3d.html", "visualization", "3D 散点")
    # Plotly 图（触发 plotly bundle + newPlot 脚本）
    (ws.artifacts_dir / "chart_plotly.html").write_text("<html></html>", encoding="utf-8")
    (ws.artifacts_dir / "chart_plotly.plotly.json").write_text(
        json.dumps({"data": [{"type": "bar"}], "layout": {"width": 800, "height": 600}}),
        encoding="utf-8",
    )
    ws.register_artifact(ws.artifacts_dir / "chart_plotly.html", "visualization", "柱状图")

    mock_bundle = ws.artifacts_dir / "echarts.min.js"
    mock_bundle.write_text("/* mock echarts */", encoding="utf-8")
    mock_gl = ws.artifacts_dir / "echarts-gl.min.js"
    mock_gl.write_text("/* mock gl */", encoding="utf-8")
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=mock_bundle), \
         patch.object(DataWorkspace, "ensure_echarts_gl_bundle", return_value=mock_gl):
        html = build_dashboard_html(ws)

    assert "/* mock echarts */" in html
    assert "/* mock gl */" in html
    assert "Plotly.newPlot" in html
    # plotly 存档里的固定宽高应被删除（autosize 自适应）
    assert '"width":800' not in html
    assert 'card full' in html  # 3D 图占满整行
    assert "scatter3D" in html
    # 回归保护（图例溢出/遮挡 bug）：dashboard 内 Plotly 图必须横向排布
    # 在绘图区下方，窄窗口/窄卡片下图例不溢出、不遮挡数据。
    assert "l.legend.orientation='h'" in html
    # modebar 按钮提示中文本地化（plotly 自带 locale 不含 zh-CN）
    assert "下载为 PNG 图片" in html
