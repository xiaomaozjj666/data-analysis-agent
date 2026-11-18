"""技能包导出：SKILL.md + analysis_recipe.json + skill.json。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from data_agent.skill import (
    MANIFEST_FILE,
    RECIPE_FILENAME,
    SKILL_DOC,
    build_skill_bundle,
    export_skill,
    install_skill,
    list_skills,
    main,
    resolve_skills_root,
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


def _installed_names(root):
    payload = list_skills(root=root)
    return [(item["slug"], item["valid"]) for item in payload["skills"]]


def test_install_skill_copies_contract_and_lists_it(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    target_root = tmp_path / "installed"

    result = install_skill(bundle, root=target_root)

    assert result["slug"] == exported["slug"]
    assert result["replaced"] is False
    assert result["scope"] == "project"
    target = target_root / exported["slug"]
    for filename in (SKILL_DOC, RECIPE_FILENAME, MANIFEST_FILE):
        assert (target / filename).is_file()
    listing = list_skills(root=target_root)
    assert listing["root"] == str(target_root.resolve())
    assert _installed_names(target_root) == [(exported["slug"], True)]
    assert listing["skills"][0]["recipe_sha256"] == exported["recipe_sha256"]


def test_install_refuses_overwrite_without_force(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    target_root = tmp_path / "installed"
    install_skill(bundle, root=target_root)

    with pytest.raises(FileExistsError, match="force"):
        install_skill(bundle, root=target_root)
    replaced = install_skill(bundle, root=target_root, force=True)
    assert replaced["replaced"] is True
    assert _installed_names(target_root) == [(exported["slug"], True)]


def test_install_rejects_tampered_recipe(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    recipe_path = bundle / RECIPE_FILENAME
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe["task"] = "被篡改的任务"
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="哈希不一致"):
        install_skill(bundle, root=tmp_path / "installed")


def test_install_rejects_unsafe_slug(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    manifest_path = bundle / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["slug"] = "../escape"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="slug"):
        install_skill(bundle, root=tmp_path / "installed")
    assert not (tmp_path / "installed" / ".." / "escape").exists()


def test_install_rejects_symlinked_contract_file(tmp_path):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    original = bundle / RECIPE_FILENAME
    outside = tmp_path / "outside.json"
    outside.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    original.unlink()
    try:
        original.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建符号链接")
    with pytest.raises(ValueError, match="符号链接"):
        install_skill(bundle, root=tmp_path / "installed")


def test_list_marks_corrupt_or_missing_skill_invalid(tmp_path):
    root = tmp_path / "installed"
    good = root / "good"
    good.mkdir(parents=True)
    bundle = build_skill_bundle({"task": "销售分析", "steps": []})
    for filename, text in bundle.items():
        (good / filename).write_text(text, encoding="utf-8")
    broken = root / "broken"
    broken.mkdir()
    (broken / MANIFEST_FILE).write_text("{}", encoding="utf-8")

    payload = list_skills(root=root)
    by_slug = {item["slug"]: item for item in payload["skills"]}
    assert by_slug["good"]["valid"] is True
    assert by_slug["broken"]["valid"] is False
    assert "缺少" in by_slug["broken"]["error"]


def test_cli_install_and_list(tmp_path, capsys):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    root = tmp_path / "cli-skills"

    assert main(["install", "--bundle", str(bundle), "--root", str(root)]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["slug"] == exported["slug"]

    assert main(["list", "--root", str(root)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["skills"][0]["valid"] is True


def test_skills_root_scope_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SKILLS_DIR", str(tmp_path / "env-skills"))
    assert resolve_skills_root("project") == (tmp_path / "env-skills").resolve()
    monkeypatch.delenv("DATA_AGENT_SKILLS_DIR")
    explicit = tmp_path / "explicit"
    assert resolve_skills_root("global", root=explicit) == explicit.resolve()
    with pytest.raises(ValueError, match="scope"):
        resolve_skills_root("elsewhere")


def test_resolve_skills_root_default_project_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DATA_AGENT_SKILLS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_skills_root("project") == (tmp_path / ".data-agent" / "skills").resolve()
    assert resolve_skills_root("global").is_absolute()


def test_load_bundle_contract_rejects_bad_inputs(tmp_path):
    from data_agent.skill import _load_bundle_contract

    with pytest.raises(FileNotFoundError, match="不存在"):
        _load_bundle_contract(tmp_path / "missing")

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / SKILL_DOC).write_text("# x", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="缺少"):
        _load_bundle_contract(partial)

    bad_json = tmp_path / "bad-json"
    bad_json.mkdir()
    for name in (SKILL_DOC, MANIFEST_FILE):
        (bad_json / name).write_text("{}", encoding="utf-8")
    (bad_json / RECIPE_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="无法解析"):
        _load_bundle_contract(bad_json)

    non_dict = tmp_path / "non-dict"
    non_dict.mkdir()
    (non_dict / SKILL_DOC).write_text("# x", encoding="utf-8")
    (non_dict / MANIFEST_FILE).write_text("{}", encoding="utf-8")
    (non_dict / RECIPE_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        _load_bundle_contract(non_dict)

    (non_dict / MANIFEST_FILE).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        _load_bundle_contract(non_dict)

    (non_dict / MANIFEST_FILE).write_text('{"slug": "safe"}', encoding="utf-8")
    (non_dict / RECIPE_FILENAME).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="recipe_sha256"):
        _load_bundle_contract(non_dict)


def test_install_rolls_back_when_replace_fails(tmp_path, monkeypatch):
    workspace = _workspace_with_recipe(tmp_path)
    exported = export_skill(workspace.root, tmp_path / "out")
    bundle = tmp_path / "out" / exported["slug"]
    target_root = tmp_path / "installed"
    install_skill(bundle, root=target_root)
    target = target_root / exported["slug"]
    before = (target / MANIFEST_FILE).read_text(encoding="utf-8")

    import data_agent.skill as skill_module

    real_replace = os.replace

    def flaky(src, dst):
        if Path(src).name.startswith(".install-") and Path(dst) == target:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(skill_module.os, "replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        install_skill(bundle, root=target_root, force=True)
    assert (target / MANIFEST_FILE).read_text(encoding="utf-8") == before
    assert not [item for item in target_root.iterdir() if item.name.startswith(".")]


def test_list_skips_hidden_symlink_and_non_directory(tmp_path):
    import data_agent.skill as skill_module

    root = tmp_path / "installed"
    root.mkdir()
    (root / ".hidden").mkdir()
    (root / "plain.txt").write_text("x", encoding="utf-8")
    try:
        (root / "link").symlink_to(tmp_path / "elsewhere")
    except OSError:
        # Windows 未开启开发者模式时创建符号链接需要额外特权；隐藏目录与
        # 普通文件已经覆盖同一条跳过分支。
        pass
    assert skill_module.list_skills(root=root)["skills"] == []


def test_list_reports_bad_manifest_variants(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    base = build_skill_bundle({"task": "t", "steps": []})

    def write(slug, *, recipe_text=None, manifest_text=None):
        target = root / slug
        target.mkdir()
        (target / SKILL_DOC).write_text(base[SKILL_DOC], encoding="utf-8")
        (target / RECIPE_FILENAME).write_text(
            recipe_text if recipe_text is not None else base[RECIPE_FILENAME],
            encoding="utf-8",
        )
        (target / MANIFEST_FILE).write_text(
            manifest_text if manifest_text is not None else base[MANIFEST_FILE],
            encoding="utf-8",
        )

    write("non-dict", recipe_text="[]")
    write("no-hash", manifest_text='{"slug": "no-hash"}')
    write("mismatch", manifest_text=json.dumps({"slug": "mismatch", "recipe_sha256": "x"}))
    by_slug = {item["slug"]: item for item in list_skills(root=root)["skills"]}
    assert by_slug["non-dict"]["valid"] is False
    assert "JSON 对象" in by_slug["non-dict"]["error"]
    assert "缺少 recipe_sha256" in by_slug["no-hash"]["error"]
    assert "不一致" in by_slug["mismatch"]["error"]
