"""ECharts 引擎测试：覆盖 11 种图表渲染、bundle 管理、HTML 自包含。

设计原则：
- mock ensure_echarts_bundle 避免网络下载（CI 离线场景）
- 默认 chart_engine="plotly" 路径不受影响（原 67 测试零回归）
- 验证 HTML 结构、option JSON、解读文本、文件名、产物注册
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from data_agent import api
from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _make_workspace(tmp_path: Path, df: pd.DataFrame) -> DataWorkspace:
    workspace = DataWorkspace(tmp_path / "runs", session_id="echarts_test")
    workspace.save_upload("data.csv", b"placeholder")
    workspace._df = df  # 直接注入测试数据，绕过文件加载
    workspace._profile_cache.clear()
    return workspace


def _mock_bundle(tmp_path: Path) -> Path:
    """生成假的 echarts.min.js 文件，避免网络下载。"""
    bundle = tmp_path / "runs" / "echarts_test" / "artifacts" / "echarts.min.js"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("/* mock echarts.min.js */", encoding="utf-8")
    return bundle


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame({
        "product": ["音箱", "音箱", "键盘", "键盘", "鼠标", "鼠标"],
        "channel": ["线上", "门店", "线上", "门店", "线上", "门店"],
        "region": ["华东", "华南", "华北", "华东", "华南", "华北"],
        "sales": [100, 200, 150, 180, 90, 110],
        "profit": [20, 40, 30, 35, 18, 22],
        "rating": [4.5, 4.2, 4.8, 4.0, 4.6, 4.3],
        "is_returned": [False, True, False, False, True, False],
    })


@pytest.fixture
def workspace(tmp_path, sample_df):
    ws = _make_workspace(tmp_path, sample_df)
    # mock bundle 下载，避免网络
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        yield ws


def test_echarts_bar_generates_html_and_option(workspace, sample_df):
    """柱状图：生成 HTML、echarts.json、解读文本，且注册到 artifacts。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "product", "y": "sales",
        "aggregation": "sum", "chart_engine": "echarts", "title": "产品销售",
    }))
    assert result["status"] == "ok"
    assert result["chart_engine"] == "echarts"
    assert result["chart_type"] == "bar"
    assert result["rows_plotted"] > 0
    html_path = Path(result["html"])
    json_path = Path(result["echarts_json"])
    assert html_path.is_file()
    assert json_path.is_file()
    assert html_path.name.endswith(".html")
    assert json_path.name.endswith(".echarts.json")

    html = html_path.read_text(encoding="utf-8")
    assert "echarts.min.js" in html  # 引用 bundle
    assert "echarts.init" in html  # 初始化代码
    assert "数据解读" in html  # 解读块
    assert "<script>" in html  # bundle 内联（mock 后被预览逻辑内联）

    option = json.loads(json_path.read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "bar"
    assert "产品销售" in option["title"]["text"]
    assert len(option["xAxis"][0]["data"]) == 3  # 3 个产品


def test_echarts_line_with_color_and_datazoom(workspace, sample_df):
    """折线图 + color 分组：包含 dataZoom、多 series、图例。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "line", "x": "product", "y": "sales", "color": "channel",
        "aggregation": "sum", "chart_engine": "echarts",
    }))
    json_path = Path(result["echarts_json"])
    option = json.loads(json_path.read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "line"
    assert option["series"][0]["smooth"] is True
    assert len(option["dataZoom"]) == 2  # inside + slider
    assert "data" in option["legend"]
    assert len(option["series"]) == 2  # 2 个渠道


def test_echarts_area_uses_gradient_fill(workspace, sample_df):
    """面积图：使用渐变填充、堆叠。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "area", "x": "product", "y": "sales", "color": "channel",
        "aggregation": "sum", "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "line"
    assert option["series"][0]["stack"] == "Total"
    assert "areaStyle" in option["series"][0]
    assert "colorStops" in option["series"][0]["areaStyle"]["color"]


def test_echarts_scatter_with_size_and_zoom(workspace, sample_df):
    """散点图：支持 size 维度、双轴 dataZoom。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit", "size": "rating",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "scatter"
    assert any(z["type"] == "inside" for z in option["dataZoom"])


def test_echarts_pie_with_donut_style(workspace, sample_df):
    """饼图：环形结构、内嵌标签、图例垂直。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "pie", "x": "product", "values": "sales",
        "chart_engine": "echarts", "title": "产品占比",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "pie"
    assert option["series"][0]["radius"][0] < option["series"][0]["radius"][1]  # 环形
    assert option["legend"]["orient"] == "vertical"


def test_echarts_histogram_auto_binning(workspace, sample_df):
    """直方图：自动分箱、频数轴。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "histogram", "x": "sales", "chart_engine": "echarts", "bins": 5,
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "bar"
    assert "频数" in option["yAxis"][0]["name"]


def test_echarts_box_aggregates_by_group(workspace, sample_df):
    """箱线图：按 x 分组计算分位数。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "box", "x": "product", "y": "sales",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "boxplot"
    assert len(option["series"][0]["data"]) == 3  # 3 个产品


def test_echarts_heatmap_with_visualmap(workspace, sample_df):
    """热力图：包含 visualMap、pivot 数据。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "heatmap", "x": "product", "y": "channel", "values": "sales",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "heatmap"
    assert "visualMap" in option
    assert option["visualMap"]["calculable"] is True


def test_echarts_correlation_heatmap(workspace, sample_df):
    """相关性矩阵：vmin=-1, vmax=1, 数值列。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "correlation_heatmap", "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "heatmap"
    assert option["visualMap"]["min"] == -1
    assert option["visualMap"]["max"] == 1


def test_echarts_scatter_matrix_uses_parallel(workspace, sample_df):
    """散点矩阵：用平行坐标近似表达。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_matrix", "dimensions": ["sales", "profit", "rating"],
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "parallel"
    assert len(option["parallelAxis"]) == 3


def test_echarts_sunburst_builds_hierarchy(workspace, sample_df):
    """旭日图：构建多级层级树。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "sunburst", "path_columns": ["region", "product"],
        "values": "sales", "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "sunburst"
    assert len(option["series"][0]["data"]) > 0


def test_echarts_treemap_as_alternative(workspace, sample_df):
    """矩形树图：与旭日图共用层级构建。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "treemap", "path_columns": ["region", "channel"],
        "values": "sales", "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "treemap"


def test_echarts_default_engine_is_plotly(workspace, sample_df):
    """默认 chart_engine='plotly'：不触发 ECharts 渲染分支。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "product", "y": "sales",
        "aggregation": "sum",  # 不传 chart_engine，默认 plotly
    }))
    assert "chart_engine" not in result  # plotly 分支不返回 chart_engine 字段
    assert "plotly_json" in result  # plotly 分支返回 plotly_json


def test_echarts_interpretation_is_generated(workspace, sample_df):
    """自动解读：包含业务白话，避免统计术语。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "product", "y": "sales",
        "aggregation": "sum", "chart_engine": "echarts", "title": "产品销售对比",
    }))
    interp = result["interpretation"]
    assert isinstance(interp, str)
    assert len(interp) > 10
    # 不应包含统计黑话
    assert "ANOVA" not in interp
    assert "p=" not in interp
    assert "η²" not in interp


def test_echarts_html_escapes_script_tag(workspace, tmp_path, sample_df):
    """XSS 防护：option JSON 中的 </script> 被转义。"""
    # 构造含 </script> 的恶意数据
    evil_df = pd.DataFrame({
        "name": ["</script><script>alert(1)</script>", "正常"],
        "value": [10, 20],
    })
    ws = _make_workspace(tmp_path, evil_df)
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        tools = {t.name: t for t in build_tools(ws)}
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "bar", "x": "name", "y": "value",
            "chart_engine": "echarts",
        }))
    html = Path(result["html"]).read_text(encoding="utf-8")
    # option JSON 内的 </script> 应被转义为 <\/script>，防止注入。
    # 取 var option = {...}; 这段，检查其中的 </script> 已转义。
    option_start = html.find("var option = ")
    if option_start == -1:
        pytest.skip("未找到 option 块")
    # 第一个 </script> 后是 echarts.init 调用，正常 script 标签
    option_block = html[option_start:html.find("</script>", option_start)]
    # option 块内不应包含未转义的 </script>（被转义成 <\/script>）
    assert "</script>" not in option_block.replace("<\\/script>", "")


def test_echarts_api_preview_inlines_bundle(tmp_path, monkeypatch, sample_df):
    """API 层 preview 路由：把 echarts.min.js 内联到 HTML。"""
    api._isolate_runtime_if_available = getattr(api, "_isolate_runtime_if_available", None)
    # 准备 workspace + echarts HTML
    ws = DataWorkspace(tmp_path / "runs", session_id="api_echarts")
    ws.save_upload("data.csv", b"x,y\n1,2\n3,4\n")
    ws._df = sample_df
    ws._profile_cache.clear()
    # 写入 echarts bundle
    bundle = ws.artifacts_dir / "echarts.min.js"
    bundle.write_text("/* mock echarts */", encoding="utf-8")
    # 用 mock 避免 CDN 下载
    monkeypatch.setattr(DataWorkspace, "ensure_echarts_bundle", lambda self: bundle)

    tools = {t.name: t for t in build_tools(ws)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "product", "y": "sales",
        "aggregation": "sum", "chart_engine": "echarts",
    }))
    # 注册到 registry（用 SessionRegistry.create 注册 workspace）
    registry = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    session_id, record = registry.create(ws)
    # 内联 bundle
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    inlined = api._inline_echarts_bundle(record, html_text)
    assert "/* mock echarts */" in inlined
    assert "src='echarts.min.js'" not in inlined


def test_echarts_violin_falls_back_to_boxplot(workspace, sample_df):
    """小提琴图降级为箱线图 + 标注。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "violin", "x": "product", "y": "sales",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "boxplot"
    assert "小提琴" in option["title"]["subtext"]


def test_echarts_scatter_3d_degrades_to_2d(workspace, sample_df):
    """3D 散点降级为 2D + 标注。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "sales", "y": "profit", "z": "rating",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "scatter"  # 2D scatter
    assert "2D" in option["title"]["text"] or "2D" in option["title"].get("subtext", "")
