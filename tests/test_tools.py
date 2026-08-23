from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import patch

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
        # 扩充映射：数量/客户细分/订单编号等常见业务列名
        assert _human_column_label("quantity") == "数量"
        assert _human_column_label("qty") == "数量"
        assert _human_column_label("customer_segment") == "客户细分"
        assert _human_column_label("order_id") == "订单编号"
        assert _human_column_label("unit_price") == "单价"

    def test_fallback_underscore_to_space(self):
        assert _human_column_label("first_name") == "first name"
        assert _human_column_label("user_id") == "user id"
        # 未收录但语义明确的列名保持原文（可溯源）
        assert _human_column_label("sales_target") == "sales target"

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


# ---------------------------------------------------------------------------
# 剩余分支：datetime 列映射 / mode 空 / nice_ticks 边界 / 沙箱错误路径
# ---------------------------------------------------------------------------


class TestNormalizeColumnNamesExtra:
    def test_datetime_columns_remapped(self):
        df = pd.DataFrame({"First Date": [1], "Sales!": [2]})
        result, cols, dt, changed = _normalize_column_names(df, ["Sales!"], ["First Date"])
        assert changed is True
        assert cols == ["sales"]
        assert dt == ["first_date"]


def test_apply_missing_strategy_mode_on_all_nan():
    df = pd.DataFrame({"cat": [None, None]})
    _apply_missing_strategy(df, ["cat"], "mode")  # mode 为空 → continue 不抛
    assert df["cat"].isna().sum() == 2


def test_summarize_result_none_and_stdout_truncation(workspace, monkeypatch):
    """_summarize_result(None) 与 print 输出截断分支。"""
    from data_agent.tools import _sandbox
    from data_agent.tools._sandbox import _summarize_result

    assert _summarize_result(None) is None  # 192

    monkeypatch.setattr(_sandbox, "_SANDBOX_STDOUT_MAX_CHARS", 100)
    tool = tool_map(workspace)["run_python_code"]
    result = json.loads(tool.invoke({"code": "print('x' * 500)\nresult = 1"}))
    assert result["status"] == "ok"
    assert "已截断" in result["stdout"]  # 323


def test_run_python_code_memory_monitor_process_gone(workspace, monkeypatch):
    """内存监控线程遇到进程消失（psutil 异常）应静默退出（256-257 分支）。"""
    import psutil

    class GoneProcess:
        def memory_info(self):
            raise psutil.NoSuchProcess(999)

    # _sandbox 在函数内 `import psutil`，拿到的是同一模块对象，替换其 Process
    monkeypatch.setattr(psutil, "Process", lambda *a: GoneProcess())
    tool = tool_map(workspace)["run_python_code"]
    # 加长计算让 worker 存活到 monitor 首次采样（0.5s 间隔）
    result = json.loads(
        tool.invoke(
            {
                "code": (
                    "n = 0\n"
                    "for i in range(30_000_000):\n"
                    "    n += i\n"
                    "result = n"
                )
            }
        )
    )
    assert result["status"] == "ok"


def test_run_python_code_memory_monitor_start_failure(workspace, monkeypatch):
    """psutil.Process() 抛错时监控线程启动失败应被吞掉（268-270 分支）。"""
    import psutil


    def boom(*args, **kwargs):
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr(psutil, "Process", boom)
    tool = tool_map(workspace)["run_python_code"]
    result = json.loads(tool.invoke({"code": "result = 2 + 2"}))
    assert result["status"] == "ok"


def test_nice_ticks_step_zero_fallback(monkeypatch):
    """_nice_num 返回 0 时 step 应回退 1.0（防御分支）。"""
    from data_agent.tools import _helpers

    monkeypatch.setattr(_helpers, "_nice_num", lambda x, round_=True: 0.0)
    vmin, vmax, step = _helpers._nice_ticks(0, 100, 5)
    assert step == 1.0
    assert vmin <= 0 <= 100 <= vmax


def test_run_python_code_rejects_oversized_code(workspace):
    from data_agent.tools import _sandbox

    tool = tool_map(workspace)["run_python_code"]
    long_code = "result = 1\n" * (_sandbox._SANDBOX_CODE_MAX_CHARS // 10 + 1)
    with pytest.raises(ValueError, match="代码过长"):
        tool.invoke({"code": long_code})


def test_run_python_code_rejects_syntax_error(workspace):
    tool = tool_map(workspace)["run_python_code"]
    with pytest.raises(ValueError, match="语法错误"):
        tool.invoke({"code": "def broken(:"})


def test_safe_import_rejects_non_whitelisted():
    from data_agent.tools._sandbox import _safe_import

    with pytest.raises(ImportError):
        _safe_import("os")
    with pytest.raises(ImportError):
        _safe_import("pandas.io", level=1)


def test_summarize_result_ndarray_truncation():
    from data_agent.tools._sandbox import _summarize_result

    result = _summarize_result(np.arange(100))
    assert result["type"] == "ndarray"
    assert result["truncated"] is True
    assert len(result["values"]) == 50
    # 小 ndarray 直接转 list
    assert _summarize_result(np.array([1, 2])) == [1, 2]


def test_run_python_code_memory_limit_error(workspace, monkeypatch):
    """worker 结束后内存仍超限应抛 MemoryError（283-284 分支）。"""
    from data_agent.tools import _sandbox

    monkeypatch.setattr(_sandbox, "_SANDBOX_MEMORY_LIMIT_BYTES", 1024)  # 1KB
    tool = tool_map(workspace)["run_python_code"]
    with pytest.raises(MemoryError, match="内存"):
        tool.invoke(
            {
                "code": (
                    "b = bytes(4 * 1024 * 1024)\n"
                    "n = 0\n"
                    "for i in range(20_000_000):\n"
                    "    n += i\n"
                    "result = len(b) + n"
                )
            }
        )


def test_run_python_code_memory_timeout_while_alive(workspace, monkeypatch):
    """worker 超时未结束且内存超限应抛 MemoryError（267-270 分支）。"""
    from data_agent.tools import _sandbox

    monkeypatch.setattr(_sandbox, "_SANDBOX_MEMORY_LIMIT_BYTES", 1024)
    monkeypatch.setattr(_sandbox, "_SANDBOX_TIMEOUT_SECONDS", 0.5)
    tool = tool_map(workspace)["run_python_code"]
    with pytest.raises(MemoryError, match="内存"):
        tool.invoke({"code": "b = bytes(4 * 1024 * 1024)\nwhile True:\n    pass"})


# ---------------------------------------------------------------------------
# builder.py：nice ticks 边界 / 统计分支 / 图表类型分支 / 导出分支
# ---------------------------------------------------------------------------


def test_apply_plotly_nice_ticks_edge_cases():
    import plotly.express as px

    from data_agent.tools.builder import _apply_plotly_nice_ticks, _collect_trace_values

    # 无 y 值 → 直接返回不报错（202 分支）
    fig = px.scatter(x=[1, 2], y=[3, 4])
    fig.data[0].y = None
    _apply_plotly_nice_ticks(fig, "scatter", "x", "y", {})

    # scatter_3d：z_range 存在分支（234）
    fig3d = px.scatter_3d(x=[1, 2], y=[3, 4], z=[5, 6])
    _apply_plotly_nice_ticks(fig3d, "scatter_3d", "x", "y", {"axis_ranges": {"z": (0, 10)}})

    # scatter_3d：无 z 值 → return（238）
    fig3b = px.scatter_3d(x=[1, 2], y=[3, 4], z=[5, 6])
    fig3b.data[0].z = None
    _apply_plotly_nice_ticks(fig3b, "scatter_3d", "x", "y", {})

    # 异常吞掉（250-252）：nice ticks 失败不影响图表
    fig2 = px.scatter(x=[1, 2], y=[3, 4])
    with patch("data_agent.tools.builder._nice_ticks", side_effect=RuntimeError("boom")):
        _apply_plotly_nice_ticks(fig2, "scatter", "x", "y", {})  # 不应抛出

    # "极端值提示" trace 跳过 + 非数值跳过（259-260 / 269-270）
    fig3 = px.scatter(x=[1, 2], y=[3, 4])
    fig3.add_scatter(x=[10], y=[999], name="极端值提示")
    fig3.data[1].y = ["bad", 999]
    values = _collect_trace_values(fig3, "y")
    assert 999 not in values
    assert values == [3.0, 4.0]


def test_clean_data_parse_numeric_and_outlier(workspace):
    """clean_data 的 parse_numeric 与 outlier 处理分支。"""
    result = json.loads(
        tool_map(workspace)["clean_data"].invoke(
            {
                "parse_numeric": True,
                "outlier_method": "iqr",
                "outlier_action": "cap",
                "drop_duplicates": False,
                "trim_strings": False,
                "missing_strategy": "none",
            }
        )
    )
    assert result["status"] == "ok"
    assert any("parsed numeric" in change for change in result["changes"])
    assert any("capped" in change for change in result["changes"])


def test_statistical_analysis_correlation_insufficient_pairs(tmp_path):
    """相关分析中有效配对 < 3 或值无变化时 p_value 应为 None。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id="corr_pairs")
    workspace.dataframe = pd.DataFrame(
        {
            "a": [1.0, 2.0, 3.0],
            "b": [None, None, 1.0],  # 有效配对仅 1 对 < 3
            "c": [5.0, 5.0, 5.0],  # 常量 → nunique < 2
        }
    )
    result = json.loads(
        tool_map(workspace)["statistical_analysis"].invoke({"method": "correlation"})
    )
    assert result["p_values"]["a"]["b"] is None
    assert result["p_values"]["a"]["c"] is None


def test_statistical_analysis_groupby_requires_group(workspace):
    with pytest.raises(ValueError, match="group_by"):
        tool_map(workspace)["statistical_analysis"].invoke({"method": "groupby"})


def test_auto_bar_with_duplicate_x_auto_aggregates(tmp_path):
    """auto 推断 bar + 无 color + 重复 x → 自动按 x 求和聚合（792 分支）。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id="auto_agg")
    workspace.dataframe = pd.DataFrame(
        {"cat": [f"c{i % 12}" for i in range(48)], "sales": [float(i) for i in range(48)]}
    )
    result = json.loads(
        tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "auto", "x": "cat", "y": "sales"}
        )
    )
    assert result["status"] == "ok"
    assert result["chart_type"] == "bar"
    assert Path(result["html"]).exists()


def test_plotly_area_box_violin_charts(workspace):
    """Plotly 的 area/box/violin 渲染分支。"""
    tools = tool_map(workspace)
    for chart_type in ("area", "box", "violin"):
        result = json.loads(
            tools["create_visualization"].invoke(
                {"chart_type": chart_type, "x": "region", "y": "sales"}
            )
        )
        assert result["status"] == "ok", f"{chart_type} 失败: {result}"
        assert Path(result["html"]).exists()


def test_plotly_large_scatter_switches_to_webgl(tmp_path):
    """大数据（>10K 行）Plotly 散点/折线应切换为 scattergl（WebGL），
    否则 SVG 全量渲染 30 万点以分钟计（实测浏览器 30s 无反应）。"""
    import numpy as np

    rng = np.random.default_rng(7)
    n = 12_000
    big = pd.DataFrame(
        {
            "region": ["East", "West"] * (n // 2),
            "sales": rng.uniform(100, 5_000, n),
            "profit": rng.uniform(10, 1_500, n),
        }
    )
    source = tmp_path / "big.csv"
    big.to_csv(source, index=False)
    ws = DataWorkspace(tmp_path / "runs", session_id="big")
    ws.load(source, copy_into_workspace=True)
    tools = tool_map(ws)

    # 散点：轨迹应被换成 scattergl，且 marker/数据保留
    result = json.loads(
        tools["create_visualization"].invoke(
            {"chart_type": "scatter", "x": "profit", "y": "sales", "color": "region"}
        )
    )
    assert result["status"] == "ok"
    payload = json.loads(Path(result["plotly_json"]).read_text(encoding="utf-8"))
    assert payload["data"][0]["type"] == "scattergl"
    assert payload["data"][0]["marker"]["color"]
    # plotly.py 把 numpy 数组写成 typed-array（{"dtype","bdata"}），解码验证点数
    import base64 as _b64

    x = payload["data"][0]["x"]
    if isinstance(x, dict):
        arr = np.frombuffer(_b64.b64decode(x["bdata"]), dtype=np.dtype(x["dtype"]))
    else:
        arr = np.asarray(x)
    # color 分组时每个 trace 只带本组数据（2 组 × 6000 = 12000）
    assert arr.size * len(payload["data"]) == n
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert "scattergl" in html_text

    # 折线同样切换
    result_line = json.loads(
        tools["create_visualization"].invoke(
            {"chart_type": "line", "x": "sales", "y": "profit", "aggregation": "none", "title": "序列"}
        )
    )
    line_payload = json.loads(Path(result_line["plotly_json"]).read_text(encoding="utf-8"))
    assert line_payload["data"][0]["type"] == "scattergl"

    # 小数据仍保持普通 scatter（不切换）
    small_ws = DataWorkspace(tmp_path / "runs_small", session_id="small")
    big.head(50).to_csv(tmp_path / "small.csv", index=False)
    small_ws.load(tmp_path / "small.csv", copy_into_workspace=True)
    small_result = json.loads(
        tool_map(small_ws)["create_visualization"].invoke(
            {"chart_type": "scatter", "x": "profit", "y": "sales"}
        )
    )
    small_payload = json.loads(Path(small_result["plotly_json"]).read_text(encoding="utf-8"))
    assert small_payload["data"][0]["type"] == "scatter"


def test_visualization_falls_back_to_full_html_without_bundle(workspace, monkeypatch):
    """plotly bundle 不可用时应回退到内联 plotlyjs 的完整 HTML（1093 分支）。"""
    monkeypatch.setattr(workspace, "ensure_plotly_bundle", lambda: None)
    result = json.loads(
        tool_map(workspace)["create_visualization"].invoke(
            {"chart_type": "bar", "x": "region", "y": "sales", "aggregation": "sum"}
        )
    )
    assert result["status"] == "ok"
    html_text = Path(result["html"]).read_text(encoding="utf-8")
    assert "Plotly.newPlot" in html_text


@pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="Kaleido 无头渲染在 CI 上偶发挂起且无法被信号超时中断（本地已验证）",
)
def test_create_visualization_export_png_success(workspace):
    """export_png=True 且 kaleido 可用时应生成 PNG 并注册 image 产物。"""
    result = json.loads(
        tool_map(workspace)["create_visualization"].invoke(
            {
                "chart_type": "bar",
                "x": "region",
                "y": "sales",
                "aggregation": "sum",
                "export_png": True,
            }
        )
    )
    assert result["status"] == "ok"
    assert "png" in result
    png_path = Path(result["png"])
    assert png_path.exists()
    assert png_path.suffix == ".png"
    assert png_path.read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# charts.py：聚合/语义防护/解读文本/自动选图边界分支
# ---------------------------------------------------------------------------


def test_aggregate_for_chart_requires_y_for_non_count(workspace):
    from data_agent.tools.charts import _aggregate_for_chart

    with pytest.raises(ValueError, match="聚合需要 y"):
        _aggregate_for_chart(workspace.dataframe, x="region", y=None, color=None, aggregation="sum")


def test_aggregate_for_chart_all_nan_x_returns_early(workspace):
    from data_agent.tools.charts import _aggregate_for_chart

    df = pd.DataFrame({"x": [None, None], "color": ["A", "B"], "y": [1.0, 2.0]})
    result, y, coverage = _aggregate_for_chart(df, x="x", y="y", color="color", aggregation="sum")
    assert coverage["complete"] is True


def test_aggregate_for_chart_skips_reindex_when_too_large(workspace):
    from data_agent.tools.charts import _aggregate_for_chart

    df = pd.DataFrame(
        {
            "x": [f"x{i}" for i in range(60)],
            "color": [f"c{i}" for i in range(60)],
            "y": [1.0] * 60,
        }
    )
    result, y, coverage = _aggregate_for_chart(df, x="x", y="y", color="color", aggregation="sum")
    assert coverage.get("skipped_reindex") is True


def test_append_title_note_sup_branch():
    import plotly.graph_objects as go

    from data_agent.tools.charts import _append_title_note

    fig = go.Figure()
    fig.update_layout(title_text="标题<br><sup>注1</sup>")
    _append_title_note(fig, "注2")
    assert "注2</sup>" in fig.layout.title.text
    assert "；" in fig.layout.title.text


def test_annotate_extreme_values_skips_non_bar_line_and_swallows_errors():
    import plotly.graph_objects as go

    from data_agent.tools.charts import _annotate_extreme_values

    # pie trace → continue（230）
    fig = go.Figure(go.Pie(labels=["a"], values=[1]))
    _annotate_extreme_values(fig)
    assert len(fig.layout.annotations) == 0

    # x/y 长度不匹配 → continue（234）
    fig2 = go.Figure(go.Bar(x=["a", "b"], y=[1]))
    _annotate_extreme_values(fig2)
    assert len(fig2.layout.annotations) == 0

    # 异常吞掉（279-280）
    fig3 = go.Figure(go.Bar(x=["a"], y=[1]))
    with patch("data_agent.tools.charts.max", side_effect=RuntimeError("boom")):
        _annotate_extreme_values(fig3)  # 不应抛出


def test_severe_axis_compression_zero_spread_and_low_ratio():
    from data_agent.tools.charts import _severe_axis_compression

    # spread==0 → MAD 路径；mad==0 → None（296-301）
    assert _severe_axis_compression([5, 5, 5, 5, 5, 100]) is None
    # 压缩比 < 8 → None（316）
    assert _severe_axis_compression([1, 2, 3, 4, 5, 6, 7, 8]) is None
    # 正常场景
    result = _severe_axis_compression([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1000])
    assert result is not None
    assert result["upper"] < 1000


def test_trace_axis_values_skips_outlier_hint():
    import plotly.graph_objects as go

    from data_agent.tools.charts import _trace_axis_values

    fig = go.Figure()
    fig.add_scatter(x=[1, 2], y=[3, 4])
    fig.add_scatter(x=[10], y=[999], name="极端值提示")
    assert _trace_axis_values(fig, "y") == [3.0, 4.0]


def test_apply_outlier_scale_controls_empty_trace_and_callout():
    import plotly.graph_objects as go

    from data_agent.tools.charts import _apply_outlier_scale_controls

    # 极端点 4 个 → callout 带"另有 N 个极端点"（408）
    fig = go.Figure()
    fig.add_bar(x=["a", "b", "c", "d", "e"], y=[1, 2, 3, 4, 500])
    result = _apply_outlier_scale_controls(fig, "bar", "robust")
    assert result["scale_mode"] == "robust"
    assert result["extreme_points"] >= 1

    # 无 x/y 的 trace → continue（372）
    fig2 = go.Figure()
    fig2.add_bar(x=["a"], y=[1])
    fig2.add_annotation(text="x")  # 无 trace 数据
    result2 = _apply_outlier_scale_controls(fig2, "bar", "robust")
    assert result2["scale_mode"] in ("robust", "full")


def test_validate_chart_semantics_high_cardinality(workspace):
    from data_agent.tools.charts import _validate_chart_semantics

    df = pd.DataFrame(
        {
            "类目": [f"c{i % 80}" for i in range(100)],  # 80 唯一（ratio 0.8 < 0.95，非 ID）
            "数值": list(range(100)),
            "分组": [f"g{i % 70}" for i in range(100)],  # 70 唯一
        }
    )
    # bar 高基数 x → raise（616）
    with pytest.raises(ValueError, match="类别过多"):
        _validate_chart_semantics(df, chart_type="bar", x="类目")
    # heatmap x 高基数 → raise（622）
    with pytest.raises(ValueError, match="类别过多"):
        _validate_chart_semantics(df, chart_type="heatmap", x="类目", y="分组", values="数值")
    # color 高基数 → raise（630）
    df_small_x = pd.DataFrame(
        {
            "类别": [f"a{i % 5}" for i in range(100)],
            "数值": list(range(100)),
            "类目": [f"c{i % 80}" for i in range(100)],
        }
    )
    with pytest.raises(ValueError, match="图例不可读"):
        _validate_chart_semantics(df_small_x, chart_type="bar", x="类别", y="数值", color="类目")
    # path_columns 高基数 → raise（636）
    with pytest.raises(ValueError, match="层级图不可读"):
        _validate_chart_semantics(df, chart_type="sunburst", path_columns=["类目"], values="数值")
    # 单行数据直接返回（566）
    _validate_chart_semantics(df.iloc[:1], chart_type="bar", x="类目")


def test_looks_like_id_column_empty_frame():
    from data_agent.tools.charts import _looks_like_id_column

    df = pd.DataFrame({"id": pd.Series(dtype="object")})
    assert _looks_like_id_column(df, "id", strict=True) is False  # rows==0 → False（531）


def test_looks_like_datetime_series_empty_and_errors(monkeypatch):
    import pandas as pd

    from data_agent.tools.charts import _looks_like_datetime_series

    # 空 sample → False（663）
    assert _looks_like_datetime_series(pd.Series([None, None], dtype="object")) is False
    # to_datetime 抛异常 → False（666-667）
    monkeypatch.setattr("data_agent.tools.charts.pd.to_datetime", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _looks_like_datetime_series(pd.Series(["2025-01-01", "2025-01-02"])) is False


def test_infer_chart_type_dimensions_two_and_no_columns():
    from data_agent.tools.charts import _infer_chart_type

    df = pd.DataFrame({"a": [1, 2], "b": [3, 4], "c": ["x", "y"]})
    result = _infer_chart_type(
        df, x=None, y=None, color=None, z=None, size=None, values=None,
        path_columns=None, dimensions=["a", "b"], aggregation="none", top_n=None,
    )
    assert result == "scatter"  # 2 维 → scatter（720）
    df2 = pd.DataFrame({"t": ["x", "y"]})
    with pytest.raises(ValueError, match="auto 模式下无法确定"):
        _infer_chart_type(
            df2, x=None, y=None, color=None, z=None, size=None, values=None,
            path_columns=None, dimensions=None, aggregation="none", top_n=None,
        )  # 无 x/y 无数值列 → raise（732）


def test_plotly_interpretation_branches():
    from data_agent.tools.charts import (
        _plotly_auto_interpret,
        _plotly_interpret_box,
        _plotly_interpret_pie,
        _plotly_interpret_scatter,
        _plotly_interpret_trend,
    )

    # box/violin 解读（841/954）
    box_text = _plotly_auto_interpret(
        pd.DataFrame({"g": ["a", "b"], "v": [1.0, 2.0]}), chart_type="box", x="g", y="v",
        color=None, aggregation="none", title="分组分布",
    )
    assert "箱体" in box_text

    # trend color 分支 pivot 空（861）：color 列全 NaN → groupby 无有效组
    trend_text = _plotly_interpret_trend(
        pd.DataFrame({"x": ["a", "b"], "y": [1.0, 2.0], "c": [None, None]}), chart_type="line",
        x="x", y="y", color="c", aggregation="sum", title="趋势",
    )
    assert "分组对比" in trend_text

    # 少于 3 个点 → 波动描述（901）
    short = _plotly_interpret_trend(
        pd.DataFrame({"x": ["a", "b"], "y": [1.0, 2.0]}), chart_type="line",
        x="x", y="y", color=None, aggregation="sum", title="短序列",
    )
    assert "波动" in short

    # pie 无数值列（910）与总和 ≤ 0（914）
    pie_no_num = _plotly_interpret_pie(pd.DataFrame({"cat": ["a"]}), x="cat", title="占比")
    assert "占比" in pie_no_num
    pie_zero = _plotly_interpret_pie(pd.DataFrame({"cat": ["a", "b"], "v": [0.0, -1.0]}), x="cat", title="占比")
    assert "占比" in pie_zero

    # scatter 非数值列（930）
    scatter_text = _plotly_interpret_scatter(
        pd.DataFrame({"x": ["a", "b"], "y": ["c", "d"]}), x="x", y="y", title="关系"
    )
    assert "分布关系" in scatter_text

    # scatter corr NaN（936）
    nan_text = _plotly_interpret_scatter(
        pd.DataFrame({"x": [1.0, 1.0], "y": [2.0, 2.0]}), x="x", y="y", title="关系"
    )
    assert "NaN" not in nan_text and "展示" in nan_text

    # box 本体（954）
    assert "四分位距" in _plotly_interpret_box(x="g", y="v", title="箱线")


# ---------------------------------------------------------------------------
# charts.py 剩余分支：MAD 路径 / 压缩比 / 空 trace / 高基数 heatmap / 推断边界
# ---------------------------------------------------------------------------


def test_severe_axis_compression_mad_path():
    from data_agent.tools.charts import _severe_axis_compression

    # spread==0 → MAD 路径；MAD=0 → None（296-300）
    assert _severe_axis_compression([1.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 100.0]) is None


def test_severe_axis_compression_low_ratio_returns_none():
    from data_agent.tools.charts import _severe_axis_compression

    # 极端点存在但压缩比 < 8 → None（316）
    assert _severe_axis_compression([float(i) for i in range(1, 11)] + [30.0]) is None


def test_apply_outlier_scale_controls_scatter_variants():
    import plotly.graph_objects as go

    from data_agent.tools.charts import _apply_outlier_scale_controls

    # x 有极端值 → guard 非空，trace 循环执行；空 trace → continue（372）
    fig = go.Figure()
    fig.add_scatter(x=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 1000.0], y=[1.0] * 9)
    fig.add_scatter()  # 空 trace
    result = _apply_outlier_scale_controls(fig, "scatter", "robust")
    assert result["scale_mode"] == "robust"

    # x 有极端值、y 无 → y guard 为 None → zeros 分支（385-386）
    fig2 = go.Figure()
    fig2.add_scatter(x=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 1000.0], y=[1.0] * 9)
    result2 = _apply_outlier_scale_controls(fig2, "scatter", "robust")
    assert result2["scale_mode"] == "robust"

    # x 与 y 都有极端值 → y guard 非空 → 向量化 mask 分支（383-384）
    fig2b = go.Figure()
    fig2b.add_scatter(
        x=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 1000.0],
        y=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 2000.0],
    )
    result2b = _apply_outlier_scale_controls(fig2b, "scatter", "robust")
    assert result2b["scale_mode"] == "robust"

    # 仅 y 有极端值 → x guard 为 None → x zeros 分支（381-382）
    fig2c = go.Figure()
    fig2c.add_scatter(x=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], y=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 2000.0])
    result2c = _apply_outlier_scale_controls(fig2c, "scatter", "robust")
    assert result2c["scale_mode"] == "robust"

    # 无任何极端值 → 直接返回 full（363-364）
    fig2d = go.Figure()
    fig2d.add_scatter(x=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], y=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    result2d = _apply_outlier_scale_controls(fig2d, "scatter", "robust")
    assert result2d["scale_mode"] == "full"

    # 极端点对应 x 为非数值（字符串）→ 详情走 str 分支（397-398）
    fig2e = go.Figure()
    fig2e.add_scatter(x=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, "txt"], y=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 2000.0])
    result2e = _apply_outlier_scale_controls(fig2e, "scatter", "robust")
    assert result2e["scale_mode"] == "robust"

    # 极端点 > 3 → callout 带"另有 N 个"（408）
    fig3 = go.Figure()
    fig3.add_scatter(
        x=[float(i) for i in range(1, 51)] + [1000.0, 2000.0, 3000.0, 4000.0],
        y=[1.0] * 54,
    )
    result3 = _apply_outlier_scale_controls(fig3, "scatter", "robust")
    assert result3["extreme_points"] >= 4


def test_validate_chart_semantics_heatmap_y_high_cardinality():
    from data_agent.tools.charts import _validate_chart_semantics

    df = pd.DataFrame(
        {
            "类目": [f"c{i % 30}" for i in range(120)],
            "分组": [f"g{i % 80}" for i in range(120)],
            "数值": list(range(120)),
        }
    )
    # x 低基数、y 高基数（80 > 60）→ 626
    with pytest.raises(ValueError, match="类别过多"):
        _validate_chart_semantics(df, chart_type="heatmap", x="类目", y="分组", values="数值")


def test_looks_like_datetime_series_empty_string_column():
    from data_agent.tools.charts import _looks_like_datetime_series

    # string dtype 全 NaN → sample 空 → False（663）
    series = pd.Series([None, None], dtype="string")
    assert _looks_like_datetime_series(series) is False


def test_infer_chart_type_single_numeric_no_xy():
    from data_agent.tools.charts import _infer_chart_type

    # 1-2 个数值列且无 x/y → histogram（731）
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    result = _infer_chart_type(
        df, x=None, y=None, color=None, z=None, size=None, values=None,
        path_columns=None, dimensions=None, aggregation="none", top_n=None,
    )
    assert result == "histogram"
    # 分类 x + 分类 y → bar（766 分类计数）
    df2 = pd.DataFrame({"cat": ["a", "b"], "kind": ["x", "y"]})
    result2 = _infer_chart_type(
        df2, x="cat", y="kind", color=None, z=None, size=None, values=None,
        path_columns=None, dimensions=None, aggregation="none", top_n=None,
    )
    assert result2 == "bar"


def test_apply_plotly_nice_ticks_empty_x_values():
    import plotly.express as px

    from data_agent.tools.builder import _apply_plotly_nice_ticks

    # x 值清空 → 220 分支 return（不报错）
    fig = px.scatter(x=[1, 2], y=[3, 4])
    fig.data[0].x = None
    fig.data[0].y = [3.0, 4.0]
    _apply_plotly_nice_ticks(fig, "scatter", "x", "y", {})


def test_collect_trace_values_non_numeric_values():
    import plotly.graph_objects as go

    from data_agent.tools.builder import _collect_trace_values

    # 非数值元素转换失败 → continue（269-270）
    fig = go.Figure()
    fig.add_scatter(x=[1, 2], y=[3, 4])
    fig.data[0].y = ["bad", 4]
    values = _collect_trace_values(fig, "y")
    assert values == [4.0]


class TestChartPalette:
    """图表分类色板：Tableau 10 官方色板，双引擎共享同一实例。"""

    def test_palette_is_tableau10_without_duplicates(self):
        from data_agent.tools.builder import _CHART_COLORS

        assert len(_CHART_COLORS) == 10
        assert len(set(_CHART_COLORS)) == 10, "色板不得包含重复色"
        # Tableau 10 前两色（蓝/橙）锚定，防止误改
        assert _CHART_COLORS[0] == "#4E79A7"
        assert _CHART_COLORS[1] == "#F28E2B"
        # 全部为合法的 6 位十六进制
        for color in _CHART_COLORS:
            assert len(color) == 7 and color.startswith("#")

    def test_palette_shared_with_echarts_engine(self):
        from data_agent.echarts_engine import _ECHARTS_PALETTE
        from data_agent.tools.builder import _CHART_COLORS

        # 双引擎必须使用同一份色板，否则同数据两引擎配色分裂
        assert _ECHARTS_PALETTE is _CHART_COLORS

    def test_generated_chart_html_embeds_new_palette(self, tmp_path):
        """create_visualization 生成的 HTML 应携带 Tableau 10 colorway。"""
        from data_agent.tools import build_tools
        from data_agent.workspace import DataWorkspace

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        workspace = DataWorkspace(runs_dir, session_id="api_palette")
        workspace.dataframe = pd.DataFrame({
            "category": ["A", "B", "C"],
            "sales": [100, 200, 300],
        })
        tools = build_tools(workspace)
        vis = next(t for t in tools if t.name == "create_visualization")
        vis.invoke({"chart_type": "bar", "x": "category", "y": "sales", "aggregation": "sum"})
        html = (workspace.artifacts_dir / "柱状图_1.html").read_text(encoding="utf-8")
        assert "#4E79A7" in html
        assert '"colorway"' in html
