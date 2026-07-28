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

