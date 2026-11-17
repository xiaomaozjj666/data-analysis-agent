"""技能包导出：SKILL.md + analysis_recipe.json + skill.json。"""

from __future__ import annotations

import json

import pytest

from data_agent.skill import (
    MANIFEST_FILE,
    RECIPE_FILENAME,
    SKILL_DOC,
    build_skill_bundle,
    export_skill,
)
from data_agent.workspace import DataWorkspace


def _workspace_with_recipe(tmp_path) -> DataWorkspace:
    workspace = DataWorkspace(tmp_path / "runs", session_id="skill")
    source = workspace.save_upload("sales.csv", b"region,sales\nEast,100\nWest,200\n")
    workspace.load(source)
    recipe = {
        "version": 1,
        "task": "分析各区域销售趋势",
        "data_fingerprint": {
            "rows": 2,
            "columns": ["region", "sales"],
            "dtypes": {"region": "object", "sales": "int64"},
            "sample_sha256_16": "deadbeef",
        },
        "plan": [{"id": "s1", "title": "检查数据"}],
        "steps": [
            {
                "id": "s1",
                "title": "检查数据",
                "status": "done",
                "tool_calls": [{"name": "inspect_data", "args": {"sample_rows": 5}}],
            }
        ],
    }
    (workspace.artifacts_dir / RECIPE_FILENAME).write_text(
        json.dumps(recipe, ensure_ascii=False), encoding="utf-8"
    )
    return workspace


def test_build_bundle_contains_manifest_and_tool_steps():
    bundle = build_skill_bundle(
        {
            "version": 1,
            "task": "分析销售",
            "data_fingerprint": {"rows": 2, "columns": ["sales"], "sample_sha256_16": "x"},
            "steps": [
                {
                    "id": "s1",
                    "title": "检查",
                    "tool_calls": [{"name": "inspect_data", "args": {"sample_rows": 5}}],
                }
            ],
        },
        created_at="2026-11-17T00:00:00+00:00",
    )
    assert set(bundle) == {SKILL_DOC, RECIPE_FILENAME, MANIFEST_FILE}
    assert "分析销售" in bundle[SKILL_DOC]
    assert "inspect_data" in bundle[SKILL_DOC]
    assert "sample_rows" in bundle[SKILL_DOC]
    assert "不是模型推理过程" in bundle[SKILL_DOC]
    manifest = json.loads(bundle[MANIFEST_FILE])
    assert manifest["tools"] == ["inspect_data"]
    assert manifest["version"] == 1
    assert len(manifest["recipe_sha256"]) == 64
    assert manifest["created_at"] == "2026-11-17T00:00:00+00:00"


def test_export_writes_three_files(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    result = export_skill(workspace.root, tmp_path / "out")
    target = tmp_path / "out" / result["slug"]
    assert target.is_dir()
    assert {path.name for path in target.iterdir()} == {
        SKILL_DOC,
        RECIPE_FILENAME,
        MANIFEST_FILE,
    }
    manifest = json.loads((target / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["task"] == "分析各区域销售趋势"
    assert "分析各区域销售趋势" in (target / SKILL_DOC).read_text(encoding="utf-8")


def test_export_refuses_overwrite_without_force(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    export_skill(workspace.root, tmp_path / "out")
    with pytest.raises(FileExistsError, match="技能目录已存在"):
        export_skill(workspace.root, tmp_path / "out")


def test_export_force_overwrites(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    first = export_skill(workspace.root, tmp_path / "out", name="旧名字")
    target = tmp_path / "out" / first["slug"]
    (target / "stale.txt").write_text("old", encoding="utf-8")
    result = export_skill(workspace.root, tmp_path / "out", name="旧名字", force=True)
    assert result["slug"] == first["slug"]
    assert not (target / "stale.txt").exists()


def test_export_missing_recipe_raises(tmp_path):
    workspace = DataWorkspace(tmp_path / "runs", session_id="empty")
    with pytest.raises(FileNotFoundError, match=RECIPE_FILENAME):
        export_skill(workspace.root, tmp_path / "out")


def test_slug_is_path_safe_and_keeps_chinese(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    result = export_skill(workspace.root, tmp_path / "out", name="../../分析/销售 趋势?")
    assert "/" not in result["slug"] and ".." not in result["slug"]
    assert "分析" in result["slug"]


def test_cli_export_prints_summary(tmp_path, capsys):
    from data_agent.skill import main

    workspace = _workspace_with_recipe(tmp_path)
    code = main(["export", "--session", str(workspace.root), "--out", str(tmp_path / "out")])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] == [SKILL_DOC, RECIPE_FILENAME, MANIFEST_FILE]
    assert payload["tools"] == ["inspect_data"]


def test_cli_reports_missing_recipe_without_traceback(tmp_path, capsys):
    from data_agent.skill import main

    workspace = DataWorkspace(tmp_path / "runs", session_id="empty")
    code = main(["export", "--session", str(workspace.root), "--out", str(tmp_path / "out")])
    assert code == 2
    assert "analysis_recipe.json" in capsys.readouterr().err


def test_run_bundle_replays_recipe_without_llm(tmp_path):
    """技能包重跑走确定性工具调用，不构造分析 Agent。"""
    from data_agent.skill import run_bundle

    workspace = _workspace_with_recipe(tmp_path)
    result = export_skill(workspace.root, tmp_path / "out")
    data = workspace.source_path
    replay = run_bundle(tmp_path / "out" / result["slug"], data)
    assert replay["calls"][0]["name"] == "inspect_data"
    assert replay["calls"][0]["status"] == "ok"
    assert replay["fingerprint_now"]["rows"] == 2


def test_run_bundle_rejects_tampered_recipe(tmp_path):
    """skill.json 的配方哈希不一致时拒绝重跑（分发包被改过）。"""
    import json

    from data_agent.skill import RECIPE_FILENAME as RECIPE
    from data_agent.skill import run_bundle

    workspace = _workspace_with_recipe(tmp_path)
    result = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / result["slug"]
    recipe_path = bundle / RECIPE
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe["task"] = "被改过的任务"
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="配方哈希不一致"):
        run_bundle(bundle, workspace.source_path)


def test_export_force_rolls_back_when_replace_fails(tmp_path, monkeypatch):
    """覆盖失败时旧技能包必须还在（不能旧包新包都没了）。"""
    import os as os_module

    workspace = _workspace_with_recipe(tmp_path)
    first = export_skill(workspace.root, tmp_path / "out")
    target = tmp_path / "out" / first["slug"]
    (target / "marker.txt").write_text("old", encoding="utf-8")

    real_replace = os_module.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:  # 第二次是 staging -> target
            raise OSError("target locked")
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", flaky_replace)
    with pytest.raises(OSError, match="locked"):
        export_skill(workspace.root, tmp_path / "out", name=first["name"], force=True)
    assert (target / "marker.txt").read_text(encoding="utf-8") == "old"


def test_build_bundle_without_tool_calls_notes_it():
    from data_agent.skill import build_skill_bundle

    bundle = build_skill_bundle(
        {
            "version": 1,
            "task": "空配方",
            "steps": [{"id": "s1", "title": "无调用", "tool_calls": []}],
        }
    )
    assert "配方中没有可重放的工具调用" in bundle["SKILL.md"]


def test_build_bundle_rejects_non_dict_recipe():
    from data_agent.skill import build_skill_bundle

    with pytest.raises(ValueError, match="包含 task 字段"):
        build_skill_bundle(["not-a-dict"])  # type: ignore[arg-type]


def test_load_session_recipe_rejects_non_object(tmp_path):
    from data_agent.skill import RECIPE_FILENAME, load_session_recipe

    path = tmp_path / "artifacts" / RECIPE_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="不是 JSON 对象"):
        load_session_recipe(tmp_path)


def test_run_bundle_rejects_missing_and_non_object_recipe(tmp_path):
    from data_agent.skill import RECIPE_FILENAME, run_bundle

    with pytest.raises(FileNotFoundError, match=RECIPE_FILENAME):
        run_bundle(tmp_path / "missing", tmp_path / "data.csv")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / RECIPE_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="不是 JSON 对象"):
        run_bundle(bundle, tmp_path / "data.csv")


def test_cli_reports_bundle_error(tmp_path, capsys):
    from data_agent.skill import main

    code = main(["run", "--bundle", str(tmp_path / "missing"), "--data", str(tmp_path / "x.csv")])
    assert code == 2
    assert "analysis_recipe.json" in capsys.readouterr().err
