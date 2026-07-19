from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from data_agent.tools import build_tools
from data_agent.workspace import PLOTLY_BUNDLE_NAME, DataWorkspace


def tool_map(workspace):
    return {item.name: item for item in build_tools(workspace)}


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
