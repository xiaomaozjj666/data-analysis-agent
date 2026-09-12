"""跨源合并（join_datasets）测试：护栏、诊断与工作区状态一致性。

合并的失败模式大多不抛异常（键不唯一放大行数、键类型不匹配全军覆没、空值键
静默丢失），所以这里的重点不是"能连上"，而是"错误配置会不会被拦住、结果会
不会带够可解释的诊断信息"。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from data_agent.tools import build_tools
from data_agent.tools._join import (
    MAX_ROW_MULTIPLIER,
    list_available_sources,
    merge_datasets,
    normalize_keys,
    resolve_source,
    validate_keys,
)
from data_agent.workspace import DataWorkspace


def _tools(workspace) -> dict:
    return {tool.name: tool for tool in build_tools(workspace)}


def _add_upload(workspace, name: str, frame: pd.DataFrame) -> None:
    workspace.save_upload(name, frame.to_csv(index=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# 来源解析：只允许工作区内的相对路径
# ---------------------------------------------------------------------------


def test_resolve_source_accepts_bare_name_and_relative_path(workspace):
    """裸文件名解析到上传目录；input/ 前缀的相对路径同样可用。"""
    subs = (workspace.input_dir, workspace.artifacts_dir)
    by_bare = resolve_source(workspace.root, subs, "dirty.csv")
    by_relative = resolve_source(workspace.root, subs, "input/dirty.csv")
    assert by_bare == by_relative
    assert by_bare.is_file()


def test_resolve_source_rejects_absolute_path(workspace, tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="绝对路径"):
        resolve_source(workspace.root, (workspace.input_dir,), str(outside))


def test_resolve_source_rejects_parent_traversal(workspace):
    with pytest.raises(ValueError, match="穿越"):
        resolve_source(workspace.root, (workspace.input_dir,), "../outside.csv")
    with pytest.raises(ValueError, match="穿越"):
        resolve_source(workspace.root, (workspace.input_dir,), "input/../../outside.csv")


def test_resolve_source_lists_available_files_when_missing(workspace):
    """找不到文件时必须把可用文件列出来，模型才能自我纠正。"""
    with pytest.raises(ValueError) as exc:
        resolve_source(workspace.root, (workspace.input_dir, workspace.artifacts_dir), "nope.csv")
    assert "input/dirty.csv" in str(exc.value)


def test_list_available_sources_is_relative_to_workspace_root(workspace):
    listing = list_available_sources(workspace.root, (workspace.input_dir, workspace.artifacts_dir))
    assert "input/dirty.csv" in listing
    assert all(not entry.startswith("/") for entry in listing)


# ---------------------------------------------------------------------------
# 键规整与校验
# ---------------------------------------------------------------------------


def test_normalize_keys_defaults_right_to_left():
    assert normalize_keys(["区域"], None) == (["区域"], ["区域"])
    assert normalize_keys("区域", "region") == (["区域"], ["region"])


def test_normalize_keys_rejects_empty_and_mismatched_length():
    with pytest.raises(ValueError, match="left_on 不能为空"):
        normalize_keys([], None)
    with pytest.raises(ValueError, match="连接键数量不一致"):
        normalize_keys(["a", "b"], ["c"])


def test_normalize_keys_rejects_duplicate_left_keys():
    with pytest.raises(ValueError, match="重复列"):
        normalize_keys(["a", "a"], None)


def test_validate_keys_names_the_missing_column_and_available_columns():
    left = pd.DataFrame({"区域": ["华东"], "销售额": [1]})
    right = pd.DataFrame({"region": ["华东"], "经理": ["张三"]})
    with pytest.raises(ValueError, match="待合并文件（右表）中不存在连接键") as right_exc:
        validate_keys(left, right, ["区域"], ["不存在"])
    assert "region" in str(right_exc.value)
    with pytest.raises(ValueError, match="主数据（左表）中不存在连接键") as left_exc:
        validate_keys(left, right, ["不存在"], ["region"])
    assert "区域" in str(left_exc.value)


# ---------------------------------------------------------------------------
# 合并护栏
# ---------------------------------------------------------------------------


def test_merge_inner_returns_matched_rows_only():
    left = pd.DataFrame({"k": ["a", "b", "c"], "v": [1, 2, 3]})
    right = pd.DataFrame({"k": ["a", "c"], "w": [10, 30]})
    merged, diag = merge_datasets(left, right, ["k"], ["k"], "inner")
    assert len(merged) == 2
    assert set(merged["k"]) == {"a", "c"}
    assert diag["matched_keys"] == 2
    assert diag["left_unmatched_keys"] == 1
    assert diag["right_unmatched_keys"] == 0


def test_merge_left_keeps_unmatched_left_rows():
    left = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    right = pd.DataFrame({"k": ["a"], "w": [10]})
    merged, _ = merge_datasets(left, right, ["k"], ["k"], "left")
    assert len(merged) == 2
    assert merged.loc[merged["k"] == "b", "w"].isna().all()


def test_merge_refuses_empty_result():
    """同类型但取值体系不同导致的零匹配必须报错，而不是交付一张空表。"""
    left = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    right = pd.DataFrame({"k": ["x", "y"], "w": [10, 20]})
    with pytest.raises(ValueError, match="合并结果为空"):
        merge_datasets(left, right, ["k"], ["k"], "inner")


def test_merge_translates_dtype_mismatch_into_actionable_error():
    """pandas 对数值/文本键会抛英文错误并建议 concat，需替换为可执行的中文引导。"""
    left = pd.DataFrame({"k": [1, 2], "v": [1, 2]})
    right = pd.DataFrame({"k": ["1", "2"], "w": [10, 20]})
    with pytest.raises(ValueError) as exc:
        merge_datasets(left, right, ["k"], ["k"], "inner")
    message = str(exc.value)
    assert "类型不一致" in message
    assert "repair_data_format" in message


def test_merge_refuses_row_explosion():
    """两侧键都不唯一时的笛卡尔放大会被拒绝，并给出聚合建议。"""
    left = pd.DataFrame({"k": [1] * 10, "v": range(10)})
    right = pd.DataFrame({"k": [1] * 10, "w": range(10)})
    with pytest.raises(ValueError, match="膨胀") as exc:
        merge_datasets(left, right, ["k"], ["k"], "inner")
    assert "聚合去重" in str(exc.value)


def test_merge_allows_growth_within_multiplier():
    """适度的一对多放大不拒绝，但必须留下放大警告。"""
    left = pd.DataFrame({"k": [1, 2], "v": [10, 20]})
    right = pd.DataFrame({"k": [1, 1, 2], "w": [1, 2, 3]})
    merged, diag = merge_datasets(left, right, ["k"], ["k"], "left")
    assert len(merged) == 3
    assert len(merged) <= max(len(left), len(right)) * MAX_ROW_MULTIPLIER
    assert any("多于主数据行数" in item for item in diag["warnings"])


def test_merge_rejects_unknown_join_how():
    left = pd.DataFrame({"k": ["a"]})
    right = pd.DataFrame({"k": ["a"]})
    with pytest.raises(ValueError, match="不支持的连接方式"):
        merge_datasets(left, right, ["k"], ["k"], "cross")


def test_right_join_warning_states_rows_are_dropped_not_null_filled():
    """right join 下主数据未匹配的行是被丢弃的，文案不能写成"新增列为空值"。"""
    left = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    right = pd.DataFrame({"k": ["a", "c"], "w": [10, 30]})
    _, diag = merge_datasets(left, right, ["k"], ["k"], "right")
    text = " ".join(diag["warnings"])
    assert "不会出现在结果中" in text
    assert "新增列为空值" not in text


def test_list_available_sources_skips_non_data_files(workspace):
    """artifacts 目录混放 bundle 与图表 HTML，不能列进"可用数据文件"。"""
    (workspace.artifacts_dir / "plotly.min.js").write_text("//", encoding="utf-8")
    (workspace.artifacts_dir / "chart_1.html").write_text("<html></html>", encoding="utf-8")
    listing = list_available_sources(workspace.root, (workspace.input_dir, workspace.artifacts_dir))
    assert not any(entry.endswith((".js", ".html")) for entry in listing)
    assert "input/dirty.csv" in listing


def test_merge_reports_overlapping_columns_with_suffixes():
    left = pd.DataFrame({"k": ["a"], "数值": [1]})
    right = pd.DataFrame({"k": ["a"], "数值": [2]})
    merged, diag = merge_datasets(left, right, ["k"], ["k"], "inner")
    assert {"数值_x", "数值_y"} <= set(merged.columns)
    assert set(diag["renamed_columns"]) == {"数值_x", "数值_y"}


def test_merge_handles_source_column_named_like_the_indicator():
    """源表自带 _merge 列时不得因 indicator 重名而崩溃。"""
    left = pd.DataFrame({"k": ["a"], "_merge": ["x"], "v": [1]})
    right = pd.DataFrame({"k": ["a"], "w": [2]})
    merged, diag = merge_datasets(left, right, ["k"], ["k"], "inner")
    assert "_merge" in merged.columns
    assert merged["_merge"].tolist() == ["x"]
    assert diag["matched_keys"] == 1


def test_merge_warns_on_null_keys():
    left = pd.DataFrame({"k": ["a", None], "v": [1, 2]})
    right = pd.DataFrame({"k": [None], "w": [9]})
    merged, diag = merge_datasets(left, right, ["k"], ["k"], "left")
    assert diag["left_null_key_rows"] == 1
    assert diag["right_null_key_rows"] == 1
    assert any("连接键为空" in item for item in diag["warnings"])


def test_merge_supports_multi_column_keys():
    left = pd.DataFrame({"省": ["鲁", "苏"], "市": ["济南", "南京"], "v": [1, 2]})
    right = pd.DataFrame({"省": ["鲁", "苏"], "市": ["济南", "苏州"], "w": [9, 8]})
    merged, _ = merge_datasets(left, right, ["省", "市"], ["省", "市"], "inner")
    assert merged.to_dict(orient="records") == [{"省": "鲁", "市": "济南", "v": 1, "w": 9}]


# ---------------------------------------------------------------------------
# 工具层：工作区状态、产物与基线
# ---------------------------------------------------------------------------


def test_join_tool_lists_sources_when_called_without_a_name(workspace):
    _add_upload(workspace, "region.csv", pd.DataFrame({"region": ["East"], "manager": ["A"]}))
    payload = json.loads(_tools(workspace)["join_datasets"].invoke({"right_source": ""}))
    assert payload["status"] == "needs_input"
    assert "input/region.csv" in payload["available_sources"]


def test_join_tool_requires_left_on(workspace):
    _add_upload(workspace, "region.csv", pd.DataFrame({"region": ["East"], "manager": ["A"]}))
    with pytest.raises(ValueError, match="left_on 不能为空"):
        _tools(workspace)["join_datasets"].invoke({"right_source": "region.csv"})


def test_join_tool_replaces_active_dataset_and_resets_baseline(workspace):
    before_rows = len(workspace.dataframe)
    mapping = pd.DataFrame({"region": ["East", "West"], "manager": ["张三", "李四"]})
    _add_upload(workspace, "region.csv", mapping)
    payload = json.loads(
        _tools(workspace)["join_datasets"].invoke(
            {"right_source": "region.csv", "left_on": ["region"], "how": "left"}
        )
    )
    assert payload["status"] == "ok"
    assert "manager" in workspace.dataframe.columns
    assert len(workspace.dataframe) == before_rows
    # 基线重置为合并结果行数，后续清洗才有正确参照
    assert workspace.source_row_count == len(workspace.dataframe)


def test_join_tool_registers_dataset_artifact(workspace):
    _add_upload(workspace, "region.csv", pd.DataFrame({"region": ["East", "West"], "manager": ["A", "B"]}))
    payload = json.loads(
        _tools(workspace)["join_datasets"].invoke({"right_source": "region.csv", "left_on": ["region"]})
    )
    assert payload["output"].endswith("joined_data.csv")
    assert any(item["name"] == "joined_data.csv" for item in workspace.artifacts)


def test_join_tool_leaves_no_state_change_when_guard_rejects(workspace):
    """被护栏拒绝的合并不允许留下任何状态变更。"""
    before = workspace.dataframe.copy()
    before_artifacts = len(workspace.artifacts)
    _add_upload(workspace, "bad.csv", pd.DataFrame({"region": [1, 2], "x": [1, 2]}))
    with pytest.raises(ValueError):
        _tools(workspace)["join_datasets"].invoke(
            {"right_source": "bad.csv", "left_on": ["region"], "how": "inner"}
        )
    pd.testing.assert_frame_equal(workspace.dataframe, before)
    assert len(workspace.artifacts) == before_artifacts


def test_join_tool_rejects_source_outside_workspace(workspace, tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("region,manager\nEast,A\n", encoding="utf-8")
    with pytest.raises(ValueError, match="绝对路径"):
        _tools(workspace)["join_datasets"].invoke(
            {"right_source": str(outside), "left_on": ["region"]}
        )


def test_clean_data_accepts_deduplication_after_a_shrinking_join(tmp_path):
    """收缩型合并后基线必须跟着降，否则连去重都会被 20% 安全下限挡住。

    这是一个真实的使用障碍：主数据 10 行、合并后只剩 1 行时，若基线仍锚定在
    10 行，安全下限为 2 行，clean_data 会拒绝任何操作，合并结果等于无法继续加工。
    """
    ws = DataWorkspace(tmp_path / "runs", session_id="shrink")
    source = tmp_path / "orders.csv"
    pd.DataFrame({"订单号": [f"O{i}" for i in range(10)], "金额": range(10)}).to_csv(source, index=False)
    ws.load(source, copy_into_workspace=True)
    # 仅 1 个订单能在右表匹配 → inner join 后只剩 1 行
    ws.save_upload(
        "one.csv",
        pd.DataFrame({"订单号": ["O3"], "客户": ["张三"]}).to_csv(index=False).encode("utf-8"),
    )
    tools = {tool.name: tool for tool in build_tools(ws)}
    payload = json.loads(
        tools["join_datasets"].invoke(
            {"right_source": "one.csv", "left_on": ["订单号"], "how": "inner"}
        )
    )
    assert payload["result_rows"] == 1
    assert ws.source_row_count == 1
    # 若基线未重置，此处会因 max(1, (10+4)//5)=2 行的下限而拒绝
    result = json.loads(tools["clean_data"].invoke({"drop_duplicates": True}))
    assert result["status"] == "ok"


def test_snapshot_restore_rolls_back_the_row_baseline(workspace):
    """合并步骤回滚后，行数基线必须一起回到合并前的规模。"""
    snapshot = workspace.snapshot_state()
    baseline_before = snapshot.source_row_count
    mapping = pd.DataFrame({"region": ["East"], "manager": ["张三"]})
    _add_upload(workspace, "east.csv", mapping)
    _tools(workspace)["join_datasets"].invoke(
        {"right_source": "east.csv", "left_on": ["region"], "how": "inner"}
    )
    assert workspace.source_row_count != baseline_before
    workspace.restore_state(snapshot)
    assert workspace.source_row_count == baseline_before
