"""跨源合并与候选数据集采纳工具：``join_datasets`` / ``adopt_dataset``。

从 ``builder.build_tools`` 按域拆出的工具定义；键校验、扇出护栏与
"候选数据集不自动替换主数据"的语义保持原实现不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool, tool

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace

from ._join import (
    list_available_sources,
    merge_datasets,
    normalize_keys,
    resolve_source,
    validate_keys,
)


def build_dataset_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建数据集合并与采纳类工具（join_datasets、adopt_dataset）。"""

    @tool
    def join_datasets(
        right_source: str,
        left_on: list[str] | None = None,
        right_on: list[str] | None = None,
        how: Literal["inner", "left", "right", "outer"] = "inner",
    ) -> str:
        """Merge another data file of this session into the active dataset.

        Use this when the analysis needs columns that live in a second uploaded file
        (for example joining monthly sales with a region mapping table, or stacking
        quarterly sheets into one annual table) instead of writing ad-hoc pandas code.

        Call it with an empty right_source to get the list of data files available in
        this session — do that first when you are unsure of the exact file name.

        right_source is a file name or a path relative to this session; a bare name
        resolves against the uploads directory, and artifacts/<name> also works.
        Absolute paths and ".." traversal are rejected.
        left_on and right_on are the join keys; omit right_on when both sides use the
        same key names. how selects inner / left / right / outer.

        The merged result is saved as a candidate dataset (joined_data.csv) and does
        NOT replace the active dataset. To analyze the merged table, call
        adopt_dataset('joined_data.csv') explicitly — or let the user click
        「设为主数据」in the UI. This keeps silent row-count changes out of the
        main data: the user always sees what happened before anything is adopted.

        Guards (always enforced regardless of parameters):
          - Join keys must exist on both sides; the error lists the available columns.
          - An empty result is refused: it almost always means the key types or key
            value spaces are incompatible, and handing back an empty table is worse
            than failing loudly.
          - The result may not exceed 5x the larger input table. Exceeding that means
            the keys are not unique and the join is fanning out, which silently
            inflates every aggregate downstream.
        """
        if not (right_source or "").strip():
            return json_text(
                {
                    "status": "needs_input",
                    "available_sources": list_available_sources(
                        workspace.root, (workspace.input_dir, workspace.artifacts_dir)
                    ),
                    "message": "请从 available_sources 中选择要合并的文件名，再次调用本工具。",
                }
            )
        if not left_on:
            raise ValueError("left_on 不能为空，请指定主数据的连接键列。")
        source_path = resolve_source(
            workspace.root, (workspace.input_dir, workspace.artifacts_dir), right_source
        )
        left_frame = workspace.dataframe
        right_frame = workspace.read_source(source_path)
        left_keys, right_keys = normalize_keys(left_on, right_on)
        validate_keys(left_frame, right_frame, left_keys, right_keys)
        merged, diagnostics = merge_datasets(left_frame, right_frame, left_keys, right_keys, how)
        merged = merged.reset_index(drop=True)
        # 显式传 frame：主数据未接管，缺省会把活动数据集（旧主数据）写成候选。
        output = workspace.save_dataframe(
            "joined_data.csv", dataframe=merged, description=f"跨源合并候选数据集（{source_path.name}，未采纳）"
        )
        # 候选登记：产物中心可见、可一键采纳；主数据保持不变。
        workspace.register_candidate_dataset(
            "joined_data.csv",
            rows=len(merged),
            note=f"由 {source_path.name} 以 {how} 合并得到",
        )
        warnings: list[str] = list(diagnostics.get("warnings", [])) if isinstance(
            diagnostics, dict
        ) else []
        if len(merged) < len(left_frame) / 2:
            warnings.append(
                f"合并结果（{len(merged)} 行）比主数据（{len(left_frame)} 行）少了一半以上："
                "多为 inner join 键不匹配所致。采纳前请先确认这是预期行为。"
            )
        diagnostics_clean = (
            {key: value for key, value in diagnostics.items() if key != "warnings"}
            if isinstance(diagnostics, dict)
            else {}
        )
        return json_text(
            {
                "status": "ok",
                "adopted": False,
                "source": source_path.name,
                "how": how,
                "left_on": left_keys,
                "right_on": right_keys,
                "candidate": {"name": "joined_data.csv", "rows": len(merged)},
                "active_rows": len(workspace.dataframe),
                **diagnostics_clean,
                "warnings": warnings,
                "output": output,
                "note": (
                    "合并结果已保存为候选数据集 joined_data.csv，主数据未改变。"
                    "若要基于合并结果继续分析，请调用 adopt_dataset 工具显式采纳，"
                    "或由用户在产物中心点击「设为主数据」。"
                ),
            }
        )

    @tool
    def adopt_dataset(name: str) -> str:
        """Adopt a candidate dataset as the active dataset (explicit, user-visible).

        Candidates are produced by join_datasets (and exports). Adoption REPLACES
        the active dataset and resets the row-count baseline for cleaning guards,
        so call it only after telling the user which table becomes active and how
        the row count changes. Call with an empty name to list pending candidates.
        """
        candidates = workspace.candidate_datasets
        if not (name or "").strip():
            if not candidates:
                return json_text({"status": "needs_input", "candidates": [], "message": "当前没有待采纳的候选数据集。"})
            return json_text(
                {
                    "status": "needs_input",
                    "candidates": candidates,
                    "message": "请从 candidates 中选择要采纳的文件名，再次调用本工具。",
                }
            )
        target = Path(name).name
        if target not in {item["name"] for item in candidates}:
            raise ValueError(
                f"{target} 不在候选数据集中。可用候选：{[item['name'] for item in candidates] or '（无）'}。"
                "若该文件已在产物中，也可让用户在产物中心手动采纳。"
            )
        result = workspace.adopt_candidate(target)
        return json_text(
            {
                "status": "ok",
                **result,
                "note": (
                    f"已采纳 {result['name']} 为活动数据集：{result['before_rows']} 行 → "
                    f"{result['after_rows']} 行，行数基线已重置。请把这件事明确告知用户。"
                ),
            }
        )

    return [join_datasets, adopt_dataset]
