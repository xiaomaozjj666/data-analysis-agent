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
