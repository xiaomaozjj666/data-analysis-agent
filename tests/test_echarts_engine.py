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


def test_echarts_large_scatter_uses_small_transparent_points(tmp_path):
    """大数据散点：缩小点径 + 半透明，避免后绘制的系列盖住其它分组
    颜色（视觉上只有一个颜色）。"""
    import numpy as np

    rng = np.random.default_rng(3)
    n = 12_000
    df = pd.DataFrame({
        "region": ["East", "West"] * (n // 2),
        "sales": rng.uniform(100, 5000, n),
        "profit": rng.uniform(10, 1500, n),
    })
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter", "x": "profit", "y": "sales",
            "color": "region", "chart_engine": "echarts",
        }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    series = option["series"]
    assert len(series) == 2                       # 两个区域的独立系列
    # 点径/透明度按点数分档（12k 落在最大一档）：小点 + 半透明 + 无描边
    assert series[0]["symbolSize"] == pytest.approx(3.2)
    assert series[0]["itemStyle"]["opacity"] == pytest.approx(0.45)
    assert series[0]["itemStyle"]["borderWidth"] == 0
    # 分组颜色保留（Tableau 色板按系列索引区分）
    assert series[0]["itemStyle"]["color"] != series[1]["itemStyle"]["color"]


def test_echarts_bar_value_label_uses_wan_formatter(tmp_path):
    """柱状图顶部数值标签用「万」缩写 formatter，而非 {c} 模板裸显
    浮点（70012.68000000001）与 10 万级长数字。"""
    df = pd.DataFrame({
        "region": ["A", "B", "C"],
        "sales": [54849.46, 70012.68, 109179.03],
    })
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "bar", "x": "region", "y": "sales",
            "aggregation": "sum", "chart_engine": "echarts",
        }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    formatter = option["series"][0]["label"]["formatter"]
    assert isinstance(formatter, str) and formatter.startswith("function(p)")
    assert "万" in formatter


def test_echarts_html_template_has_datazoom_adaptive_size():
    """ECharts 模板必须带 datazoom 自适应点径逻辑（大数据散点深度放大
    后仍清晰可辨，与 Plotly 分支语义一致）。"""
    from data_agent.echarts_engine import _ECHARTS_HTML_TEMPLATE

    assert "chart.on('datazoom'" in _ECHARTS_HTML_TEMPLATE
    assert "4 * Math.sqrt(zoom)" in _ECHARTS_HTML_TEMPLATE


def test_echarts_html_template_zoom_adaptive_is_event_driven():
    """缩放自适应点径必须事件驱动，且双轴归因 + restore 回底。

    - 旧实现每个 datazoom 事件调 chart.getOption()——它深拷贝整个
      option（30 万点散点的 series 数据几十 MB），滚轮缩放逐事件执行
      就是持续卡顿源；
    - 旧实现只读 dz[0]，只缩放 Y 轴时点径不更新（Plotly 分支取
      max(x,y) 因子，语义不一致）；
    - 自适应按 dataZoom 显式 id 门控，箱线图离群点等同为 scatter
      数值 symbolSize 的系列不被散点点径改写。
    """
    from data_agent.echarts_engine import _ECHARTS_HTML_TEMPLATE

    assert "(chart.getOption() || {}).dataZoom" not in _ECHARTS_HTML_TEMPLATE
    assert "e.batch" in _ECHARTS_HTML_TEMPLATE
    assert "chart.on('restore'" in _ECHARTS_HTML_TEMPLATE
    # 门控：只有带显式 id 的双轴 dataZoom（散点图）才挂自适应
    assert "Object.keys(_dzAxis).length" in _ECHARTS_HTML_TEMPLATE


def test_echarts_scatter_datazoom_has_axis_ids(workspace, sample_df):
    """散点双轴 dataZoom 必须带显式 id（dz-x/dz-y）：模板自适应脚本
    按 e.batch[].dataZoomId 归因事件来自哪个轴，无 id 时无法区分。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    ids = [z.get("id") for z in option["dataZoom"]]
    assert ids == ["dz-x", "dz-y"]


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


def test_echarts_scatter_3d_reports_sampling_and_keeps_small_groups(tmp_path):
    """30 万行 3D 散点：抽样到 3000 点必须在图上写明，且小组不被抽空。

    3D 云的抽样此前是静默的（用户以为看的是全量），且整体 df.sample 在分组
    极不平衡时会把小组合并成零星几个点。
    """
    rng = np.random.default_rng(9)
    rows = 60_000
    frame = pd.DataFrame({
        "x": rng.normal(0, 1, rows),
        "y": rng.normal(0, 1, rows),
        "z": rng.normal(0, 1, rows),
        "group": np.where(rng.random(rows) < 0.999, "大组", "小组"),
    })
    workspace = _make_workspace(tmp_path, frame)
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter_3d", "x": "x", "y": "y", "z": "z", "color": "group",
        "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    subtext = option["title"]["subtext"]
    assert "已抽样" in subtext and "60,000" in subtext
    points = {series["name"]: len(series["data"]) for series in option["series"]}
    assert sum(points.values()) <= 3000  # 预算是硬上限：3D 云超了会卡渲染
    # 小组（0.1% ≈ 60 条）必须完整保留，不能被抽样抹掉
    assert points.get("小组", 0) == int((frame["group"] == "小组").sum())


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



# === 大数据 HTML 嵌入降采样：交互 HTML 抽样渲染，完整数据保留在 JSON 产物 ===


def test_echarts_embed_sampling_big_line(tmp_path):
    """6 万行折线：HTML 按上限抽样 + 声明注记，.echarts.json 保留全量。

    回归背景：≥2 万行的**数值散点**自密度视图上线后不再逐点渲染、也不再抽样
    （见 test_echarts_density_html_small_without_sampling），抽样路径由折线
    （类目轴）继续覆盖；散点的嵌入抽样另由
    test_echarts_embed_sampling_categorical_scatter 覆盖（x 非数值 → 无法聚合）。
    """
    rng = np.random.default_rng(7)
    n = 60_000
    df = pd.DataFrame({"t": [f"t{i:06d}" for i in range(n)], "y": rng.normal(0, 1, n)})
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "line", "x": "t", "y": "y", "chart_engine": "echarts",
        }))
    sampling = result["sampling"]
    assert sampling["applied"] is True
    assert sampling["original_points"] > 50_000
    assert sampling["embedded_points"] <= 50_000
    # 完整数据保留在 JSON 产物（类目轴长度 == 原始行数）
    full_option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert len(full_option["xAxis"][0]["data"]) == n
    # HTML 是抽样副本：轴与系列同步抽稀，体积明显小于全量 JSON，且带抽样声明
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert "等距抽样" in html_text
    assert Path(result["html"]).stat().st_size < Path(result["echarts_json"]).stat().st_size


def test_echarts_embed_sampling_categorical_scatter(tmp_path):
    """6 万行散点但 x 是文本类别：聚合不成密度，退回逐点散点并继续抽样。

    密度视图要求 x/y 都是数值列；x 非数值时 `_density_view_for` 返回 None，
    此时仍走原来的"逐点 + 嵌入降采样"路径（这条回归保证大散点的兜底不被
    密度改造顺手删掉）。
    """
    rng = np.random.default_rng(7)
    n = 60_000
    df = pd.DataFrame({
        "segment": [["甲", "乙", "丙", "丁"][i % 4] for i in range(n)],
        "y": rng.normal(0, 1, n),
    })
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter", "x": "segment", "y": "y", "chart_engine": "echarts",
        }))
    assert "density_view" not in result
    sampling = result["sampling"]
    assert sampling["applied"] is True
    assert sampling["embedded_points"] <= 50_000
    full_option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    full_points = sum(
        len(s["data"]) for s in full_option["series"] if s.get("type") == "scatter"
    )
    assert full_points == sampling["original_points"]


def test_echarts_embed_sampling_category_axis_alignment():
    """折线（类目轴）抽样：轴与全部系列同一步长，data[i] 仍对应 categories[i]。"""
    from data_agent.chart_sampling import sample_echarts_option_for_embed

    n = 120
    option = {
        "xAxis": [{"type": "category", "data": [f"c{i}" for i in range(n)]}],
        "series": [
            {"type": "line", "data": [i * 2 for i in range(n)]},
            {"type": "line", "data": [i * 3 for i in range(n)]},
        ],
    }
    sampled, before, after = sample_echarts_option_for_embed(option, max_points=50)
    assert before == n
    axis = sampled["xAxis"][0]["data"]
    assert len(axis) == after <= 50
    for series in sampled["series"]:
        assert len(series["data"]) == len(axis)
    # 对齐性数值验证：series[0] 的第 k 个值 = 2 × 原始下标，原始下标可由
    # 轴名还原（c0, c2, c4, ...）
    idx = [int(name[1:]) for name in axis]
    assert [v for v in sampled["series"][0]["data"]] == [i * 2 for i in idx]
    assert [v for v in sampled["series"][1]["data"]] == [i * 3 for i in idx]
    # 原始 option 不被修改
    assert len(option["xAxis"][0]["data"]) == n
    assert len(option["series"][0]["data"]) == n


def test_echarts_embed_sampling_small_chart_untouched(workspace, sample_df):
    """小数据图表不触发嵌入抽样（响应无 sampling 字段，HTML 全量渲染）。"""
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit", "chart_engine": "echarts",
    }))
    assert "sampling" not in result


def test_plotly_embed_sampling_big_line(tmp_path):
    """6 万行 Plotly 折线：HTML 抽样渲染 + 注记，.plotly.json 保留全量。

    回归背景：≥2 万行的数值散点改走密度视图后不再抽样（
    test_plotly_density_view_replaces_scatter_sampling），抽样路径由折线覆盖。
    """
    rng = np.random.default_rng(7)
    n = 60_000
    df = pd.DataFrame({"t": [f"t{i:06d}" for i in range(n)], "v": rng.normal(0, 1, n)})
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_plotly_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "line", "x": "t", "y": "v",
        }))
    sampling = result["sampling"]
    assert sampling["applied"] is True
    assert sampling["embedded_points"] <= 50_000
    # 全量 figure 保留在 JSON 产物（typed-array 编码，解码后验证点数）
    from data_agent.chart_sampling import _decode_plotly_typed_arrays

    fig = _decode_plotly_typed_arrays(json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8")))
    x_values = fig["data"][0]["x"]
    assert len(x_values) >= 50_000
    # HTML 是抽样副本且带声明
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert "等距抽样" in html_text


def test_plotly_density_view_replaces_scatter_sampling(tmp_path):
    """≥2 万行数值散点：Plotly 分支改为密度视图，响应带 density_view 且不抽样。

    密度不是抽样（是精确聚合），因此既没有 sampling 字段，也不需要"完整数据
    在 JSON 里"的免责声明——这条回归锁住响应结构，避免两条引擎的字段分裂。
    """
    rng = np.random.default_rng(7)
    n = 25_000
    df = pd.DataFrame({"x": rng.uniform(0, 100, n), "y": rng.normal(0, 1, n)})
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_plotly_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter", "x": "x", "y": "y",
        }))
    assert "sampling" not in result
    density = result["density_view"]
    assert density["applied"] is True
    assert density["rows_used"] > 20_000
    assert density["panels"] == [""]
    # 网格是精确聚合：全量记录都参与（没有抽样丢点）
    assert density["rows_outside_view"] == 0
    assert density["bins"].endswith("×80")


def test_echarts_scatter_structure_annotations(workspace, sample_df):
    """ECharts 散点结构注记：趋势线 + 双轴均值象限线挂主系列。

    大点云没有视觉锚点就是一堵"点墙"（实测用户反馈眼花缭乱）。
    markLine 与既有 IQR 边界线合并而非覆盖。
    """
    tools = {t.name: t for t in build_tools(workspace)}
    result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "scatter", "x": "sales", "y": "profit", "chart_engine": "echarts",
    }))
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    main = option["series"][0]
    mark_line = main.get("markLine", {})
    data = mark_line.get("data", [])
    formatters = [
        (item[0].get("label", {}).get("formatter") if isinstance(item, list) else item.get("label", {}).get("formatter"))
        for item in data
    ]
    assert any(f and f.startswith("趋势 r=") for f in formatters)
    assert any(f and "x 均值" in f for f in formatters)
    assert any(f and "y 均值" in f for f in formatters)
    # 趋势线坐标为点对形式
    trend_item = next(item for item in data if isinstance(item, list))
    assert len(trend_item) == 2 and "coord" in trend_item[0]


# === 大数据散点 → 密度视图（ECharts 分支）===
# 语义与 Plotly 分支（tools/density_plotly.py）一致：同一份网格、同一套 1-2-5
# 档位、同一份浅/暗色板；差异只在渲染层——ECharts 的 heatmap 两个维度都必须是
# 类目轴，所以连续数值先在 Python 侧离散成格子，一切"按数值定位"的锚点都要先
# 换算成格子下标。


def _density_frame(n: int = 24_000, *, color: bool = False, clusters: bool = True,
                   seed: int = 11) -> pd.DataFrame:
    """合成密度图数据：clusters=True 给带明显聚集的云，False 给均匀噪声。"""
    rng = np.random.default_rng(seed)
    if clusters:
        x = rng.normal(0, 1, n)
        y = x * 0.6 + rng.normal(0, 0.6, n)
    else:
        x = rng.uniform(0, 100, n)
        y = rng.uniform(0, 100, n)
    frame = pd.DataFrame({"x": x, "y": y})
    if color:
        frame["segment"] = np.where(np.arange(n) % 2 == 0, "甲", "乙")
    return frame


def _density_option(df: pd.DataFrame, *, x: str = "x", y: str = "y",
                    color: str | None = None, title: str = "分布",
                    scale_mode: str = "auto"):
    """按引擎自身的分派条件生成 option，返回 (option, view, scale_details)。"""
    from data_agent.echarts_engine import _build_echarts_option, _density_view_for

    view, scale = _density_view_for(df, chart_type="scatter", x=x, y=y, color=color,
                                    scale_mode=scale_mode)
    assert view is not None, "该数据集应触发密度视图"
    option = _build_echarts_option(
        df, chart_type="scatter", x=x, y=y, color=color, z=None, size=None, values=None,
        path_columns=None, dimensions=None, aggregation="none", title=title, bins=30,
        density_view=view,
    )
    return option, view, scale


def _heatmaps(option: dict) -> list[dict]:
    return [item for item in option["series"] if item.get("type") == "heatmap"]


def test_echarts_density_dispatch_threshold():
    """阈值分派：2 万行及以上走密度视图，以下仍是逐点散点（同一个入口）。"""
    from data_agent.density import should_use_density
    from data_agent.echarts_engine import _build_echarts_option

    def option_for(n: int) -> dict:
        return _build_echarts_option(
            _density_frame(n), chart_type="scatter", x="x", y="y", color=None, z=None,
            size=None, values=None, path_columns=None, dimensions=None,
            aggregation="none", title="阈值", bins=30,
        )

    assert should_use_density(20_000) is True
    assert should_use_density(19_999) is False

    heavy = option_for(20_000)
    assert "densityBands" in heavy
    assert len(_heatmaps(heavy)) == 1
    # 逐点散点消失：只剩最外围叠加（≤120 点）与两条边缘直方图；
    # 另有一层"抽样原始点"（默认隐藏，点图例才显示），因此单独按系列名核对。
    extremes = [item for item in heavy["series"] if item.get("name") == "最外围记录"]
    assert sum(len(item["data"]) for item in extremes) <= 120
    assert [item["name"] for item in heavy["series"] if item["type"] == "bar"] == ["x 分布", "y 分布"]

    light = option_for(19_999)
    assert "densityBands" not in light
    assert _heatmaps(light) == []
    assert any(item["type"] == "scatter" for item in light["series"])


def test_echarts_density_bands_pieces_and_dark_palette():
    """档位 → visualMap pieces（唯一色标 = 全部分面的颜色键）+ densityBands 双色板。"""
    from data_agent.density import band_label

    option, view, _ = _density_option(_density_frame(color=True), color="segment")
    visual_map = option["visualMap"]
    assert visual_map["type"] == "piecewise"
    assert visual_map["dimension"] == 2
    # 颜色键不是筛选器：点某档不应把其它档的格子藏起来（与 Plotly colorbar 一致）
    assert visual_map["selectedMode"] is False
    bands = view.bands
    assert [piece["label"] for piece in visual_map["pieces"]] == [band_label(b) for b in bands]
    assert [piece["color"] for piece in visual_map["pieces"]] == view.band_colors()
    for piece, (low, high) in zip(visual_map["pieces"], bands, strict=True):
        if high is None:
            assert piece["min"] == low and "max" not in piece
        elif high <= low:
            assert piece["value"] == low
        else:
            assert (piece["min"], piece["max"]) == (low, high)
    # 色标只作用于热力图系列：边缘直方图与最外围记录不参与记录数着色
    heatmap_indexes = [i for i, s in enumerate(option["series"]) if s.get("type") == "heatmap"]
    assert visual_map["seriesIndex"] == heatmap_indexes
    # 亮/暗两套档位色随图带过去（换肤脚本与前端缩略图整组切换）
    payload = option["densityBands"]
    assert [item["color"] for item in payload["light"]] == view.band_colors()
    assert [item["color"] for item in payload["dark"]] == view.band_colors(dark=True)
    assert [item["label"] for item in payload["light"]] == [band_label(b) for b in bands]
    assert all({"label", "low", "high", "color"} <= set(item) for item in payload["dark"])


def test_echarts_density_zero_cells_omitted_and_category_axes():
    """零记录格不发数据项（背景透出覆盖范围）；两个维度都是类目轴。"""
    option, view, _ = _density_option(_density_frame())
    grid = view.panels[0].grid
    heatmap = _heatmaps(option)[0]
    occupied = int(np.count_nonzero(grid.counts))
    assert len(heatmap["data"]) == occupied
    assert 0 < occupied < grid.counts.size  # 密度图一定有空格，否则不叫"聚集"
    values = [item if isinstance(item, list) else item["value"] for item in heatmap["data"]]
    assert min(value[2] for value in values) >= 1
    assert max(value[2] for value in values) == grid.max_count
    # 每个数据项自带分箱区间（tooltip 不回查任何映射表）
    for value in values:
        assert len(value) == 7
        assert 0 <= value[0] < grid.counts.shape[1]
        assert 0 <= value[1] < grid.counts.shape[0]
        assert value[3] < value[4] and value[5] < value[6]
    # 类目轴：ECharts heatmap 的硬约束（"must have two categories"）
    assert option["xAxis"][0]["type"] == "category"
    assert option["yAxis"][0]["type"] == "category"
    assert len(option["xAxis"][0]["data"]) == view.bin_shape[0]
    assert len(option["yAxis"][0]["data"]) == view.bin_shape[1]
    # 150+ 个格子的轴必须抽稀刻度，否则标签糊成一团
    assert option["xAxis"][0]["axisLabel"]["interval"] > 0
    assert option["yAxis"][0]["axisLabel"]["interval"] > 0
    assert all(label for label in option["xAxis"][0]["data"])


def test_echarts_density_tooltip_carries_bin_ranges():
    """tooltip formatter 直接读数据项里的分箱边界（v[3..6]）与记录数。"""
    from data_agent.echarts_engine import _JsFunction

    option, _, _ = _density_option(_density_frame(), title="tooltip")
    formatter = option["tooltip"]["formatter"]
    assert isinstance(formatter, _JsFunction)
    assert "记录数" in formatter.code
    for index in (3, 4, 5, 6):
        assert f"v[{index}]" in formatter.code
    # 列名以 JS 字符串字面量拼入（防列名里的引号破坏函数体）
    assert json.dumps("x", ensure_ascii=False) in formatter.code
    assert json.dumps("y", ensure_ascii=False) in formatter.code
    # 边缘直方图与最外围记录各有自己的 formatter（趋势线不参与 hover）
    bars = [item for item in option["series"] if item.get("type") == "bar"]
    assert len(bars) == 2
    assert all(isinstance(item["tooltip"]["formatter"], _JsFunction) for item in bars)
    scatter = [item for item in option["series"] if item.get("type") == "scatter"]
    assert all(isinstance(item["tooltip"]["formatter"], _JsFunction) for item in scatter)


def test_echarts_density_single_panel_marginals():
    """单面板：主热力图 + 顶部 x 分布 + 右侧 y 分布，逐格对齐 + 联动轴指针。"""
    option, view, _ = _density_option(_density_frame())
    assert len(option["grid"]) == 3
    heatmap = _heatmaps(option)[0]
    assert (heatmap["xAxisIndex"], heatmap["yAxisIndex"]) == (0, 0)
    bars = [item for item in option["series"] if item.get("type") == "bar"]
    assert [item["name"] for item in bars] == ["x 分布", "y 分布"]
    x_bar, y_bar = bars
    assert (x_bar["xAxisIndex"], x_bar["yAxisIndex"]) == (1, 1)
    assert (y_bar["xAxisIndex"], y_bar["yAxisIndex"]) == (2, 2)
    grid = view.panels[0].grid
    assert x_bar["data"] == [int(value) for value in grid.marginal_x()]
    assert y_bar["data"] == [int(value) for value in grid.marginal_y()]
    # 柱宽 100% + 类目间隙 0：柱子与主面板的格子逐格对齐（jointplot 的核心价值）
    assert x_bar["barWidth"] == "100%" and y_bar["barWidth"] == "100%"
    assert option["xAxis"][1]["data"] == option["xAxis"][0]["data"]
    assert option["yAxis"][2]["data"] == option["yAxis"][0]["data"]
    assert option["xAxis"][2]["type"] == "value"
    assert option["yAxis"][1]["type"] == "value"
    assert option["axisPointer"]["link"] == [{"xAxisIndex": [0, 1]}, {"yAxisIndex": [0, 2]}]
    # 轴名只在主面板写（边缘直方图共享同一分箱，标签是噪声）
    assert option["xAxis"][0]["name"] == "x"
    assert option["yAxis"][0]["name"] == "y"
    assert "name" not in option["xAxis"][1]
    # 均值参考线换算成类目下标（类目轴上直接给原始数值会落到轴外）
    mark_data = heatmap["markLine"]["data"]
    labels = [item["label"]["formatter"] for item in mark_data]
    assert any("x 均值" in text for text in labels)
    assert any("y 均值" in text for text in labels)
    for item in mark_data:
        assert isinstance(item.get("xAxis", item.get("yAxis")), int)


def test_echarts_density_faceted_layout_shares_band_scale():
    """有颜色分组：每类一个面板、共享一套档位色标，标题带记录数/占比/r。"""
    option, view, _ = _density_option(_density_frame(color=True), color="segment", title="分面")
    assert view.is_faceted
    heatmaps = _heatmaps(option)
    assert len(heatmaps) == len(view.panels) == 2
    assert len(option["grid"]) == len(view.panels)
    # 每个面板一组轴，gridIndex 与面板一一对应；两个维度仍是类目轴
    assert [axis["gridIndex"] for axis in option["xAxis"]] == list(range(len(view.panels)))
    assert [axis["gridIndex"] for axis in option["yAxis"]] == list(range(len(view.panels)))
    assert all(axis["type"] == "category" for axis in option["xAxis"])
    assert all(axis["type"] == "category" for axis in option["yAxis"])
    # 共享轴：只有最左一列写 y 刻度（2 个面板同处最后一行，x 刻度都显示）
    assert [axis["axisLabel"]["show"] for axis in option["yAxis"]] == [True, False]
    assert all(axis["axisLabel"]["show"] for axis in option["xAxis"])
    # 共享同一套档位（颜色含义跨面板一致才可横向比较）：只有一个 visualMap，
    # 且只作用于热力图系列（面板间还夹着趋势线与最外围记录）
    assert isinstance(option["visualMap"], dict)
    heatmap_indexes = [i for i, item in enumerate(option["series"])
                       if item.get("type") == "heatmap"]
    assert option["visualMap"]["seriesIndex"] == heatmap_indexes
    assert len(heatmap_indexes) == len(view.panels)
    # 面板标题：分组 · N 条（占比%）· r=0.xx（合成数据 |r| ≈ 0.7 ≥ 0.25）
    assert isinstance(option["title"], list)
    assert len(option["title"]) == 1 + len(view.panels)
    for item, panel in zip(option["title"][1:], view.panels, strict=True):
        assert item["text"].startswith(panel.name)
        assert f"{panel.grid.total:,} 条" in item["text"]
        assert "（50%）" in item["text"]
        assert "· r=" in item["text"]
    # 面板趋势线：r 达标才画，线段长 = 面板的列数（类目轴逐格给 y 下标）
    trend = [item for item in option["series"] if item.get("type") == "line"]
    assert len(trend) == len(view.panels)
    for line in trend:
        assert len(line["data"]) == view.bin_shape[0]
        assert line["xAxisIndex"] == line["yAxisIndex"]
    # 最外围记录：每个面板一份（≤120 点）
    extremes = [item for item in option["series"] if item.get("name") == "最外围记录"]
    assert len(extremes) == len(view.panels)
    assert all(len(item["data"]) <= 120 for item in extremes)
    # 分面不画均值参考线（格子太小，画进去只会变成噪声）
    assert "markLine" not in heatmaps[0]


def test_echarts_density_peak_marker_only_with_real_clusters():
    """最密格只在"确有聚集"时标注（均匀噪声的最密格只是采样涨落）。"""
    clustered, view, _ = _density_option(_density_frame(clusters=True))
    assert view.has_real_clusters
    marked = [item for item in _heatmaps(clustered)[0]["data"] if isinstance(item, dict)]
    assert len(marked) == 1
    cell = marked[0]
    assert cell["label"]["show"] is True and "最密" in cell["label"]["formatter"]
    assert cell["itemStyle"]["borderColor"] == "#E15759"
    # 标签自带白底深红字（与 Plotly 注记同款）：两种主题下都可读，因此显式给色
    assert cell["label"]["color"] == "#B23A3C"
    assert cell["label"]["backgroundColor"].startswith("rgba(255,255,255")
    grid = view.panels[0].grid
    flat = int(np.argmax(grid.counts))
    assert (cell["value"][0], cell["value"][1]) == (
        flat % grid.counts.shape[1], flat // grid.counts.shape[1],
    )
    assert cell["value"][2] == grid.max_count

    uniform, view_uniform, _ = _density_option(_density_frame(clusters=False))
    assert not view_uniform.has_real_clusters
    assert all(isinstance(item, list) for item in _heatmaps(uniform)[0]["data"])


def test_echarts_density_interpretation_and_no_point_cloud():
    """解读换成密度语义（颜色读什么、锚点在哪），且不再逐点渲染。"""
    from data_agent.density import density_note, hotspot_sentence, panels_sentence
    from data_agent.echarts_engine import _auto_interpret

    df = _density_frame(color=True)
    option, view, _ = _density_option(df, color="segment", title="解读")
    text = _auto_interpret(df, chart_type="scatter", x="x", y="y", color="segment",
                           aggregation="none", title="解读", density_view=view)
    assert "相关（r=" in text
    # 三句锚点文案逐句来自 data_agent.density（与 Plotly 分支同一份口径）
    assert density_note(view, color_label="segment") in text
    assert hotspot_sentence(view, "x", "y") in text
    assert panels_sentence(view, "segment") in text
    assert "点云已按segment拆成 2 个密度面板" in text
    assert "80×67 网格" in text  # 分面后的网格数
    assert "颜色越深表示该区域记录越密集" in text
    assert "滚轮可放大局部" in text
    # 旧的点云提示对密度图不成立（没有可框选的离散点）
    assert "滚轮缩放可查看密集区域" not in text
    # 逐点散点已消失：只剩 ≤120 点的最外围叠加
    bulk = [item for item in option["series"]
            if item.get("type") == "scatter" and len(item["data"]) > 120]
    assert bulk == []
    cells = sum(len(item["data"]) for item in _heatmaps(option))
    assert 0 < cells < len(df)
    # 没有 density_view 时仍走原来的散点解读（非密度调用方不受影响）
    plain = _auto_interpret(df, chart_type="scatter", x="x", y="y", color="segment",
                            aggregation="none", title="解读")
    assert "滚轮缩放可查看密集区域" in plain


def test_echarts_density_caps_panels_at_six():
    """颜色类别多于 6 个：按记录数取前 5 类 + "其他 N 类"面板，3 列 × 2 行。"""
    rng = np.random.default_rng(5)
    n = 24_000
    df = pd.DataFrame({
        "x": rng.normal(0, 1, n),
        "y": rng.normal(0, 1, n),
        "segment": [f"s{index % 9}" for index in range(n)],
    })
    option, view, _ = _density_option(df, color="segment")
    assert len(view.panels) == 6
    assert view.panels[-1].name.startswith("其他")
    assert len(_heatmaps(option)) == len(option["grid"]) == 6
    assert len(option["xAxis"]) == len(option["yAxis"]) == 6
    assert option["title"][-1]["text"].startswith("其他")
    # 3 列 × 2 行：只有 3 个不同的 left、2 个不同的 top
    assert len({item["left"] for item in option["grid"]}) == 3
    assert len({item["top"] for item in option["grid"]}) == 2
    # 3 列布局下只有第一列有 y 刻度、最后一行（第 4-6 个面板）有 x 刻度
    assert [axis["axisLabel"]["show"] for axis in option["yAxis"]] == [True, False, False] * 2
    assert [axis["axisLabel"]["show"] for axis in option["xAxis"]] == [False] * 3 + [True] * 3


def test_echarts_density_trend_clipped_to_view_range():
    """趋势线必须裁剪到可视范围：超出画布的那段不能被"夹"到轴边界上。

    回归背景：趋势端点按面板数据的 min/max 算，坐标轴却用主体尺度（极端记录
    触发 ``_severe_axis_compression``）。不裁剪时超出范围的部分会被类目下标
    夹到最上一格，在面板顶部横铺一整条线，读起来像"平趋势"（实测截图可见）。
    裁剪用共享的 ``density.clip_segment``（与 Plotly 分支同一实现）。
    """
    rng = np.random.default_rng(9)
    n = 24_000
    x = rng.normal(0, 1, n)
    y = 0.6 * x + rng.normal(0, 0.6, n)
    # 30 个极端记录沿趋势线向两侧拉长：坐标轴被压到主体尺度，趋势端点远在其外
    far = np.concatenate([np.linspace(-120, -90, 15), np.linspace(90, 120, 15)])
    df = pd.DataFrame({
        "x": np.concatenate([x, far]),
        "y": np.concatenate([y, 0.6 * far]),
    })
    option, view, scale = _density_option(df)
    assert scale["scale_mode"] == "robust"
    assert "x" in scale["axis_ranges"]
    ny = view.bin_shape[1]
    trend = [item for item in option["series"] if item["type"] == "line"][0]
    values = [value for value in trend["data"] if value is not None]
    assert len(values) >= 2
    # 老行为会把越界段钉在边界格上（出现 ny-1 或 0）；裁剪后这条斜线从左右两侧
    # 进出可视框，纵向下标必须严格落在内部，且仍是一条看得出来的斜线
    assert min(values) > 0 and max(values) < ny - 1
    assert max(values) - min(values) >= ny // 4


def test_echarts_density_trend_skipped_when_outside_view():
    """整段趋势线落在可视范围外时不画（与 Plotly 分支的 skip 同行为）。"""
    from data_agent.density import build_density_view
    from data_agent.echarts_engine import _density_trend_series

    df = _density_frame()
    view = build_density_view(df["x"], df["y"])
    grid = view.panels[0].grid
    inside = {"trend": [-1.0, -1.0, 1.0, 1.0], "r": 0.9}
    assert _density_trend_series(grid, inside, axis_index=0, x_range=view.x_range,
                                 y_range=view.y_range) is not None
    outside = {"trend": [50.0, 50.0, 60.0, 60.0], "r": 0.9}
    assert _density_trend_series(grid, outside, axis_index=0, x_range=view.x_range,
                                 y_range=view.y_range) is None
    # 分面门槛：|r| 不足时不画
    weak = {"trend": [-1.0, -1.0, 1.0, 1.0], "r": 0.1}
    assert _density_trend_series(grid, weak, axis_index=0, x_range=view.x_range,
                                 y_range=view.y_range, min_r=0.25) is None


def test_echarts_density_facet_trend_requires_min_r():
    """分面趋势线只在 |r| ≥ 0.25 时出现（弱相关的斜线只会被读成趋势）。"""
    rng = np.random.default_rng(21)
    n = 24_000
    df = pd.DataFrame({
        "x": rng.normal(0, 1, n),
        "y": rng.normal(0, 1, n),  # 与 x 无关 → 每个面板 r ≈ 0
        "segment": np.where(np.arange(n) % 2 == 0, "甲", "乙"),
    })
    option, view, _ = _density_option(df, color="segment")
    assert len(_heatmaps(option)) == len(view.panels) == 2
    assert [item for item in option["series"] if item["type"] == "line"] == []
    assert all("· r=" not in item["text"] for item in option["title"][1:])


def test_echarts_density_mean_lines_skip_out_of_range():
    """均值参考线落在可视范围外时不画（夹到边界格上会画出一条位置错误的线）。"""
    rng = np.random.default_rng(4)
    n = 24_000
    x = rng.normal(0, 1, n)
    y = 0.6 * x + rng.normal(0, 0.6, n)
    tail = 4_000  # 14% 的高利润长尾：均值被抬到 [-4, 4] 之外，但不足以取消主体尺度
    df = pd.DataFrame({
        "x": np.concatenate([x, rng.normal(0, 1, tail)]),
        "y": np.concatenate([y, rng.uniform(30, 60, tail)]),
    })
    option, view, scale = _density_option(df)
    assert scale["scale_mode"] == "robust"
    assert scale["axis_ranges"]["y"] == [-4.0, 4.0]
    mark_data = _heatmaps(option)[0]["markLine"]["data"]
    labels = [item["label"]["formatter"] for item in mark_data]
    assert any("x 均值" in text for text in labels)
    assert not any("y 均值" in text for text in labels)


def test_echarts_density_labels_snap_float_noise():
    """跨零点的分箱边界会算出 -2e-07 这类浮点噪声，标签与悬浮提示必须显示 0。

    实测：主体尺度范围 (-100.0000004, 300.0000004) 均分 80 格后，第 20 条
    边界是 -2.0000002e-07，``_nice_axis_formatter`` 会把它写成 "-2.00e-07"
    挂在 y 轴上（截图里一眼可见）。
    """
    from data_agent.echarts_engine import _bin_labels, _snap_edges

    edges = np.linspace(-100.0000004, 300.0000004, 81)  # 与 nice ticks 的残留同形
    noise = min(abs(float(value)) for value in edges)
    assert 0 < noise < 1e-5  # 确实存在噪声级边界
    labels = _bin_labels(edges)
    assert "0" in labels
    assert not any("e-" in label for label in labels)
    # 真实的小数值不受影响（窄区间数据的边界远大于格宽的百万分之一）
    assert float(_snap_edges(np.array([0.0, 1e-04, 2e-04]))[1]) == 1e-04
    # 数据项里的分箱区间同样归零（悬浮提示不出现 -2e-07）
    option, _, _ = _density_option(_density_frame())
    values = [item if isinstance(item, list) else item["value"]
              for item in _heatmaps(option)[0]["data"]]
    assert all(abs(value[5]) == 0 or abs(value[5]) > 1e-5 for value in values)


def test_echarts_density_dark_mode_swaps_band_colors():
    """换肤脚本按 densityBands 整组换档位色，并保住峰值标签的其余字段。"""
    from data_agent.echarts_engine import (
        _ECHARTS_DARK_MODE_SCRIPT,
        _ECHARTS_HTML_TEMPLATE,
        _build_echarts_html,
    )

    script = _ECHARTS_DARK_MODE_SCRIPT
    assert "densityBands" in script
    assert "densityBands.dark" in script and "densityBands.light" in script
    assert "upd.pieces" in script
    # 峰值格的 item 级 label（show/formatter/position/白底深红字）不能被
    # "深格白字翻转"丢掉：只翻 #ffffff 那一种约定，其余颜色原样保留
    assert "copy.label = label" in script
    assert "=== '#ffffff'" in script
    # 非标准顶层键不保证被 getOption 保留：模板把色板挂到实例上兜底
    assert "chart.__densityBands = option.densityBands" in _ECHARTS_HTML_TEMPLATE
    html = _build_echarts_html(
        title="t", option={"densityBands": {"light": [], "dark": []}},
        script_src="echarts.min.js", interpretation="",
    )
    assert "densityBands" in html


def test_echarts_density_html_small_without_sampling(tmp_path):
    """≥2 万行数值散点：HTML 是密度图（几百 KB 量级），不做嵌入抽样。"""
    rng = np.random.default_rng(3)
    n = 25_000
    df = pd.DataFrame({"x": rng.normal(0, 1, n), "y": rng.normal(0, 1, n)})
    ws = _make_workspace(tmp_path, df)
    tools = {t.name: t for t in build_tools(ws)}
    with patch.object(DataWorkspace, "ensure_echarts_bundle", return_value=_mock_bundle(tmp_path)):
        result = json.loads(tools["create_visualization"].invoke({
            "chart_type": "scatter", "x": "x", "y": "y", "chart_engine": "echarts",
        }))
    assert "sampling" not in result
    density = result["density_view"]
    assert density["applied"] is True
    assert density["panels"] == [""]
    assert density["rows_used"] == n
    assert result["scale_mode"] in {"full", "robust"}
    option = json.loads(Path(result["echarts_json"]).read_text(encoding="utf-8"))
    assert "densityBands" in option
    # 逐点散点的 option 体积随行数线性增长（2.5 万行约 1MB+）；密度图只发格子
    assert Path(result["echarts_json"]).stat().st_size < 700_000
    cells = sum(len(item["data"]) for item in _heatmaps(option))
    assert 0 < cells < n
    extremes = [item for item in option["series"] if item.get("name") == "最外围记录"]
    assert sum(len(item["data"]) for item in extremes) <= 120
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert "等距抽样" not in html_text


def test_echarts_density_detail_layer_is_legend_toggled_and_hidden_by_default():
    """ECharts 侧与 Plotly 同语义的"放大看原始记录"：图例开关 + 默认隐藏。

    ECharts 没有图内按钮，用原生图例：series 进 legend.data，并在
    legend.selected 里默认 false——不依赖自定义 JS，单文件下载照常可用。
    """
    from data_agent.echarts_engine import _DENSITY_DETAIL_POINTS, _build_echarts_option

    option = _build_echarts_option(
        _density_frame(20_000), chart_type="scatter", x="x", y="y", color=None, z=None,
        size=None, values=None, path_columns=None, dimensions=None,
        aggregation="none", title="细节图层", bins=30,
    )
    detail = [item for item in option["series"] if "抽样原始点" in str(item.get("name"))]
    assert len(detail) == 1
    series = detail[0]
    assert series["type"] == "scatter"
    assert 0 < len(series["data"]) <= _DENSITY_DETAIL_POINTS
    # 主面板坐标轴（不是边缘直方图那两根）
    assert (series["xAxisIndex"], series["yAxisIndex"]) == (0, 0)
    # 图例里必须列出它，且默认不选中（= 隐藏）
    assert series["name"] in option["legend"]["data"]
    assert option["legend"]["selected"][series["name"]] is False
    # 抽样事实写进 trace 名，避免被当成全量
    assert "抽样" in series["name"]


def test_echarts_faceted_density_has_no_detail_layer():
    """分面图不加细节图层（一整层点会落到错误面板）。"""
    from data_agent.echarts_engine import _build_echarts_option

    frame = _density_frame(20_000)
    frame["group"] = ["甲", "乙"] * 10_000
    option = _build_echarts_option(
        frame, chart_type="scatter", x="x", y="y", color="group", z=None,
        size=None, values=None, path_columns=None, dimensions=None,
        aggregation="none", title="分面", bins=30,
    )
    assert not [item for item in option["series"] if "抽样原始点" in str(item.get("name"))]
