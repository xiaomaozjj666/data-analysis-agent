from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.io as pio

from data_agent.tools import build_tools
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
    bar = pio.read_json(bar_result["plotly_json"])
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
    scatter = pio.read_json(scatter_result["plotly_json"])
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
    rating = pio.read_json(rating_result["plotly_json"])
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
    channel = pio.read_json(channel_result["plotly_json"])
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
