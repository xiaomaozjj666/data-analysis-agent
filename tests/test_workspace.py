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


def test_count_artifacts_filters_by_kind(workspace):
    """count_artifacts replaces private _artifacts access from tools.py.

    The chart-numbering logic in create_visualization relies on counting
    existing visualization artifacts; this test pins the contract so a
    future refactor doesn't silently break the file-name sequence.
    """
    assert workspace.count_artifacts() == 0
    assert workspace.count_artifacts("visualization") == 0
    assert workspace.count_artifacts("dataset") == 0

    workspace.save_dataframe("result.csv")
    assert workspace.count_artifacts() == 1
    assert workspace.count_artifacts("dataset") == 1
    assert workspace.count_artifacts("visualization") == 0


def test_atomic_write_text_replaces_existing_file(tmp_path):
    """_atomic_write_text must leave a complete file even when overwriting.

    A direct write_text truncates then writes; if interrupted we'd see a
    partial file. The tmp+rename pattern guarantees the destination is
    either the old content or the new content, never a mix.
    """
    from data_agent.workspace import _atomic_write_text

    target = tmp_path / "out.html"
    target.write_text("<old>previous</old>", encoding="utf-8")
    _atomic_write_text(target, "<new>replacement</new>")
    assert target.read_text(encoding="utf-8") == "<new>replacement</new>"
    # Tmp file must be cleaned up regardless of success.
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_atomic_write_text_cleans_up_tmp_on_failure(tmp_path):
    """If the rename fails the tmp file must not be left behind."""
    from data_agent.workspace import _atomic_write_text

    target = tmp_path / "subdir" / "missing.html"
    # Parent dir doesn't exist -> write_text raises -> tmp must be cleaned.
    with pytest.raises(OSError):
        _atomic_write_text(target, "content")
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_profile_degrades_deep_memory_for_large_tables(tmp_path, monkeypatch):
    """超过单元格阈值时 memory_usage 降级为 deep=False，但 unique/duplicated 保持全量精确。

    把阈值 monkeypatch 为 1 强制走浅路径：profile 仍需返回 float 型 memory_mb，
    且图表语义防护依赖的 unique 计数、duplicate_rows 不受降级影响。
    """
    import data_agent.workspace as workspace_module

    monkeypatch.setattr(workspace_module, "_PROFILE_DEEP_MEMORY_MAX_CELLS", 1)
    source = tmp_path / "big.csv"
    pd.DataFrame(
        {"name": ["alpha", "beta", "alpha", "alpha"], "value": [1, 2, 2, 1]}
    ).to_csv(source, index=False)
    workspace = DataWorkspace(tmp_path / "runs")
    profile = workspace.load(source)

    assert isinstance(profile["memory_mb"], float)
    assert profile["memory_mb"] >= 0
    name_info = next(c for c in profile["column_info"] if c["name"] == "name")
    assert name_info["unique"] == 2  # 降级只影响内存统计，unique 仍全量精确
    assert profile["duplicate_rows"] == 1  # 第 4 行重复第 1 行，duplicated 仍全量精确


def test_profile_keeps_deep_memory_for_small_tables(workspace):
    """未超阈值的小表保持 deep=True 精确统计（object 列逐对象计量，值更大）。"""
    profile = workspace.profile()
    df = workspace.dataframe
    deep_mb = round(float(df.memory_usage(deep=True).sum() / 1024**2), 3)
    assert profile["memory_mb"] == deep_mb


# ---------------------------------------------------------------------------
# allocate_chart_index：图表序号原子分配
# ---------------------------------------------------------------------------


def test_allocate_chart_index_is_unique_under_concurrency(workspace):
    """回归：ToolNode 并行执行同一轮多个 create_visualization 时，
    旧实现先读 count_artifacts 再拼文件名会竞态出重号，同类型图表
    算出相同文件名后写覆盖先写，导致会话历史里图表丢失。
    新实现加锁分配，并发拿到的序号必须互不相同且连续递增。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        indices = list(pool.map(lambda _: workspace.allocate_chart_index(), range(32)))

    assert len(set(indices)) == 32, f"序号出现重号：{sorted(indices)}"
    assert sorted(indices) == list(range(1, 33))


def test_allocate_chart_index_resumes_after_restart(tmp_path):
    """重启/会话恢复后序号从磁盘已有图表尾号之后延续，
    不会回头覆盖历史文件；非 HTML 文件不干扰分配。"""
    workspace = DataWorkspace(tmp_path / "runs", session_id="chart_seq")
    (workspace.artifacts_dir / "折线图_3.html").write_text("<html></html>", encoding="utf-8")
    (workspace.artifacts_dir / "三维散点_7.html").write_text("<html></html>", encoding="utf-8")
    (workspace.artifacts_dir / "折线图_9.plotly.json").write_text("{}", encoding="utf-8")
    (workspace.artifacts_dir / "cleaned_data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    # 新建实例（模拟重启后恢复）：磁盘 HTML 最大尾号 7 → 下一个序号 8。
    restored = DataWorkspace(tmp_path / "runs", session_id="chart_seq")
    assert restored.allocate_chart_index() == 8
    # 高水位生效：即使序号 8 的图尚未落盘，下一次分配也不重用。
    assert restored.allocate_chart_index() == 9
