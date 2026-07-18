from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from data_agent.tools import build_tools
from data_agent.workspace import DataWorkspace


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
    assert html.exists() and html.stat().st_size > 100_000
    assert chart_json.exists()
    assert {item["kind"] for item in workspace.artifacts} == {"visualization", "chart_data"}
