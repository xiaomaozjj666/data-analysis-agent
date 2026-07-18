from __future__ import annotations

import pandas as pd
import pytest

from data_agent.workspace import DataWorkspace


def test_workspace_loads_csv_and_profiles(workspace):
    profile = workspace.profile(sample_rows=3)
    assert profile["rows"] == 6
    assert profile["columns"] == 4
    assert profile["duplicate_rows"] == 1
    assert next(item for item in profile["column_info"] if item["name"] == "sales")["missing"] == 1


def test_workspace_reads_chinese_encoded_csv(tmp_path):
    path = tmp_path / "gb.csv"
    pd.DataFrame({"地区": ["华东"], "销售额": [100]}).to_csv(path, index=False, encoding="gb18030")
    workspace = DataWorkspace(tmp_path / "runs")
    profile = workspace.load(path)
    assert profile["rows"] == 1
    assert list(workspace.dataframe.columns) == ["地区", "销售额"]


def test_workspace_skips_unparseable_csv_rows_with_warning(tmp_path):
    path = tmp_path / "malformed.csv"
    path.write_text("a,b\n1,2\n3,4,5\n6,7\n", encoding="utf-8")
    workspace = DataWorkspace(tmp_path / "runs")

    profile = workspace.load(path)

    assert profile["rows"] == 2
    assert profile["load_warnings"]
    assert "跳过" in profile["load_warnings"][0]


def test_rejects_unsupported_file(tmp_path):
    path = tmp_path / "unsafe.py"
    path.write_text("print('no')", encoding="utf-8")
    workspace = DataWorkspace(tmp_path / "runs")
    with pytest.raises(ValueError, match="不支持"):
        workspace.load(path)


@pytest.mark.parametrize("suffix", [".xlsx", ".parquet", ".jsonl"])
def test_supported_structured_formats_round_trip(tmp_path, suffix):
    expected = pd.DataFrame({"地区": ["华东", "华南"], "销售额": [100.5, 88.0]})
    path = tmp_path / f"data{suffix}"
    if suffix == ".xlsx":
        expected.to_excel(path, index=False)
    elif suffix == ".parquet":
        expected.to_parquet(path, index=False)
    else:
        expected.to_json(path, orient="records", lines=True, force_ascii=False)
    workspace = DataWorkspace(tmp_path / "runs")
    workspace.load(path)
    assert workspace.dataframe.shape == (2, 2)
    assert workspace.dataframe["地区"].tolist() == ["华东", "华南"]


@pytest.mark.parametrize("suffix", ["csv", "xlsx", "parquet"])
def test_dataframe_export_formats(workspace, suffix):
    path = workspace.save_dataframe(f"result.{suffix}")
    assert path.exists() and path.stat().st_size > 0
    assert workspace.artifacts[-1]["kind"] == "dataset"
