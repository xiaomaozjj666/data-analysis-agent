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

import numpy as np
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


def test_echarts_grouped_line_dedupes_and_sorts_time_axis(tmp_path):
    """回归：color 分组聚合后类目轴必须去重且按时间排序。

    历史 bug：x_levels 取原始行出现顺序导致月份轴乱序（01→02→04→06→05→03），
    且类目轴直接取 x×color 长表导致每月重复 N 次、趋势线变阶梯平台。
    """
    # 月份故意乱序出现，复现原始数据行序 ≠ 时间序的场景
    months = ["2026-01", "2026-02", "2026-04", "2026-06", "2026-05", "2026-03"]
    df = pd.DataFrame({
        "month": [m for m in months for _ in range(2)],
        "region": ["华东", "华南"] * 6,
        "sales": list(range(100, 112)),
    })
    ws = _make_workspace(tmp_path, df)
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        tools = {t.name: t for t in build_tools(ws)}
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "line", "x": "month", "y": "sales", "color": "region",
            "aggregation": "sum", "chart_engine": "echarts",
        }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    axis = option["xAxis"][0]["data"]
    # 类目轴：去重后 6 个月份、严格时间升序
    assert axis == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    # 每个系列值与类目一一对应，无重复复制
    for series in option["series"]:
        assert len(series["data"]) == 6


def test_echarts_grouped_bar_datetime_axis_aligns_values(tmp_path):
    """回归：datetime x 轴 + color 分组时 reindex 用原始值，系列值不能全 NaN。"""
    df = pd.DataFrame({
        "month": pd.to_datetime(["2026-02-01", "2026-01-01"] * 2),
        "region": ["华东", "华东", "华南", "华南"],
        "sales": [10, 20, 30, 40],
    })
    ws = _make_workspace(tmp_path, df)
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        tools = {t.name: t for t in build_tools(ws)}
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "bar", "x": "month", "y": "sales", "color": "region",
            "aggregation": "sum", "chart_engine": "echarts",
        }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["xAxis"][0]["data"] == ["2026-01", "2026-02"]
    # 时间升序对齐后：华东=[20,10]、华南=[40,30]，不允许出现 None
    values = {s["name"]: s["data"] for s in option["series"]}
    assert values["华东"] == [20, 10]
    assert values["华南"] == [40, 30]


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
    """散点图：支持 size 维度、双轴 dataZoom；无离群时不分裂系列。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit", "size": "rating",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "scatter"
    assert any(z["type"] == "inside" for z in option["dataZoom"])
    # 样本均在 1.5 倍 IQR 内：不应出现离群点系列（系列名走业务标签映射，只校验结构）
    assert len(option["series"]) == 1
    assert "离群点" not in [s["name"] for s in option["series"]]


def test_echarts_scatter_highlights_iqr_outliers(tmp_path):
    """散点图离群高亮：超出 1.5 倍 IQR 的点拆到独立系列 + 边界参考线。"""
    df = pd.DataFrame({
        "x": [float(i % 20 + 1) for i in range(40)],
        "y": [float(50 + (i * 3) % 11) for i in range(40)],
    })
    df.loc[39, "y"] = 500.0  # 单点极端离群
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter", "x": "x", "y": "y", "chart_engine": "echarts",
        }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    names = [s["name"] for s in option["series"]]
    assert names == ["y", "离群点"]
    outlier_series = option["series"][1]
    assert len(outlier_series["data"]) == 1
    assert "markLine" in outlier_series  # 正常范围边界参考线
    assert option["legend"]["data"] == ["y", "离群点"]
    assert "IQR 检出 1 个离群点" in option["title"]["subtext"]


def test_echarts_scatter_skips_outlier_split_when_heavy_tailed(tmp_path):
    """离群占比超 20% 时视为重尾分布，不做高亮（避免满屏红点误导）。"""
    # 双峰：一半在 1~10，一半在 1000+，大量点超界
    df = pd.DataFrame({
        "x": [float(i) for i in range(30)],
        "y": [float(i % 10 + 1) for i in range(20)] + [1000.0 + i for i in range(10)],
    })
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter", "x": "x", "y": "y", "chart_engine": "echarts",
        }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert [s["name"] for s in option["series"]] == ["y"]


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


def test_echarts_scatter_matrix_is_true_splom(workspace, sample_df):
    """散点矩阵：N×N 多 grid 真 SPLOM，非对角散点 + 对角直方图。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_matrix", "dimensions": ["sales", "profit", "rating"],
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    # 3×3 网格：9 个 grid，对应 9 对坐标轴
    assert len(option["grid"]) == 9
    assert len(option["xAxis"]) == 9
    assert len(option["yAxis"]) == 9
    types = {s["type"] for s in option["series"]}
    assert types == {"scatter", "bar"}  # 非对角散点 + 对角直方图
    assert sum(1 for s in option["series"] if s["type"] == "bar") == 3  # 对角线 3 格


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
    # 必须存在 option 块：若回归导致 HTML 结构变化，此测试应失败而非跳过，
    # 否则 </script> 转义防护会失去守门作用。
    assert option_start != -1, "HTML 中未找到 var option 块，转义防护测试空转"
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


def test_preview_inlines_echarts_gl_bundle(tmp_path, sample_df):
    """预览内联 echarts-gl：相对路径 script 会被 CSP 拦截，bundle 存在时
    必须替换为内联源码，否则 scatter3D 无渲染器、3D 图空白。"""
    from data_agent.routers.artifacts import _inline_echarts_gl_bundle

    ws = DataWorkspace(tmp_path / "runs", session_id="api_gl")
    ws.save_upload("data.csv", b"x,y\n1,2\n")
    ws._df = sample_df
    (ws.artifacts_dir / "echarts-gl.min.js").write_text("/* mock gl */", encoding="utf-8")
    registry = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    _sid, record = registry.create(ws)

    html = (
        '<html><head><script src="echarts.min.js"></script>'
        '<script src="echarts-gl.min.js"></script></head><body></body></html>'
    )
    inlined = _inline_echarts_gl_bundle(record, html)
    assert "/* mock gl */" in inlined
    assert 'src="echarts-gl.min.js"' not in inlined
    # 主 bundle 标签不受影响，仍由 _inline_echarts_bundle 处理
    assert 'src="echarts.min.js"' in inlined


def test_preview_rewrites_missing_gl_bundle_to_cdn(tmp_path, sample_df):
    """gl bundle 缺失时降级：相对引用改写为 jsdelivr CDN 直引（CSP 已放行）。"""
    from data_agent.routers.artifacts import _inline_echarts_gl_bundle
    from data_agent.workspace import ECHARTS_GL_CDN_URL

    ws = DataWorkspace(tmp_path / "runs", session_id="api_gl_cdn")
    ws.save_upload("data.csv", b"x,y\n1,2\n")
    ws._df = sample_df
    registry = api.SessionRegistry(tmp_path / "runs", max_sessions=10, ttl_hours=24)
    _sid, record = registry.create(ws)

    html = '<script src="echarts-gl.min.js"></script>'
    inlined = _inline_echarts_gl_bundle(record, html)
    assert ECHARTS_GL_CDN_URL in inlined
    assert 'src="echarts-gl.min.js"' not in inlined


def test_create_visualization_unescapes_html_entities_in_title(workspace, sample_df):
    """LLM 偶发在 title 里输出 HTML 实体（如 p&lt;0.001），入口处应还原
    为纯文本，否则图表标题与产物描述会把转义残留直接展示给用户。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit",
        "chart_engine": "echarts",
        "title": "销售额 vs 利润（r=0.9, p&lt;0.001）",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["title"]["text"] == "销售额 vs 利润（r=0.9, p<0.001）"
    assert "&lt;" not in option["title"]["text"]


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


def test_echarts_scatter_3d_uses_gl(workspace, sample_df):
    """z 轴有效时用 echarts-gl 真 3D 散点；缺 z 时降级 2D + 标注。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "sales", "y": "profit", "z": "rating",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "scatter3D"
    assert "grid3D" in option and "zAxis3D" in option
    # 旋转完全由用户拖拽控制，不开自动旋转（用户明确要求）
    assert option["grid3D"]["viewControl"]["autoRotate"] is False
    # 缩放距离有边界，防止缩到看不见或推进盒体内部
    vc = option["grid3D"]["viewControl"]
    assert 0 < vc["minDistance"] < vc["distance"] < vc["maxDistance"]
    # 3D 不支持 dataZoom 框选，工具栏不应出现死按钮
    assert "dataZoom" not in option["toolbox"]["feature"]
    assert "saveAsImage" in option["toolbox"]["feature"]
    # 解读文案的交互提示须匹配 3D（无框选，只有拖拽旋转）
    assert "拖拽旋转" in result["interpretation"]
    assert "框选" not in result["interpretation"]
    # HTML 里需引入 echarts-gl bundle（本地下载或 CDN 直引）
    html = Path(result["html"]).read_text(encoding="utf-8")
    assert "echarts-gl" in html


def test_echarts_scatter_3d_degrades_without_z(workspace, sample_df):
    """缺失 z 轴时 3D 散点降级为 2D 并标注。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "sales", "y": "profit",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "scatter"  # 2D scatter
    assert "2D" in option["title"]["text"] or "2D" in option["title"].get("subtext", "")


# === chart_type="auto" 自动选图测试 ===
from data_agent.tools.charts import _infer_chart_type  # noqa: E402


def _infer(df, **kwargs):
    base = dict(x=None, y=None, color=None, z=None, size=None, values=None,
                path_columns=None, dimensions=None, aggregation="none", top_n=None)
    base.update(kwargs)
    return _infer_chart_type(df, **base)


def test_infer_categorical_x_numeric_y_no_color_few_categories_is_pie():
    df = pd.DataFrame({
        "product": ["音箱", "键盘", "鼠标"],
        "sales": [100, 200, 150],
    })
    assert _infer(df, x="product", y="sales") == "pie"


def test_infer_categorical_x_numeric_y_with_color_is_bar():
    df = pd.DataFrame({
        "product": ["音箱", "键盘", "鼠标"],
        "sales": [100, 200, 150],
        "channel": ["线上", "门店", "线上"],
    })
    assert _infer(df, x="product", y="sales", color="channel") == "bar"


def test_infer_categorical_x_numeric_y_many_categories_is_bar():
    df = pd.DataFrame({
        "product": [f"p{i}" for i in range(30)],
        "sales": list(range(30)),
    })
    assert _infer(df, x="product", y="sales") == "bar"


def test_infer_two_numeric_is_scatter():
    df = pd.DataFrame({"sales": [1, 2, 3], "profit": [4, 5, 6]})
    assert _infer(df, x="sales", y="profit") == "scatter"


def test_infer_datetime_x_numeric_y_is_line():
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=5),
        "val": [1, 2, 3, 4, 5],
    })
    assert _infer(df, x="date", y="val") == "line"


def test_infer_date_string_x_is_line():
    df = pd.DataFrame({
        "date": ["2023-01-01", "2023-02-01", "2023-03-01"],
        "val": [1, 2, 3],
    })
    assert _infer(df, x="date", y="val") == "line"


def test_infer_numeric_distribution_is_histogram():
    df = pd.DataFrame({"v": list(range(40))})
    assert _infer(df, x="v") == "histogram"


def test_infer_categorical_x_only_is_bar_count():
    df = pd.DataFrame({"product": ["a", "b", "c"]})
    assert _infer(df, x="product") == "bar"


def test_infer_many_numeric_no_xy_is_correlation_heatmap():
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6], "c": [7, 8, 9]})
    assert _infer(df) == "correlation_heatmap"


def test_infer_path_columns_is_sunburst():
    df = pd.DataFrame({"r1": ["x", "y"], "r2": ["a", "b"]})
    assert _infer(df, path_columns=["r1", "r2"]) == "sunburst"


def test_infer_dimensions_three_is_scatter_matrix():
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4], "c": [5, 6]})
    assert _infer(df, dimensions=["a", "b", "c"]) == "scatter_matrix"


def test_infer_z_given_is_scatter_3d():
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4], "c": [5, 6]})
    assert _infer(df, x="a", y="b", z="c") == "scatter_3d"


def test_auto_create_visualization_resolves_and_reports_source(workspace, sample_df):
    """auto 模式：推断饼图（product 3 类 + sales，无 color），响应标注来源。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "auto", "x": "product", "y": "sales",
        "chart_engine": "echarts", "title": "产品占比",
    }))
    assert result["status"] == "ok"
    assert result["chart_type_source"] == "auto"
    assert result["chart_type"] == "pie"
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert option["series"][0]["type"] == "pie"
    # 自动按 x 聚合后每个 product 应只有 1 个扇区（无重复）
    assert len(option["series"][0]["data"]) == 3


def test_auto_duplicate_x_without_color_aggregates(workspace, sample_df):
    """auto 柱图：product 含重复行且无 color，应自动按 x 求和聚合。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "auto", "x": "product", "y": "sales",
        "chart_engine": "echarts",
    }))
    assert result["chart_type"] == "pie"  # product<=8 且无 color → pie
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert len(option["series"][0]["data"]) == 3  # 聚合后 3 个唯一 product


def test_explicit_chart_type_overrides_auto(workspace, sample_df):
    """显式 chart_type 应覆盖自动选择，且 source 标注为 explicit。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "bar", "x": "product", "y": "sales",
        "color": "channel", "chart_engine": "echarts",
    }))
    assert result["chart_type_source"] == "explicit"
    assert result["chart_type"] == "bar"


# === 时间类目标签格式化 + 折线主流交互（峰谷标记/均值线/降采样）测试 ===
from data_agent.echarts_engine import _echarts_line, _format_time_categories  # noqa: E402


def test_format_time_categories_monthly_datetime():
    """月度 datetime 列：只保留 YYYY-MM，去掉 00:00:00 冗余后缀。"""
    values = pd.Series(pd.date_range("2026-01-01", periods=4, freq="MS"))
    assert _format_time_categories(values) == ["2026-01", "2026-02", "2026-03", "2026-04"]


def test_format_time_categories_daily_datetime():
    """整日数据：显示到日，不带时分秒。"""
    values = pd.Series(pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]))
    assert _format_time_categories(values) == ["2026-01-05", "2026-01-06", "2026-01-07"]


def test_format_time_categories_string_with_midnight_suffix():
    """字符串类目 "2026-01-01 00:00:00"（groupby 后 str(Timestamp) 的产物）应还原为 YYYY-MM。"""
    values = pd.Series(["2026-01-01 00:00:00", "2026-02-01 00:00:00", "2026-03-01 00:00:00"])
    assert _format_time_categories(values) == ["2026-01", "2026-02", "2026-03"]


def test_format_time_categories_keeps_minutes_when_intraday():
    """带时间的日内数据：保留到分钟（秒全 0 时不显示秒）。"""
    values = pd.Series(pd.to_datetime(["2026-01-01 08:30", "2026-01-01 09:45"]))
    assert _format_time_categories(values) == ["2026-01-01 08:30", "2026-01-01 09:45"]


def test_format_time_categories_non_time_returns_none():
    """非时间列（普通类目 / 数值）不做格式化。"""
    assert _format_time_categories(pd.Series(["华东", "华南", "华北"])) is None
    assert _format_time_categories(pd.Series([1, 2, 3])) is None


def test_format_time_categories_mixed_values_returns_none():
    """抽样（前 20 个）通过但全量解析失败（末尾混入非日期值）：整体放弃格式化。"""
    values = pd.Series([f"2026-01-{d:02d}" for d in range(1, 21)] + ["未知"])
    assert _format_time_categories(values) is None


def test_echarts_line_time_axis_and_marks():
    """单系列折线：时间轴标签格式化 + hideOverlap + LTTB 降采样 + 峰谷/均值标记。"""
    df = pd.DataFrame({
        "date": [f"2026-{m:02d}-01 00:00:00" for m in range(1, 7)],
        "sales": [100, 200, 150, 180, 90, 110],
    })
    option = _echarts_line(df, x="date", y="sales", color=None,
                           aggregation="sum", title="月度趋势")
    assert option["xAxis"][0]["data"] == [f"2026-{m:02d}" for m in range(1, 7)]
    assert option["xAxis"][0]["axisLabel"]["hideOverlap"] is True
    s = option["series"][0]
    assert s["sampling"] == "lttb"
    assert s["showSymbol"] is True  # 6 个点 <= 60
    mark_types = {m["type"] for m in s["markPoint"]["data"]}
    assert mark_types == {"max", "min"}
    assert s["markLine"]["data"][0]["type"] == "average"


def test_echarts_line_multi_series_no_marks_and_aligned():
    """多系列折线：不加峰谷/均值标记（避免噪声），且 reindex 对齐不受标签格式化影响。"""
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-01", "2026-02-01", "2026-02-01"],
        "channel": ["线上", "门店", "线上", "门店"],
        "sales": [100, 80, 120, 90],
    })
    option = _echarts_line(df, x="date", y="sales", color="channel",
                           aggregation="sum", title="渠道趋势")
    assert len(option["series"]) == 2
    for s in option["series"]:
        assert "markPoint" not in s
        assert "markLine" not in s
        assert s["sampling"] == "lttb"
    # 类目轴去重后与各系列一一对应（reindex 用原始 x 值对齐）
    assert option["xAxis"][0]["data"] == ["2026-01", "2026-02"]
    values = {s["name"]: s["data"] for s in option["series"]}
    assert values["线上"] == [100, 120]
    assert values["门店"] == [80, 90]


def test_echarts_line_few_points_no_marks():
    """不足 5 个有效点：不加峰谷/均值标记（无解读价值）。"""
    df = pd.DataFrame({"date": ["2026-01-01", "2026-02-01", "2026-03-01"],
                       "sales": [100, 200, 150]})
    option = _echarts_line(df, x="date", y="sales", color=None,
                           aggregation="sum", title="短序列")
    s = option["series"][0]
    assert "markPoint" not in s
    assert "markLine" not in s


# ---------------------------------------------------------------------------
# 剩余分支：格式化辅助 / 解读各分支 / 图表生成器边界 / 序列化 / 渲染
# ---------------------------------------------------------------------------


def test_safe_value_scalars():
    from data_agent.echarts_engine import _safe_value

    assert _safe_value(None) is None
    assert _safe_value(float("nan")) is None
    assert _safe_value(np.int64(5)) == 5
    assert _safe_value(np.float64(1.5)) == 1.5
    assert _safe_value(np.bool_(True)) is True


def test_format_number_variants():
    from data_agent.echarts_engine import _format_number

    assert _format_number(None) == "—"
    assert _format_number(np.int64(1234)) == "1,234"
    assert _format_number("abc") == "abc"  # 非数值 → str 原样
    assert _format_number(float("nan")) == "—"
    assert _format_number(123456.7) == "123,457"  # >= 10000 → 千分位整数
    assert _format_number(3.14159) == "3.14"


def test_auto_interpret_swallows_errors(monkeypatch):
    from data_agent.echarts_engine import _auto_interpret

    def boom(**kwargs):
        raise RuntimeError("interpret failed")

    monkeypatch.setattr("data_agent.echarts_engine._interpret_impl", boom)
    result = _auto_interpret(
        pd.DataFrame({"a": [1]}), chart_type="bar", x="a", y="a",
        color=None, aggregation="sum", title="t",
    )
    assert result == ""


def test_interpret_trend_color_empty_pivot():
    from data_agent.echarts_engine import _interpret_trend

    df = pd.DataFrame({"x": ["a", "b"], "y": [1.0, 2.0], "c": [None, None]})
    text = _interpret_trend(df, chart_type="line", x="x", y="y", color="c",
                            aggregation="sum", title="趋势")
    assert "分组对比" in text


def test_interpret_trend_line_kink_and_fallback():
    from data_agent.echarts_engine import _interpret_trend

    # 3+ 点且拐点非端点 → 拐点描述（372-386）
    df = pd.DataFrame({"x": ["a", "b", "c"], "y": [1.0, 100.0, 3.0]})
    text = _interpret_trend(df, chart_type="line", x="x", y="y", color=None,
                            aggregation="sum", title="趋势")
    assert "拐点" in text
    # 少于 3 点 → 波动描述（387-390）
    df2 = pd.DataFrame({"x": ["a", "b"], "y": [1.0, 2.0]})
    text2 = _interpret_trend(df2, chart_type="line", x="x", y="y", color=None,
                             aggregation="sum", title="趋势")
    assert "波动" in text2


def test_interpret_pie_and_scatter_branches():
    from data_agent.echarts_engine import _interpret_pie, _interpret_scatter

    # pie 无数值列（396）
    assert "占比" in _interpret_pie(pd.DataFrame({"cat": ["a"]}), x="cat", title="占比")
    # pie total <= 0（400）
    assert "占比" in _interpret_pie(pd.DataFrame({"cat": ["a", "b"], "v": [0.0, -1.0]}), x="cat", title="占比")
    # scatter 非数值列（416）
    text = _interpret_scatter(pd.DataFrame({"x": ["a"], "y": ["b"]}), x="x", y="y", title="关系")
    assert "分布关系" in text
    # scatter corr NaN（422）
    text2 = _interpret_scatter(pd.DataFrame({"x": [1.0, 1.0], "y": [2.0, 2.0]}), x="x", y="y", title="关系")
    assert "展示" in text2


def test_interpret_heatmap_and_box_branches():
    from data_agent.echarts_engine import _interpret_box, _interpret_heatmap

    # correlation 且数值列 < 2 → generic（442）
    text = _interpret_heatmap(pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}), title="热图", is_correlation=True)
    assert "颜色深浅" in text
    # correlation 全常量 → pairs 空 → generic（452）
    text2 = _interpret_heatmap(pd.DataFrame({"a": [1.0, 1.0], "b": [2.0, 2.0]}), title="热图", is_correlation=True)
    assert "颜色深浅" in text2
    # box 组数 < 2 → generic（478）
    text3 = _interpret_box(pd.DataFrame({"g": ["a"], "v": [1.0]}), x="g", y="v", title="箱线")
    assert "箱体" in text3
    # box 正常分组 + 离群统计（485-487）
    df = pd.DataFrame({"g": ["a"] * 10 + ["b"] * 10, "v": list(range(10)) + list(range(10, 20))})
    text4 = _interpret_box(df, x="g", y="v", title="箱线")
    assert "中位数" in text4


def test_format_time_categories_seconds_and_empty():
    from data_agent.echarts_engine import _format_time_categories

    # 秒非零 → 保留到秒（548）
    values = pd.Series(pd.to_datetime(["2026-01-01 08:30:15", "2026-01-02 09:45:30"]))
    assert _format_time_categories(values) == ["2026-01-01 08:30:15", "2026-01-02 09:45:30"]
    # valid 全空 → None（539）
    assert _format_time_categories(pd.to_datetime([None, None])) is None


def test_echarts_bar_and_line_without_y(sample_df):
    from data_agent.echarts_engine import _echarts_bar, _echarts_line

    option = _echarts_bar(sample_df, x="product", y=None, color=None, aggregation="count", title="t")
    assert option["series"] == []  # 606

    option2 = _echarts_line(sample_df, x="product", y=None, color=None, aggregation="count", title="t", area=False)
    assert option2["series"] == []  # 731


def test_echarts_line_area_single_series_gradient(sample_df):
    from data_agent.echarts_engine import _echarts_line

    option = _echarts_line(sample_df, x="product", y="sales", color=None,
                           aggregation="sum", title="t", area=True)
    assert "areaStyle" in option["series"][0]  # 772


def test_echarts_scatter_with_color_groups(sample_df):
    from data_agent.echarts_engine import _echarts_scatter

    option = _echarts_scatter(sample_df, x="sales", y="profit", color="channel",
                              size="rating", title="t")
    assert len(option["series"]) == 2
    assert option["legend"]["data"] == ["线上", "门店"]
    assert option["series"][0]["large"] is True  # 810-834


def test_echarts_scatter_constant_axis_and_lower_mark(tmp_path):
    from data_agent.echarts_engine import _echarts_scatter

    # x 常量 → iqr==0 → continue（849）
    df = pd.DataFrame({"x": [1.0] * 10, "y": list(range(10))})
    option = _echarts_scatter(df, x="x", y="y", color=None, size=None, title="t")
    assert option["series"][0]["type"] == "scatter"

    # y 有下界离群（IQR>0）→ 下界 markLine 挂在离群点系列（863）
    df2 = pd.DataFrame({"x": list(range(10)), "y": [10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 30.0, 40.0, 50.0, -100.0]})
    option2 = _echarts_scatter(df2, x="x", y="y", color=None, size=None, title="t")
    outlier_series = next(s for s in option2["series"] if s["name"] == "离群点")
    assert outlier_series["markLine"]["data"]


def test_size_func_without_size():
    from data_agent.echarts_engine import _size_func

    assert _size_func(None) == 10  # 921


def test_echarts_pie_falls_back_to_count(sample_df):
    from data_agent.echarts_engine import _echarts_pie

    option = _echarts_pie(sample_df, x="product", values=None, y=None, title="t")
    assert option["series"][0]["type"] == "pie"
    assert len(option["series"][0]["data"]) == 3  # 963-964 计数分支


def test_echarts_histogram_empty_falls_back_to_bar(tmp_path):
    from data_agent.echarts_engine import _echarts_histogram

    df = pd.DataFrame({"x": [None, None]})
    option = _echarts_histogram(df, x="x", color=None, bins=30, title="t")
    assert option["series"] == []  # 1004 回退 bar


def test_echarts_box_branches(tmp_path):
    from data_agent.echarts_engine import _echarts_box

    # y=None → 回退 histogram（1050）；x 需为数值列（histogram 假设数值 x）
    df = pd.DataFrame({"g": [1.0, 2.0, 3.0]})
    option = _echarts_box(df, x="g", y=None, color=None, title="t", violin=False)
    assert "series" in option

    # 空组跳过（1061）：b 组全 NaN
    df2 = pd.DataFrame({"g": ["a", "a", "b"], "v": [1.0, 2.0, None]})
    option2 = _echarts_box(df2, x="g", y="v", color=None, title="t", violin=False)
    assert len(option2["xAxis"][0]["data"]) == 1

    # 离群点 > 100 抽样（1082）+ 离群 scatter 系列（1117）：
    # 85% 低位值 + 15% 极值（远超 upper 界）→ 150 个离群点
    df3 = pd.DataFrame(
        {"g": ["a"] * 1000, "v": [float(i % 7) for i in range(850)] + [10000.0] * 150}
    )
    option3 = _echarts_box(df3, x="g", y="v", color=None, title="t", violin=False)
    assert any(s["name"] == "离群值" for s in option3["series"])

    # 类别 > 10 → dataZoom（1150）
    df4 = pd.DataFrame({"g": [f"g{i % 12}" for i in range(120)], "v": [float(i % 10) for i in range(120)]})
    option4 = _echarts_box(df4, x="g", y="v", color=None, title="t", violin=False)
    assert "dataZoom" in option4


def test_echarts_heatmap_branches(tmp_path):
    from data_agent.echarts_engine import _echarts_heatmap

    # correlation 无数值列 → 空 option（1169）
    df = pd.DataFrame({"a": ["x", "y"]})
    option = _echarts_heatmap(df, x="a", y="a", values="a", title="t", is_correlation=True)
    assert option["title"]["text"] == "t"

    # 普通 heatmap 缺 values → 空 option（1182）
    option2 = _echarts_heatmap(df, x="a", y="a", values=None, title="t", is_correlation=False)
    assert option2["title"]["text"] == "t"

    # 全值相同 → vmin/vmax 扩展（1192）
    df2 = pd.DataFrame(
        {"x": ["a", "a", "b", "b"], "y": ["c", "d", "c", "d"], "v": [5.0, 5.0, 5.0, 5.0]}
    )
    option3 = _echarts_heatmap(df2, x="x", y="y", values="v", title="t", is_correlation=False)
    assert option3["visualMap"]["min"] < 5.0 < option3["visualMap"]["max"]


def test_echarts_scatter3d_with_size_and_color(sample_df):
    from data_agent.echarts_engine import _echarts_scatter3d, _JsFunction

    option = _echarts_scatter3d(
        sample_df, x="sales", y="profit", z="rating", color="channel", size="sales", title="t"
    )
    assert option["series"][0]["type"] == "scatter3D"
    assert option["legend"]["data"] == ["线上", "门店"]  # 1350
    assert isinstance(option["series"][0]["symbolSize"], _JsFunction)  # 1296-1297


def test_echarts_scatter_matrix_branches(tmp_path):
    from data_agent.echarts_engine import _echarts_scatter_matrix

    # 无数值维度 → 空 option（1385）
    df = pd.DataFrame({"s": ["a", "b"]})
    option = _echarts_scatter_matrix(df, dimensions=["s"], color=None, title="t")
    assert option["title"]["text"] == "t"

    # 维度 > 4 → 截断 + 提示（1389-1390）
    df2 = pd.DataFrame({f"v{i}": range(10) for i in range(6)})
    option2 = _echarts_scatter_matrix(df2, dimensions=[f"v{i}" for i in range(6)], color=None, title="t")
    assert "仅展示前 4 个" in option2["title"]["subtext"]

    # 样本 > 400 → 抽样提示（1400-1401）
    df3 = pd.DataFrame({"a": range(500), "b": range(500)})
    option3 = _echarts_scatter_matrix(df3, dimensions=["a", "b"], color=None, title="t")
    assert "随机抽样 400" in option3["title"]["subtext"]

    # color 分组 → legend（1494）
    df4 = pd.DataFrame({"a": range(50), "b": range(50), "c": ["x", "y"] * 25})
    option4 = _echarts_scatter_matrix(df4, dimensions=["a", "b"], color="c", title="t")
    assert option4["legend"]["data"] == ["x", "y"]


def test_echarts_sunburst_branches(tmp_path):
    from data_agent.echarts_engine import _echarts_sunburst

    # 无 path_columns → 空 option（1503）
    option = _echarts_sunburst(pd.DataFrame({"a": [1]}), path_columns=[], values=None, title="t")
    assert option["title"]["text"] == "t"

    # 无 values → 计数聚合（1511）
    df = pd.DataFrame({"region": ["East", "East", "West"], "product": ["A", "B", "C"]})
    option2 = _echarts_sunburst(df, path_columns=["region", "product"], values=None, title="t")
    assert option2["series"][0]["type"] == "sunburst"
    assert option2["series"][0]["data"]  # 层级树数据非空


def test_serialize_option_string_formatter():
    from data_agent.echarts_engine import _serialize_option

    option = {"tooltip": {"formatter": "function(p){return p.name;}"}}
    js = _serialize_option(option)
    assert "function(p){return p.name;}" in js  # 1979-1981


def test_json_default_fallback():
    from data_agent.echarts_engine import _json_default

    assert isinstance(_json_default(object()), str)  # 2000


def test_build_echarts_html_without_interpretation():
    from data_agent.echarts_engine import _build_echarts_html

    html = _build_echarts_html(title="t", option={"series": []}, script_src="x.js", interpretation="")
    assert "数据解读" not in html  # 2022
    html2 = _build_echarts_html(title="t", option={"series": []}, script_src="x.js", interpretation="解读", extra_script_src="gl.js")
    assert "数据解读" in html2
    assert 'src="gl.js"' in html2


def test_build_echarts_option_unknown_type(tmp_path):
    from data_agent.echarts_engine import _build_echarts_option

    df = pd.DataFrame({"a": [1, 2]})
    with pytest.raises(ValueError, match="暂不支持"):
        _build_echarts_option(
            df, chart_type="bogus", x="a", y=None, color=None, z=None, size=None,
            values=None, path_columns=None, dimensions=None, aggregation="none",
            title="t", bins=30,
        )  # 2094


def test_render_echarts_falls_back_to_cdn_without_bundles(tmp_path, monkeypatch):
    """bundle 缺失（离线）时应回退 CDN 直引（2137/2146）。"""
    from data_agent.echarts_engine import ECHARTS_CDN_URL, ECHARTS_GL_CDN_URL, _render_echarts

    class FakeWorkspace:
        def __init__(self, root):
            self.artifacts_dir = root / "artifacts"
            self.artifacts_dir.mkdir(parents=True)
            self._artifacts = []

        def ensure_echarts_bundle(self):
            return None

        def ensure_echarts_gl_bundle(self):
            return None

        def register_artifact(self, path, kind, description):
            self._artifacts.append({"name": Path(path).name, "kind": kind})

    ws = FakeWorkspace(tmp_path)
    df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6], "z": [7, 8, 9]})
    result = _render_echarts(
        ws, df, chart_type="scatter_3d", x="x", y="y", z="z", color=None, size=None,
        values=None, path_columns=None, dimensions=None, aggregation="none",
        title="3D", bins=30, display_title="3D", stem="三维散点_1",
    )
    assert result["status"] == "ok"
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert ECHARTS_CDN_URL in html_text
    assert ECHARTS_GL_CDN_URL in html_text

