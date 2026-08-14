"""端到端真实数据验证：覆盖所有支持的数据文件格式 + 图表自动选择 + HTML 渲染。

本测试模块不使用 mock，真实地：
1. 生成各种格式的测试数据文件（CSV/TSV/Excel/JSON/JSONL/Parquet/PDF/TXT/DOCX）
2. 通过 DataWorkspace.load() 真实加载每种格式
3. 对每种格式运行 create_visualization(chart_type="auto") 验证自动选图
4. 验证生成的 HTML 文件包含正确的渲染引擎初始化标记
5. 验证大数据量（10万行）下的分块读取和图表生成
6. 验证非结构化数据（PDF/TXT/DOCX）的表格提取和文本提取
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


def _tool_map(workspace):
    return {item.name: item for item in build_tools(workspace)}


def _assert_html_renders(html_path: Path, engine: str = "plotly"):
    """验证 HTML 文件包含正确的渲染引擎初始化标记。"""
    assert html_path.exists(), f"HTML 文件未生成：{html_path}"
    assert html_path.stat().st_size > 5_000, (
        f"HTML 文件过小（{html_path.stat().st_size} 字节），可能渲染失败"
    )
    html_text = html_path.read_text(encoding="utf-8")
    if engine == "plotly":
        assert "Plotly.newPlot" in html_text or "plotly.min.js" in html_text, (
            f"HTML 缺少 Plotly 初始化标记，预览将空白：{html_path}"
        )
    elif engine == "echarts":
        assert "echarts.init" in html_text or "echarts.min.js" in html_text, (
            f"HTML 缺少 ECharts 初始化标记，预览将空白：{html_path}"
        )


# ---------------------------------------------------------------------------
# 1. 结构化表格格式：CSV / TSV / Excel / JSON / JSONL / Parquet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "suffix,writer",
    [
        (".csv", lambda df, p: df.to_csv(p, index=False)),
        (".tsv", lambda df, p: df.to_csv(p, index=False, sep="\t")),
        (".xlsx", lambda df, p: df.to_excel(p, index=False)),
        (".json", lambda df, p: df.to_json(p, orient="records", force_ascii=False)),
        (".jsonl", lambda df, p: df.to_json(p, orient="records", lines=True, force_ascii=False)),
        (".parquet", lambda df, p: df.to_parquet(p, index=False)),
    ],
    ids=["csv", "tsv", "xlsx", "json", "jsonl", "parquet"],
)
def test_structured_format_load_and_chart(tmp_path, suffix, writer):
    """每种结构化格式都能正确加载并生成可渲染的图表 HTML。"""
    df = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=12, freq="MS").astype(str),
        "revenue": [120, 135, 148, 142, 167, 189, 201, 195, 210, 225, 240, 260],
        "cost": [80, 85, 90, 88, 95, 100, 110, 105, 115, 120, 130, 140],
    })
    path = tmp_path / f"data{suffix}"
    writer(df, path)

    workspace = DataWorkspace(tmp_path / "runs", session_id=f"fmt_{suffix[1:]}")
    workspace.load(path, copy_into_workspace=True)
    assert workspace.dataframe.shape == (12, 3)

    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": f"收入趋势-{suffix}", "x": "date", "y": "revenue"}
        )
    )
    assert result["chart_type"] == "line"
    _assert_html_renders(Path(result["html"]))


# ---------------------------------------------------------------------------
# 2. 非结构化数据：TXT / PDF / DOCX
# ---------------------------------------------------------------------------

def test_txt_file_load_and_chart(tmp_path):
    """TXT 文件按行解析为单列 DataFrame，可生成图表。"""
    lines = [f"2025-01-{i:02d},日志条目 {i}" for i in range(1, 21)]
    path = tmp_path / "logs.txt"
    path.write_text("\n".join(lines), encoding="utf-8")

    workspace = DataWorkspace(tmp_path / "runs", session_id="txt_test")
    workspace.load(path, copy_into_workspace=True)
    assert workspace.dataframe.shape == (20, 1)
    assert "文本" in workspace.dataframe.columns

    # TXT 数据为单列文本，auto 应选择 histogram 展示文本频次分布
    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": "日志分布", "x": "文本"}
        )
    )
    _assert_html_renders(Path(result["html"]))


def test_pdf_file_load(tmp_path):
    """PDF 文件提取表格数据，验证表格行数和列数。

    reportlab 默认字体不含中文字形，中文表头/值提取后可能残缺，但表格
    结构（3 行数据 × 3 列）必须被完整提取，否则视为解析失败。此断言
    真实覆盖 _read_pdf 的表格提取路径（之前 try/except pass 使该测试
    在解析整体失败时依然通过）。
    """
    pytest.importorskip("pdfplumber")
    pytest.importorskip("reportlab")
    path = tmp_path / "report.pdf"
    _create_simple_pdf(path)

    workspace = DataWorkspace(tmp_path / "runs", session_id="pdf_test")
    profile = workspace.load(path, copy_into_workspace=True)
    assert profile["rows"] == 3
    assert profile["columns"] == 3
    assert workspace.dataframe.shape == (3, 3)


def _create_simple_pdf(path: Path):
    """创建一个包含简单表格的 PDF 文件。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    doc = SimpleDocTemplate(str(path), pagesize=A4)
    data = [
        ["月份", "销售额", "利润"],
        ["1月", "100", "20"],
        ["2月", "150", "30"],
        ["3月", "120", "25"],
    ]
    table = Table(data)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
    ]))
    doc.build([table])


def test_docx_file_load_and_chart(tmp_path):
    """DOCX 文件提取 Word 表格数据，可生成图表。"""
    pytest.importorskip("docx")
    from docx import Document

    path = tmp_path / "report.docx"
    doc = Document()
    # 添加表格
    table = doc.add_table(rows=4, cols=3)
    headers = ["季度", "收入", "支出"]
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    rows = [
        ["Q1", "1000", "800"],
        ["Q2", "1200", "900"],
        ["Q3", "1100", "850"],
    ]
    for r, row_data in enumerate(rows, 1):
        for c, val in enumerate(row_data):
            table.rows[r].cells[c].text = val
    doc.save(str(path))

    workspace = DataWorkspace(tmp_path / "runs", session_id="docx_test")
    workspace.load(path, copy_into_workspace=True)
    assert workspace.dataframe.shape[0] >= 3
    assert "季度" in workspace.dataframe.columns

    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": "季度收支", "x": "季度", "y": "收入"}
        )
    )
    _assert_html_renders(Path(result["html"]))


# ---------------------------------------------------------------------------
# 3. 编码与异常数据
# ---------------------------------------------------------------------------

def test_chinese_gb18030_csv_load_and_chart(tmp_path):
    """GB18030 编码的中文 CSV 能正确加载并生成图表。"""
    df = pd.DataFrame({
        "城市": ["北京", "上海", "广州", "深圳", "成都"],
        "人口": [2171, 2418, 1404, 1303, 1094],
        "GDP": [36102, 38700, 25030, 27670, 17716],
    })
    path = tmp_path / "chinese.csv"
    df.to_csv(path, index=False, encoding="gb18030")

    workspace = DataWorkspace(tmp_path / "runs", session_id="gb18030")
    workspace.load(path, copy_into_workspace=True)
    assert workspace.dataframe.shape == (5, 3)

    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": "城市GDP对比", "x": "城市", "y": "GDP"}
        )
    )
    _assert_html_renders(Path(result["html"]))


def test_malformed_csv_skips_bad_rows(tmp_path):
    """格式错误的 CSV 能跳过坏行并记录警告。"""
    path = tmp_path / "bad.csv"
    path.write_text("a,b,c\n1,2,3\n4,5\n6,7,8\n", encoding="utf-8")

    workspace = DataWorkspace(tmp_path / "runs", session_id="bad_csv")
    workspace.load(path, copy_into_workspace=True)
    # 小文件走严格模式 + 回退跳过坏行路径；行数不对的坏行会被 python 引擎跳过
    assert workspace.dataframe.shape[0] >= 2


def test_csv_with_missing_values(tmp_path):
    """包含空值的 CSV 能正确加载并生成图表（空值不导致渲染崩溃）。"""
    df = pd.DataFrame({
        "category": ["A", "B", "A", None, "B", "A"],
        "value": [10, None, 30, 40, None, 60],
        "score": [85, 92, 78, 88, 95, 82],
    })
    path = tmp_path / "missing.csv"
    df.to_csv(path, index=False)

    workspace = DataWorkspace(tmp_path / "runs", session_id="missing")
    workspace.load(path, copy_into_workspace=True)

    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": "含空值数据", "x": "category", "y": "score"}
        )
    )
    _assert_html_renders(Path(result["html"]))


# ---------------------------------------------------------------------------
# 4. 大数据量验证
# ---------------------------------------------------------------------------

def test_large_csv_chunked_read_and_chart(tmp_path):
    """10万行 CSV 验证分块读取和图表生成（不 OOM）。"""
    n = 100_000
    df = pd.DataFrame({
        "id": range(n),
        "value": [i * 1.5 for i in range(n)],
        "group": [f"g{i % 5}" for i in range(n)],
    })
    path = tmp_path / "large.csv"
    df.to_csv(path, index=False)

    workspace = DataWorkspace(tmp_path / "runs", session_id="large")
    workspace.load(path, copy_into_workspace=True)
    assert workspace.dataframe.shape == (n, 3)

    # 大数据量下生成直方图，验证不 OOM 且 HTML 正常
    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": "大表分布", "x": "value"}
        )
    )
    assert result["chart_type"] == "histogram"
    _assert_html_renders(Path(result["html"]))


# ---------------------------------------------------------------------------
# 5. 图表类型全覆盖验证
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,data,kwargs,expected_type",
    [
        # 时间序列 → line
        (
            "time_series",
            {
                "date": pd.date_range("2025-01-01", periods=30, freq="D").astype(str),
                "visits": [100 + i * 3 + (i % 7) * 10 for i in range(30)],
            },
            {"x": "date", "y": "visits"},
            "line",
        ),
        # 两数值关系 → scatter
        (
            "scatter_relation",
            {
                "temp": [15, 18, 22, 25, 28, 30, 32, 29, 26, 20],
                "sales": [120, 150, 180, 210, 250, 280, 300, 270, 230, 170],
            },
            {"x": "temp", "y": "sales"},
            "scatter",
        ),
        # 单数值分布 → histogram
        (
            "distribution",
            {"score": [60, 65, 70, 72, 75, 78, 80, 82, 85, 88, 90, 92, 95]},
            {"y": "score"},
            "histogram",
        ),
        # 分类+数值（少类别）→ pie
        (
            "pie_chart",
            {
                "product": ["A", "B", "C", "D"] * 3,
                "sales": [100, 200, 150, 80] * 3,
            },
            {"x": "product", "y": "sales"},
            "pie",
        ),
        # 分类计数 → bar
        (
            "bar_count",
            {"fruit": ["apple", "banana", "apple", "cherry", "banana", "apple"] * 3},
            {"x": "fruit"},
            "bar",
        ),
        # 多数值列 → correlation_heatmap
        (
            "heatmap",
            {
                "a": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "b": [2, 4, 6, 8, 10, 12, 14, 16, 18, 20],
                "c": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
            },
            {},
            "correlation_heatmap",
        ),
        # 三维数据 → scatter_3d
        (
            "scatter_3d",
            {
                "x": list(range(20)),
                "y": [i * 2 for i in range(20)],
                "z": [i * 3 for i in range(20)],
            },
            {"x": "x", "y": "y", "z": "z"},
            "scatter_3d",
        ),
    ],
    ids=["line", "scatter", "histogram", "pie", "bar", "heatmap", "3d"],
)
def test_chart_type_full_coverage(tmp_path, name, data, kwargs, expected_type):
    """每种图表类型都能根据数据自动选择并正确渲染 HTML。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id=name)
    workspace.dataframe = pd.DataFrame(data)

    result = json.loads(
        _tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": f"测试-{name}", **kwargs}
        )
    )
    assert result["chart_type"] == expected_type, (
        f"场景 {name}：期望 {expected_type}，实际 {result['chart_type']}"
    )
    _assert_html_renders(Path(result["html"]))


# ---------------------------------------------------------------------------
# 6. 数据分析工具链验证（clean + transform + statistics + visualize）
# ---------------------------------------------------------------------------

def test_full_analysis_pipeline(tmp_path):
    """完整分析流程：加载 → 清洗 → 统计 → 可视化，每步都成功。"""
    df = pd.DataFrame({
        "region": ["East", "West", "East", "West", "East", "West", "East", None],
        "sales": [100.0, 200.0, None, 230.0, 150.0, 180.0, 120.0, 90.0],
        "profit": [10, 32, 14, 40, 18, 25, 12, 8],
        "date": pd.date_range("2025-01-01", periods=8, freq="D").astype(str),
    })
    path = tmp_path / "pipeline.csv"
    df.to_csv(path, index=False)

    workspace = DataWorkspace(tmp_path / "runs", session_id="pipeline")
    workspace.load(path, copy_into_workspace=True)
    tools = _tool_map(workspace)

    # 1. 清洗数据
    clean_result = json.loads(tools["clean_data"].invoke({
        "operations": ["trim_whitespace", "remove_duplicates", "fill_missing"],
    }))
    assert clean_result["status"] == "ok"

    # 2. 统计分析
    stats_result = json.loads(tools["statistical_analysis"].invoke({
        "method": "descriptive",
    }))
    assert "result" in stats_result

    # 3. 相关性分析
    corr_result = json.loads(tools["statistical_analysis"].invoke({
        "method": "correlation",
    }))
    assert "result" in corr_result

    # 3. 可视化
    viz_result = json.loads(tools["create_visualization"].invoke({
        "chart_type": "auto",
        "title": "区域销售分析",
        "x": "date",
        "y": "sales",
    }))
    assert viz_result["chart_type"] == "line"
    _assert_html_renders(Path(viz_result["html"]))


# ---------------------------------------------------------------------------
# 7. 沙箱代码执行验证
# ---------------------------------------------------------------------------

def test_sandbox_code_execution(tmp_path):
    """run_python_code 沙箱能正确执行自定义代码并返回结果。"""
    df = pd.DataFrame({
        "category": ["A", "B", "A", "B", "A"],
        "value": [10, 20, 15, 25, 12],
    })
    path = tmp_path / "sandbox.csv"
    df.to_csv(path, index=False)

    workspace = DataWorkspace(tmp_path / "runs", session_id="sandbox")
    workspace.load(path, copy_into_workspace=True)
    tools = _tool_map(workspace)

    result = json.loads(tools["run_python_code"].invoke({
        "code": "result = df.groupby('category')['value'].mean().to_dict()",
    }))
    assert result["status"] == "ok"
    assert result["result"]["A"] == 12.333333333333334
    assert result["result"]["B"] == 22.5


def test_sandbox_blocks_file_io(tmp_path):
    """沙箱禁止文件 I/O 方法（read_csv/to_csv 等）。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id="sandbox_block")
    workspace.dataframe = pd.DataFrame({"a": [1, 2, 3]})
    tools = _tool_map(workspace)

    # to_csv 应在 AST 审查阶段被拦截，抛 ValueError
    with pytest.raises(ValueError, match="to_csv"):
        tools["run_python_code"].invoke({"code": "df.to_csv('/tmp/escape.csv')"})

    # read_csv 同样被拦截
    with pytest.raises(ValueError, match="read_csv"):
        tools["run_python_code"].invoke({
            "code": "import pandas as pd; pd.read_csv('/tmp/escape.csv')",
        })
