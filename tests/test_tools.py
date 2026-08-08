from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.io as pio
import pytest

from data_agent.tools import build_tools
from data_agent.tools._cleaning import (
    _apply_missing_strategy,
    _handle_outliers,
    _normalize_column_names,
    _parse_numeric_columns,
    _trim_string_columns,
)
from data_agent.tools._helpers import (
    _compact_number,
    _human_column_label,
    _nice_axis_formatter,
    _nice_num,
    _nice_ticks,
    _plotly_axis_tickformat,
)
from data_agent.workspace import PLOTLY_BUNDLE_NAME, DataWorkspace


def tool_map(workspace):
    return {item.name: item for item in build_tools(workspace)}


def plotly_values(values):
    """Decode Plotly 6 typed arrays while remaining compatible with plain lists."""
    if isinstance(values, dict) and "bdata" in values and "dtype" in values:
        return np.frombuffer(base64.b64decode(values["bdata"]), dtype=np.dtype(values["dtype"]))
    return values


def test_clean_data_updates_frame_and_exports(workspace):
    result = json.loads(
        tool_map(workspace)["clean_data"].invoke(
            {
                "columns": ["sales", "profit"],
                "drop_duplicates": True,
                "trim_strings": True,
                "missing_strategy": "median",
                "outlier_method": "none",
            }
        )
    )
    assert result["status"] == "ok"
    assert len(workspace.dataframe) == 5
    assert workspace.dataframe["sales"].isna().sum() == 0
    assert set(workspace.dataframe["category"]) == {"A", "B"}
    assert Path(result["output"]).exists()


def test_clean_data_refuses_high_volume_row_drop_even_with_columns(tmp_path):
    source = tmp_path / "mostly_missing.csv"
    pd.DataFrame({"keep": [1, 2, 3, 4], "notes": [None, None, None, "ok"]}).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="guard")
    workspace.load(source, copy_into_workspace=True)

    for columns in (None, ["notes"]):
        try:
            tool_map(workspace)["clean_data"].invoke(
                {
                    "columns": columns,
                    "drop_duplicates": False,
                    "trim_strings": False,
                    "missing_strategy": "drop",
                }
            )
        except Exception as exc:
            assert "高比例删行" in str(exc)
        else:
            raise AssertionError("expected the high-volume drop to be rejected")

    assert len(workspace.dataframe) == 4


def test_transform_data_exports_view_without_replacing_active_dataset(workspace):
    result = json.loads(
        tool_map(workspace)["transform_data"].invoke(
            {"filter_column": "sales", "filter_operator": "eq", "filter_value": 230}
        )
    )

    assert result["view_only"] is True
    assert result["rows"] == 1
    assert result["active_rows"] == 6
    assert len(workspace.dataframe) == 6
    assert len(pd.read_csv(result["output"])) == 1


def test_clean_data_refuses_cumulative_collapse_below_source_floor(tmp_path):
    source = tmp_path / "staged_missing.csv"
    pd.DataFrame(
        {
            "value": range(16),
            "stage_a": ["ok"] * 8 + [None] * 8,
            "stage_b": ["ok"] * 4 + [None] * 12,
            "stage_c": ["ok"] * 2 + [None] * 14,
        }
    ).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="cumulative_guard")
    workspace.load(source, copy_into_workspace=True)
    clean = tool_map(workspace)["clean_data"]
    common = {"drop_duplicates": False, "trim_strings": False, "missing_strategy": "drop"}

    clean.invoke({**common, "columns": ["stage_a"]})
    clean.invoke({**common, "columns": ["stage_b"]})
    assert len(workspace.dataframe) == 4
    try:
        clean.invoke({**common, "columns": ["stage_c"]})
    except Exception as exc:
        assert "累计删除过多" in str(exc)
    else:
        raise AssertionError("expected cumulative row-loss guard to reject the operation")
    assert len(workspace.dataframe) == 4


def test_repair_data_format_applies_only_unambiguous_repairs(tmp_path):
    source = tmp_path / "format_dirty.csv"
    pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02"],
            "sales": ["1,200", " 1300 "],
            "region": [" East ", "na"],
            "code": ["A001", "A002"],
            "profit": [-10, 20],
        }
    ).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs")
    workspace.load(source)

    result = json.loads(tool_map(workspace)["repair_data_format"].invoke({}))

    assert result["changed"] is True
    assert pd.api.types.is_datetime64_any_dtype(workspace.dataframe["date"])
    assert pd.api.types.is_numeric_dtype(workspace.dataframe["sales"])
    assert workspace.dataframe["region"].isna().sum() == 1
    assert not pd.api.types.is_numeric_dtype(workspace.dataframe["code"])
    assert workspace.dataframe["profit"].tolist() == [-10, 20]
    assert Path(result["output"]).exists()


def test_statistics_groupby_and_regression(workspace):
    tools = tool_map(workspace)
    grouped = json.loads(
        tools["statistical_analysis"].invoke(
            {"method": "groupby", "columns": ["sales"], "group_by": "region", "aggregation": "mean"}
        )
    )
    assert grouped["method"] == "groupby"
    regression = json.loads(
        tools["statistical_analysis"].invoke(
            {"method": "linear_regression", "columns": ["sales"], "target": "profit"}
        )
    )
    assert regression["r2"] > 0.8
    assert regression["sample_size"] == 5
    assert "adjusted_r2" in regression

    correlation = json.loads(
        tools["statistical_analysis"].invoke({"method": "correlation", "columns": ["sales", "profit"]})
    )
    assert correlation["sample_sizes"]["sales"]["profit"] == 5
    assert "p_values" in correlation


def test_complex_visualizations_create_offline_artifacts(workspace):
    result = json.loads(
        tool_map(workspace)["create_visualization"].invoke(
            {
                "chart_type": "correlation_heatmap",
                "dimensions": ["sales", "profit"],
                "title": "关系诊断",
            }
        )
    )
    html = Path(result["html"])
    chart_json = Path(result["plotly_json"])
    plotly_bundle = workspace.artifacts_dir / PLOTLY_BUNDLE_NAME
    assert html.exists() and html.stat().st_size > 5_000
    assert plotly_bundle.exists() and plotly_bundle.stat().st_size > 1_000_000
    assert chart_json.exists()
    # The bundle is shared, not registered as an artifact, and each chart HTML
    # references it relatively instead of inlining the full Plotly.js source.
    html_text = html.read_text(encoding="utf-8")
    assert "<script src='plotly.min.js'" in html_text
    # The HTML must NOT inline the multi-megabyte Plotly.js source.
    assert html.stat().st_size < plotly_bundle.stat().st_size
    assert {item["kind"] for item in workspace.artifacts} == {"visualization", "chart_data"}


def test_visualizations_keep_extreme_values_but_default_to_readable_scale(tmp_path):
    source = tmp_path / "extreme_sales.csv"
    pd.DataFrame(
        {
            "product": ["A", "B", "C", "D", "E", "F"],
            "units": [2, 3, 4, 5, 8, 9999],
            "revenue": [900, 1200, 1700, 2100, 3600, 2_990_001],
        }
    ).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="outlier_chart")
    workspace.load(source, copy_into_workspace=True)
    visualization = tool_map(workspace)["create_visualization"]

    bar_result = json.loads(
        visualization.invoke(
            {
                "chart_type": "bar",
                "x": "product",
                "y": "revenue",
                "aggregation": "sum",
                "title": "产品收入",
            }
        )
    )
    bar = pio.from_json(Path(bar_result["plotly_json"]).read_text(encoding="utf-8"))
    assert bar_result["scale_mode"] == "robust"
    assert bar_result["extreme_points"] == 1
    assert bar.layout.yaxis.range[1] < 10_000
    assert max(
        float(value)
        for trace in bar.data
        if trace.type == "bar"
        for value in plotly_values(trace.y)
    ) == 2_990_001
    assert [button.label for button in bar.layout.updatemenus[0].buttons] == ["主体尺度", "全量视图"]
    assert all(button.method == "relayout" for button in bar.layout.updatemenus[0].buttons)
    assert any("极端值超出主体尺度" in annotation.text for annotation in bar.layout.annotations)
    html_text = Path(bar_result["html"]).read_text(encoding="utf-8")
    assert "height:100%" in html_text
    assert '"displaylogo": false' in html_text

    scatter_result = json.loads(
        visualization.invoke(
            {
                "chart_type": "scatter",
                "x": "units",
                "y": "revenue",
                "color": "product",
                "title": "销量与收入",
            }
        )
    )
    scatter = pio.from_json(Path(scatter_result["plotly_json"]).read_text(encoding="utf-8"))
    assert scatter_result["scale_mode"] == "robust"
    assert scatter.layout.xaxis.range[1] < 20
    assert scatter.layout.yaxis.range[1] < 10_000
    # The real point remains in the base traces and is only outside the default viewport.
    assert max(
        float(value)
        for trace in scatter.data
        for value in plotly_values(trace.y)
    ) == 2_990_001


def test_grouped_bars_explain_absent_category_combinations(tmp_path):
    source = tmp_path / "category_coverage.csv"
    pd.DataFrame(
        {
            "product": ["音箱", "音箱", "键盘", "键盘", "键盘"],
            "channel": ["线上", "线上", "门店", "门店", "经销商"],
            "is_returned": [False, False, False, True, False],
            "rating": [4.8, 4.6, 4.0, 2.1, 3.9],
            "revenue": [900, 1100, 450, 400, 500],
        }
    ).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="coverage_chart")
    workspace.load(source, copy_into_workspace=True)
    visualization = tool_map(workspace)["create_visualization"]

    rating_result = json.loads(
        visualization.invoke(
            {
                "chart_type": "bar",
                "x": "product",
                "y": "rating",
                "color": "is_returned",
                "aggregation": "mean",
                "title": "评分与退货",
            }
        )
    )
    rating = pio.from_json(Path(rating_result["plotly_json"]).read_text(encoding="utf-8"))
    assert {trace.name for trace in rating.data} == {"否", "是"}
    assert rating_result["category_coverage"] == {
        "complete": False,
        "observed_combinations": 3,
        "total_combinations": 4,
        "missing_count": 1,
    }
    assert "组合覆盖 3/4" in rating.layout.title.text
    assert "不是数值为 0，也不是漏画" in rating.layout.title.text
    assert any(annotation.text == "无样本" for annotation in rating.layout.annotations)
    # hovertemplate 使用预计算的 hover 文本（customdata[2]），避免无记录 bar 显示 nan。
    assert all("customdata[2]" in (trace.hovertemplate or "") for trace in rating.data)
    # 验证预计算文本内容：有记录 bar 包含 "样本数"，无记录 bar 包含 "无样本"。
    all_hover_texts: list[str] = []
    for trace in rating.data:
        cd = trace.customdata
        if cd is None:
            continue
        for row in cd:
            # customdata 每行结构：[sample_count, has_records, hover_text]
            if len(row) >= 3:
                all_hover_texts.append(str(row[2]))
    assert any("样本数" in text for text in all_hover_texts)
    assert any("无样本" in text for text in all_hover_texts)

    channel_result = json.loads(
        visualization.invoke(
            {
                "chart_type": "bar",
                "x": "product",
                "y": "revenue",
                "color": "channel",
                "aggregation": "sum",
                "title": "收入渠道",
            }
        )
    )
    channel = pio.from_json(Path(channel_result["plotly_json"]).read_text(encoding="utf-8"))
    assert channel_result["category_coverage"]["observed_combinations"] == 3
    assert channel_result["category_coverage"]["total_combinations"] == 6
    assert channel_result["category_coverage"]["missing_count"] == 3
    assert sum(annotation.text == "○" for annotation in channel.layout.annotations) == 3


def _semantic_workspace(tmp_path):
    """构造含 ID 列/常量列/高基数列的数据集，验证图表意义性防护。"""
    rows = 60
    source = tmp_path / "semantic.csv"
    pd.DataFrame(
        {
            "order_id": [f"ORD{i:04d}" for i in range(rows)],
            "用户编号": range(1000, 1000 + rows),
            "region": ["East", "West", "North"] * (rows // 3),
            "city": [f"城市{i % 25}" for i in range(rows)],
            "constant": ["同一值"] * rows,
            "sales": [float(100 + (i * 37) % 400) for i in range(rows)],
        }
    ).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs", session_id="semantic_guard")
    workspace.load(source, copy_into_workspace=True)
    return workspace


def test_visualization_rejects_id_and_constant_columns(tmp_path):
    """意义性防护：ID 列分布图、常量列图必须被拒绝并给出修正建议。"""
    visualization = tool_map(_semantic_workspace(tmp_path))["create_visualization"]

    # 英文 ID 列（命名 + 逐行唯一）作柱状图 x → 拒绝
    for params in (
        {"chart_type": "bar", "x": "order_id", "aggregation": "count"},
        {"chart_type": "pie", "x": "用户编号", "values": "sales"},
        {"chart_type": "bar", "x": "region", "y": "sales", "aggregation": "sum", "color": "order_id"},
    ):
        try:
            visualization.invoke(params)
        except Exception as exc:
            assert "标识符" in str(exc)
        else:
            raise AssertionError(f"expected identifier-column chart to be rejected: {params}")

    # 常量列作任何角色 → 拒绝
    try:
        visualization.invoke({"chart_type": "histogram", "x": "constant"})
    except Exception as exc:
        assert "只有一个取值" in str(exc)
    else:
        raise AssertionError("expected constant-column chart to be rejected")


def test_visualization_high_cardinality_requires_top_n(tmp_path):
    """意义性防护：高基数饼图需要 top_n，传入后可正常生成；业务列不受影响。"""
    visualization = tool_map(_semantic_workspace(tmp_path))["create_visualization"]

    # 25 个类别的饼图（上限 20）→ 拒绝并提示 top_n
    try:
        visualization.invoke({"chart_type": "pie", "x": "city", "values": "sales"})
    except Exception as exc:
        assert "top_n" in str(exc)
    else:
        raise AssertionError("expected high-cardinality pie to be rejected")

    # 传 top_n 后通过防护并正常产出
    top_result = json.loads(
        visualization.invoke({"chart_type": "pie", "x": "city", "values": "sales", "top_n": 10})
    )
    assert Path(top_result["html"]).exists()

    # 低基数业务列不受防护影响
    ok_result = json.loads(
        visualization.invoke(
            {"chart_type": "bar", "x": "region", "y": "sales", "aggregation": "sum", "title": "区域销售"}
        )
    )
    assert Path(ok_result["html"]).exists()


# ---------------------------------------------------------------------------
# 自动选图：chart_type="auto" 应根据数据列类型与格式自动推断最合适的图型。
# 覆盖 _infer_chart_type 的全部 14 条决策分支，并验证图表实际生成无报错、
# HTML 文件包含 Plotly 初始化标记（防止"点开空白"回归）。
# ---------------------------------------------------------------------------

_AUTO_CHART_SCENARIOS = [
    # (名称, 数据字典, 额外 invoke 参数, 期望 chart_type)
    # 1. 时间序列（x 为日期 + y 数值）→ line
    (
        "time_series_line",
        {
            "date": pd.date_range("2025-01-01", periods=20, freq="D").astype(str),
            "sales": [100 + i * 5 for i in range(20)],
        },
        {"x": "date", "y": "sales"},
        "line",
    ),
    # 2. 时间序列（x 为日期，无 y）→ bar（计数）
    (
        "time_series_bar",
        {
            "date": pd.date_range("2025-01-01", periods=6, freq="ME").astype(str),
            "event": ["a", "b", "a", "c", "b", "a"],
        },
        {"x": "date"},
        "bar",
    ),
    # 3. 两数值关系 → scatter
    (
        "two_numeric_scatter",
        {
            "height": [160, 170, 175, 180, 165, 172, 168, 185, 190, 155],
            "weight": [55, 65, 70, 80, 58, 68, 62, 85, 90, 50],
        },
        {"x": "height", "y": "weight"},
        "scatter",
    ),
    # 4. 单数值分布（无 x，y 数值）→ histogram
    (
        "single_numeric_hist",
        {"score": [60, 70, 75, 80, 85, 90, 65, 72, 88, 95, 78, 82]},
        {"y": "score"},
        "histogram",
    ),
    # 5. 连续数值分布（x 数值，无 y，唯一值 > 12）→ histogram
    (
        "continuous_x_hist",
        {"value": list(range(100))},
        {"x": "value"},
        "histogram",
    ),
    # 6. 连续数值分布（x 数值，无 y，唯一值 ≤ 12）→ bar
    (
        "low_unique_numeric_bar",
        {"level": [1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 1, 2, 3, 4, 5]},
        {"x": "level"},
        "bar",
    ),
    # 7. 分类 + 数值（无 color，类别 ≤ 8）→ pie
    (
        "category_numeric_pie",
        {
            "product": ["A", "B", "C", "D"] * 5,
            "revenue": [100, 200, 150, 80] * 5,
        },
        {"x": "product", "y": "revenue"},
        "pie",
    ),
    # 8. 分类 + 数值（有 color）→ bar
    (
        "category_with_color_bar",
        {
            "region": ["East", "West", "East", "West", "East", "West"] * 3,
            "channel": ["online", "store", "online", "store", "online", "store"] * 3,
            "sales": [100, 200, 150, 180, 120, 210] * 3,
        },
        {"x": "region", "y": "sales", "color": "channel", "aggregation": "sum"},
        "bar",
    ),
    # 9. 分类 + 数值（无 color，类别 > 8）→ bar
    (
        "high_card_category_bar",
        {
            "city": [f"city_{i:02d}" for i in range(12)] * 4,
            "population": list(range(12)) * 4,
        },
        {"x": "city", "y": "population", "aggregation": "sum"},
        "bar",
    ),
    # 10. 分类计数（x 分类，无 y）→ bar
    (
        "category_count_bar",
        {
            "fruit": ["apple", "banana", "apple", "cherry", "banana", "apple"] * 3,
        },
        {"x": "fruit"},
        "bar",
    ),
    # 11. 无 x/y 但数值列 ≥ 3 → correlation_heatmap
    (
        "multi_numeric_heatmap",
        {
            "a": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "b": [2, 4, 6, 8, 10, 12, 14, 16, 18, 20],
            "c": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
        },
        {},
        "correlation_heatmap",
    ),
    # 12. 多维数值（dimensions ≥ 3，无 x/y）→ scatter_matrix
    (
        "scatter_matrix_3d",
        {
            "x1": list(range(20)),
            "x2": list(range(0, 40, 2)),
            "x3": list(range(0, 60, 3)),
        },
        {"dimensions": ["x1", "x2", "x3"]},
        "scatter_matrix",
    ),
    # 13. 层级结构（path_columns）→ sunburst
    (
        "hierarchy_sunburst",
        {
            "region": ["North", "North", "South", "South"] * 3,
            "city": ["A", "B", "C", "D"] * 3,
            "sales": [100, 200, 150, 80] * 3,
        },
        {"path_columns": ["region", "city"], "values": "sales"},
        "sunburst",
    ),
    # 14. 三维（z + x + y）→ scatter_3d
    (
        "three_d_scatter",
        {
            "x": list(range(15)),
            "y": list(range(0, 30, 2)),
            "z": list(range(0, 45, 3)),
        },
        {"x": "x", "y": "y", "z": "z"},
        "scatter_3d",
    ),
]


@pytest.mark.parametrize(
    "name,data,kwargs,expected",
    _AUTO_CHART_SCENARIOS,
    ids=[s[0] for s in _AUTO_CHART_SCENARIOS],
)
def test_auto_chart_type_infers_correct_type(tmp_path, name, data, kwargs, expected):
    """chart_type='auto' 应根据数据列类型自动选择合适的图型并成功生成 HTML。"""
    # 直接设置 dataframe，避免 CSV 序列化导致的单列数据列名损坏
    # （pandas to_csv/read_csv 在单列 + index=False 时偶发把列名首字符吞入 index）。
    workspace = DataWorkspace(tmp_path / "runs", session_id=name)
    workspace.dataframe = pd.DataFrame(data)

    result = json.loads(
        tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "title": name, **kwargs}
        )
    )
    assert result["chart_type"] == expected, (
        f"自动选图：场景「{name}」期望 {expected}，实际 {result['chart_type']}"
    )
    assert result["chart_type_source"] == "auto"

    html = Path(result["html"])
    assert html.exists(), f"HTML 文件未生成：{html}"
    assert html.stat().st_size > 5_000, f"HTML 文件过小，可能渲染失败：{html.stat().st_size}"
    html_text = html.read_text(encoding="utf-8")
    # 验证 HTML 包含 Plotly 初始化标记，防止"点开空白"回归
    assert "plotly.min.js" in html_text or "Plotly.newPlot" in html_text, (
        f"HTML 缺少 Plotly 初始化标记，预览将空白：{html}"
    )


# ---------------------------------------------------------------------------
# run_python_code 沙箱工具
# ---------------------------------------------------------------------------


def test_run_python_code_longtail_computation(workspace):
    """长尾计算：分组求和 + 环比，result 为 Series 时返回截断预览。"""
    result = json.loads(
        tool_map(workspace)["run_python_code"].invoke(
            {
                "code": (
                    'grouped = df.groupby("region")["profit"].sum()\n'
                    'print("groups:", len(grouped))\n'
                    "result = grouped"
                )
            }
        )
    )
    assert result["status"] == "ok"
    assert result["result"]["type"] == "series"
    assert result["result"]["values"]["East"] == 46.0
    assert "groups: 2" in result["stdout"]


def test_run_python_code_dataframe_result_truncated(workspace):
    """DataFrame result 截断到上限行数并标注原始规模。"""
    result = json.loads(
        tool_map(workspace)["run_python_code"].invoke(
            {"code": "result = pd.concat([df] * 20, ignore_index=True)"}
        )
    )
    assert result["result"]["type"] == "dataframe"
    assert result["result"]["rows"] == 120
    assert len(result["result"]["records"]) == 50
    assert result["result"]["truncated"] is True


def test_run_python_code_df_is_copy(workspace):
    """代码里改 df 不影响工作区主数据（沙箱只读契约）。"""
    before = len(workspace.dataframe)
    result = json.loads(
        tool_map(workspace)["run_python_code"].invoke(
            {"code": "df.drop(df.index, inplace=True)\nresult = len(df)"}
        )
    )
    assert result["result"] == 0
    assert len(workspace.dataframe) == before


def test_run_python_code_blocks_dangerous_code(workspace):
    """安全边界：白名单外 import、dunder 逃逸、危险内建、动态执行全部拒绝。"""
    tool = tool_map(workspace)["run_python_code"]
    for code, keyword in (
        ("import os\nresult = os.getcwd()", "禁止导入"),
        ("from subprocess import run\nresult = 1", "禁止导入"),
        ("result = ().__class__.__mro__", "下划线开头"),
        ("result = df._mgr", "下划线开头"),
        ("result = open('x.txt')", "禁止使用"),
        ("result = eval('1+1')", "禁止使用"),
        ("result = getattr(df, 'to_csv')", "禁止使用"),
        ("result = __import__('os')", "禁止使用"),
    ):
        try:
            tool.invoke({"code": code})
        except Exception as exc:
            assert keyword in str(exc), f"unexpected message for {code!r}: {exc}"
        else:
            raise AssertionError(f"expected sandbox to reject: {code!r}")


def test_run_python_code_allows_whitelisted_import(workspace):
    result = json.loads(
        tool_map(workspace)["run_python_code"].invoke(
            {"code": "import math\nresult = math.sqrt(16)"}
        )
    )
    assert result["result"] == 4.0


def test_run_python_code_runtime_error_surfaces(workspace):
    """运行期异常（如列不存在）原样回传，交给错误中间件生成引导文案。"""
    try:
        tool_map(workspace)["run_python_code"].invoke({"code": "result = df['不存在的列']"})
    except Exception as exc:
        assert "不存在的列" in str(exc)
    else:
        raise AssertionError("expected runtime KeyError to surface")


def test_run_python_code_timeout(workspace, monkeypatch):
    """死循环触发超时熔断，错误消息含引导文案。"""
    from data_agent.tools import _sandbox

    monkeypatch.setattr(_sandbox, "_SANDBOX_TIMEOUT_SECONDS", 0.5)
    try:
        tool_map(workspace)["run_python_code"].invoke(
            {"code": "x = 0\nwhile True:\n    x += 1"}
        )
    except Exception as exc:
        assert "熔断" in str(exc)
    else:
        raise AssertionError("expected timeout to trip")


# ---------------------------------------------------------------------------
# _cleaning.py 清洗辅助函数测试
# ---------------------------------------------------------------------------


class TestNormalizeColumnNames:
    """列名规范化：特殊字符→下划线、重复列名加序号、中文保留。"""

    def test_already_clean_no_change(self):
        df = pd.DataFrame({"a": [1], "b": [2]})
        result, cols, dt, changed = _normalize_column_names(df, None, None)
        assert changed is False
        assert list(result.columns) == ["a", "b"]

    def test_special_chars_normalized(self):
        df = pd.DataFrame({"First Name": [1], "Last-Name": [2], "Age!": [3]})
        result, cols, dt, changed = _normalize_column_names(df, ["First Name"], None)
        assert changed is True
        assert list(result.columns) == ["first_name", "last_name", "age"]
        assert cols == ["first_name"]

    def test_duplicate_names_get_suffix(self):
        df = pd.DataFrame([[1, 2, 3]], columns=["col", "col", "col"])
        result, _, _, changed = _normalize_column_names(df, None, None)
        assert changed is True
        assert list(result.columns) == ["col", "col_2", "col_3"]

    def test_chinese_columns_preserved(self):
        df = pd.DataFrame({"姓名": [1], "年龄": [2]})
        result, _, _, changed = _normalize_column_names(df, ["姓名"], ["年龄"])
        assert changed is False  # 中文列名不需要规范化
        assert list(result.columns) == ["姓名", "年龄"]

    def test_empty_column_name_fallback(self):
        df = pd.DataFrame({"": [1], " ": [2]})
        result, _, _, changed = _normalize_column_names(df, None, None)
        assert changed is True
        assert "column" in result.columns


class TestTrimStringColumns:
    """文本列修剪：去除首尾空格，非字符串值保持不变。"""

    def test_trims_whitespace(self):
        df = pd.DataFrame({"name": ["  Alice  ", " Bob ", ""]})
        count = _trim_string_columns(df)
        assert count == 1
        assert df["name"].tolist() == ["Alice", "Bob", ""]

    def test_non_string_values_unchanged(self):
        # None 在数值列中被 pandas 转为 NaN（float），此处只验证非字符串列
        # 不被 trim 操作改动，值集合等价即可。
        df = pd.DataFrame({"val": [1, 2, None], "name": [" A ", "B", " C "]})
        count = _trim_string_columns(df)
        assert count == 1
        assert df["val"].isna().sum() == 1
        assert df["val"].dropna().tolist() == [1.0, 2.0]
        assert df["name"].tolist() == ["A", "B", "C"]

    def test_no_string_columns(self):
        df = pd.DataFrame({"a": [1, 2], "b": [3.0, 4.0]})
        assert _trim_string_columns(df) == 0


class TestParseNumericColumns:
    """数值列解析：按阈值判断是否将文本列转为数值列。"""

    def test_high_numeric_ratio_converts(self):
        df = pd.DataFrame({"val": ["1", "2", "3", "4", "5"]})
        converted = _parse_numeric_columns(df, threshold=0.8)
        assert converted == ["val"]
        assert df["val"].dtype in ["int64", "float64"]

    def test_low_numeric_ratio_not_converted(self):
        df = pd.DataFrame({"val": ["1", "2", "abc", "def", "ghi"]})
        converted = _parse_numeric_columns(df, threshold=0.8)
        assert converted == []
        # pandas 2.x 可能把字符串列推断为 StringDtype 而非 object，二者都表示
        # 未被转为数值列，这里用 kind 判断避免版本差异。
        assert df["val"].dtype.kind in ("O", "S", "U")

    def test_empty_column_skipped(self):
        df = pd.DataFrame({"val": [None, None, None]})
        converted = _parse_numeric_columns(df)
        assert converted == []


class TestApplyMissingStrategy:
    """缺失值策略：drop/ffill/bfill/mean/median/mode。"""

    def test_drop_strategy(self):
        df = pd.DataFrame({"a": [1, None, 3, 4, 5]})
        _apply_missing_strategy(df, ["a"], "drop")
        assert len(df) == 4
        assert df["a"].isna().sum() == 0

    def test_drop_high_ratio_raises(self):
        df = pd.DataFrame({"a": [1, None, None, None, None]})
        with pytest.raises(ValueError, match="拒绝高比例删行"):
            _apply_missing_strategy(df, ["a"], "drop")

    def test_forward_fill(self):
        df = pd.DataFrame({"a": [1.0, None, 3.0, None, 5.0]})
        _apply_missing_strategy(df, ["a"], "forward_fill")
        assert df["a"].tolist() == [1.0, 1.0, 3.0, 3.0, 5.0]

    def test_backward_fill(self):
        # bfill 无法填充尾部 NaN（其后无值可回填），只验证前 4 行被正确填充。
        df = pd.DataFrame({"a": [None, 2.0, None, 4.0, None]})
        _apply_missing_strategy(df, ["a"], "backward_fill")
        assert df["a"].iloc[:4].tolist() == [2.0, 2.0, 4.0, 4.0]
        assert df["a"].iloc[4] is pd.NA or (isinstance(df["a"].iloc[4], float) and np.isnan(df["a"].iloc[4]))

    def test_mean_fill(self):
        df = pd.DataFrame({"a": [10.0, 20.0, None, 30.0]})
        _apply_missing_strategy(df, ["a"], "mean")
        assert df["a"].isna().sum() == 0
        assert abs(df["a"].iloc[2] - 20.0) < 0.01

    def test_median_fill(self):
        # median of [10, 20, 30] = 20.0（偶数个时取中间两数均值，此处 3 个取中位 20）
        df = pd.DataFrame({"a": [10.0, 20.0, None, 30.0]})
        _apply_missing_strategy(df, ["a"], "median")
        assert df["a"].isna().sum() == 0
        assert df["a"].iloc[2] == 20.0

    def test_mode_fill(self):
        df = pd.DataFrame({"cat": ["A", "B", "A", None, "A"]})
        _apply_missing_strategy(df, ["cat"], "mode")
        assert df["cat"].isna().sum() == 0
        assert df["cat"].iloc[3] == "A"

    def test_mean_on_non_numeric_raises(self):
        df = pd.DataFrame({"cat": ["A", "B", None]})
        with pytest.raises(ValueError, match="不是数值列"):
            _apply_missing_strategy(df, ["cat"], "mean")

    def test_no_missing_no_change(self):
        df = pd.DataFrame({"a": [1.0, 2.0, 3.0]})
        _apply_missing_strategy(df, ["a"], "mean")
        assert df["a"].tolist() == [1.0, 2.0, 3.0]


class TestHandleOutliers:
    """离群值检测与处理：IQR/zscore，cap/remove 两种动作。"""

    def test_iqr_cap(self):
        df = pd.DataFrame({"a": [1, 2, 3, 4, 100]})
        count, bounds = _handle_outliers(df, ["a"], "iqr", "cap")
        assert count == 1
        assert "a" in bounds
        assert df["a"].max() < 100

    def test_iqr_remove(self):
        df = pd.DataFrame({"a": [1, 2, 3, 4, 5, 100]})
        count, _ = _handle_outliers(df, ["a"], "iqr", "remove")
        assert count >= 1
        assert 100 not in df["a"].values

    def test_iqr_remove_high_ratio_raises(self):
        # 9 个值，Q1(index2)=5, Q3(index6)=5, IQR=0。
        # 离群值 1/100/200 占 3/9=33.3% > 30% 阈值，remove 时应被拒绝。
        df = pd.DataFrame({"a": [1, 5, 5, 5, 5, 5, 5, 100, 200]})
        with pytest.raises(ValueError, match="拒绝一次删除过多"):
            _handle_outliers(df, ["a"], "iqr", "remove")

    def test_zscore_cap(self):
        # 10 个 1 + 1 个 100：mean≈10, std≈28.5, z(100)≈3.16 > 3 → 离群。
        df = pd.DataFrame({"a": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 100]})
        count, bounds = _handle_outliers(df, ["a"], "zscore", "cap")
        assert count == 1
        assert "a" in bounds

    def test_zscore_zero_std_skipped(self):
        df = pd.DataFrame({"a": [5, 5, 5, 5, 5]})
        count, bounds = _handle_outliers(df, ["a"], "zscore", "cap")
        assert count == 0
        assert "a" not in bounds

    def test_no_numeric_columns(self):
        # _numeric_columns 在无数值列时抛 ValueError，_handle_outliers 透传该异常。
        df = pd.DataFrame({"cat": ["A", "B", "C"]})
        with pytest.raises(ValueError, match="没有可用于该操作的数值列"):
            _handle_outliers(df, ["cat"], "iqr", "cap")


# ---------------------------------------------------------------------------
# _helpers.py 辅助函数测试
# ---------------------------------------------------------------------------


class TestHumanColumnLabel:
    """列名可读化：业务映射优先，回退下划线转空格。"""

    def test_known_business_label(self):
        assert _human_column_label("sales") == "销售额"
        assert _human_column_label("revenue") == "收入"
        assert _human_column_label("order_date") == "订单日期"

    def test_fallback_underscore_to_space(self):
        assert _human_column_label("first_name") == "first name"
        assert _human_column_label("user_id") == "user id"

    def test_empty_or_none(self):
        assert _human_column_label(None) == ""
        assert _human_column_label("") == ""


class TestCompactNumber:
    """紧凑数值格式化：M/K/千分位。"""

    def test_millions(self):
        assert _compact_number(1_500_000) == "1.50M"

    def test_thousands(self):
        assert _compact_number(35_600) == "35.6K"

    def test_tens(self):
        assert _compact_number(42) == "42"

    def test_small_number(self):
        result = _compact_number(3.14)
        assert "3.14" in result


class TestNiceNum:
    """nice number 算法：对齐到 1/2/5/10 的倍数。"""

    @pytest.mark.parametrize("x,round_,expected", [
        (0, True, 0.0),
        (0, False, 0.0),
        # round_=True 用 <= 判断：1.3<=1.5→1.0, 3.5<=7.0→5.0, 8.0>7.0→10.0
        (1.3, True, 1.0),
        (3.5, True, 5.0),
        (8.0, True, 10.0),
        # round_=False 用 < 判断：1.3<1.5→1.0, 3.5<7.0→5.0, 8.0>=7.0→10.0
        (1.3, False, 1.0),
        (3.5, False, 5.0),
        (8.0, False, 10.0),
        # 负数：取绝对值计算 nice_fraction 后乘以符号
        (-7.0, True, -5.0),   # 7.0<=7.0→5.0
        (-7.0, False, -10.0), # 7.0>=7.0→10.0
        # 26: exp=1, fraction=2.6, 2.6<=3.0→2.0, result=2.0*10=20.0
        (26, True, 20.0),
        (26, False, 20.0),
    ])
    def test_nice_num_values(self, x, round_, expected):
        assert _nice_num(x, round_) == expected


class TestNiceTicks:
    """nice ticks：生成圆数刻度范围与步长。"""

    def test_normal_range(self):
        vmin, vmax, step = _nice_ticks(0, 100, 5)
        assert step > 0
        assert vmin <= 0
        assert vmax >= 100

    def test_vmin_equals_vmax(self):
        vmin, vmax, step = _nice_ticks(50, 50)
        assert vmin < 50 < vmax
        assert step > 0

    def test_vmin_equals_vmax_zero(self):
        vmin, vmax, step = _nice_ticks(0, 0)
        assert vmin < 0 < vmax
        assert step > 0

    def test_swapped_min_max(self):
        vmin1, vmax1, step1 = _nice_ticks(10, 90)
        vmin2, vmax2, step2 = _nice_ticks(90, 10)
        assert vmin1 == vmin2
        assert vmax1 == vmax2

    def test_negative_range(self):
        vmin, vmax, step = _nice_ticks(-50, 50)
        assert vmin <= -50
        assert vmax >= 50
        assert step > 0

    def test_small_range(self):
        vmin, vmax, step = _nice_ticks(0.1, 0.9)
        assert step > 0
        assert vmin <= 0.1


class TestNiceAxisFormatter:
    """大数值自适应单位格式化：亿/万/千分位/小数/科学计数。"""

    @pytest.mark.parametrize("value,expected_contains", [
        (0, "0"),
        (150_000_000, "1.5亿"),
        (35_000, "3.5万"),
        (5_000, "5,000"),
        (500, "500"),
        (25.5, "25.5"),
        (3.14, "3.14"),
        (0.05, "0.05"),
        (0.005, "0.005"),
        (0.0001, "e-"),
        (-1_000_000, "-100万"),
        (-500, "-500"),
    ])
    def test_format_values(self, value, expected_contains):
        result = _nice_axis_formatter(value)
        assert expected_contains in result


class TestPlotlyAxisTickformat:
    """Plotly tickformat 生成：根据数值范围选择合适格式。"""

    @pytest.mark.parametrize("value_range,expected", [
        ((0, 0), ",.0f"),
        ((0, 5000), ",.0f"),
        ((0, 50), ",.1f"),
        ((0, 5), ".2f"),
        ((0, 0.05), ".3f"),
        ((0, 0.005), ".4f"),
        ((0, 0.0001), ".2e"),
    ])
    def test_tickformat_values(self, value_range, expected):
        assert _plotly_axis_tickformat(value_range) == expected
