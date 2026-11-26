"""runs/ 保留策略：只删会话目录、尊重保留数与时间阈值、CLI clean 命令可用。"""

from __future__ import annotations

import json
import logging
import os
import time

import pytest
from typer.testing import CliRunner

from data_agent import cli as cli_module
from data_agent import retention
from data_agent.retention import find_session_dirs, format_bytes, prune_runs, purge_trash

DAY = 86400


def _make(runs, name, age_days, now):
    d = runs / name
    (d / "input").mkdir(parents=True)
    f = d / "input" / "data.csv"
    f.write_text("a\n1\n", encoding="utf-8")
    ts = now - age_days * DAY
    for p in (f, d / "input", d):
        os.utime(p, (ts, ts))
    return d


def test_only_session_dirs_are_considered(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    _make(runs, "cli_0123456789ab", 30, now)
    _make(runs, "_bundles", 30, now)
    _make(runs, "cli_not-a-session", 30, now)
    (runs / "memory.json").write_text("[]", encoding="utf-8")
    names = {c.path.name for c in find_session_dirs(runs)}
    assert names == {"cli_0123456789ab"}


def test_prune_respects_age_keep_latest_and_prefix(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    old_a = _make(runs, "cli_aaaaaaaaaaaa", 30, now)
    old_b = _make(runs, "mcp_bbbbbbbbbbbb", 20, now)
    fresh = _make(runs, "cli_cccccccccccc", 1, now)
    web = _make(runs, "api_dddddddddddd", 40, now)

    preview = prune_runs(runs, max_age_days=7, keep_latest=1, dry_run=True, now=now)
    assert {c.path.name for c in preview.removed} == {old_a.name, old_b.name}
    assert old_a.exists() and old_b.exists()  # dry-run 不动磁盘

    result = prune_runs(runs, max_age_days=7, keep_latest=1, now=now)
    assert not old_a.exists() and not old_b.exists()
    assert fresh.exists()
    assert web.exists()  # 默认不清理 Web 会话
    assert result.freed_bytes > 0


def test_keep_latest_protects_even_old_dirs(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    newest_old = _make(runs, "cli_111111111111", 10, now)
    _make(runs, "cli_222222222222", 20, now)
    result = prune_runs(runs, max_age_days=7, keep_latest=1, now=now)
    assert newest_old.exists()
    assert len(result.removed) == 1


def test_clean_command_dry_run_and_delete(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    target = _make(runs, "cli_eeeeeeeeeeee", 30, now)
    runner = CliRunner()

    dry = runner.invoke(
        cli_module.app, ["clean", "--runs-dir", str(runs), "--keep", "0", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert "cli_eeeeeeeeeeee" in dry.output
    assert target.exists()

    real = runner.invoke(cli_module.app, ["clean", "--runs-dir", str(runs), "--keep", "0", "--yes"])
    assert real.exit_code == 0, real.output
    # 默认软删除：移入回收站并给出恢复说明，而不是直接 rmtree
    assert "已移动 1 个目录到回收站" in real.output
    assert "恢复方式" in real.output
    assert not target.exists()
    trash_batches = [p for p in (runs / "_trash").iterdir() if p.is_dir()]
    assert len(trash_batches) == 1
    assert (trash_batches[0] / target.name).is_dir()
    assert (trash_batches[0] / "MANIFEST.json").is_file()


def test_clean_with_nothing_to_do(tmp_path):
    (tmp_path / "runs").mkdir()
    result = CliRunner().invoke(cli_module.app, ["clean", "--runs-dir", str(tmp_path / "runs")])
    assert result.exit_code == 0
    assert "没有需要清理的目录" in result.output


def test_clean_rejects_missing_or_non_directory_runs_dir(tmp_path):
    """显式 --runs-dir 写错时不能静默返回成功（此前路径打错也报「没有需要清理」）。"""
    result = CliRunner().invoke(cli_module.app, ["clean", "--runs-dir", str(tmp_path / "typo")])
    assert result.exit_code == 2
    assert "不存在或不是目录" in result.output

    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")
    result = CliRunner().invoke(cli_module.app, ["clean", "--runs-dir", str(file_path)])
    assert result.exit_code == 2


def test_clean_reports_invalid_env_config(monkeypatch):
    """默认目录来自配置时，配置写错要给出「配置有误」而不是静默成功。"""
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "not-a-number")
    result = CliRunner().invoke(cli_module.app, ["clean"])
    assert result.exit_code == 2
    assert "配置有误" in result.output


def test_clean_rejects_days_zero_without_all(tmp_path):
    runs = tmp_path / "runs"
    _make(runs, "cli_000000000001", 30, time.time())
    result = CliRunner().invoke(
        cli_module.app, ["clean", "--runs-dir", str(runs), "--days", "0", "--yes"]
    )
    assert result.exit_code == 2
    assert "--all" in result.output
    # 目录仍在：参数被拒绝时不能动文件
    assert (runs / "cli_000000000001").exists()


def test_prune_skips_path_replaced_between_scan_and_delete(tmp_path, monkeypatch):
    """扫描后、删除前目录被换成链接/文件时不再删除（TOCTOU 防护）。"""
    now = time.time()
    runs = tmp_path / "runs"
    target = _make(runs, "cli_0123456789ab", 30, now)

    from pathlib import Path

    original = Path.is_symlink
    seen = {"count": 0}

    def fake_is_symlink(self):
        # 第一次是 find_session_dirs 的扫描（False → 入选），删除前复查返回 True
        if self.name == "cli_0123456789ab":
            seen["count"] += 1
            return seen["count"] > 1
        return original(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    result = prune_runs(runs, max_age_days=7, keep_latest=0)
    assert result.removed == []
    assert result.failed == [target]
    assert target.is_dir()  # 目录还在


def test_prune_removes_only_session_dirs_and_keeps_others(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    _make(runs, "cli_0123456789ab", 30, now)
    (runs / "_bundles").mkdir(parents=True)
    (runs / "memory.json").write_text("[]", encoding="utf-8")
    result = prune_runs(runs, max_age_days=7, keep_latest=0)
    assert len(result.removed) == 1
    assert (runs / "_bundles").exists()
    assert (runs / "memory.json").exists()


def test_version_flag_prints_version():
    result = CliRunner().invoke(cli_module.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip().startswith("data-agent ")


def test_find_session_dirs_skips_symlink(tmp_path, monkeypatch):
    """链接形态的会话目录不参与清理（避免跟着链接删到别处）。"""
    now = time.time()
    runs = tmp_path / "runs"
    _make(runs, "cli_0123456789ab", 30, now)

    from pathlib import Path

    original = Path.is_symlink

    def fake_is_symlink(self):
        if self.name == "cli_0123456789ab":
            return True
        return original(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    assert find_session_dirs(runs) == []


def test_scan_skips_unreadable_children(tmp_path, monkeypatch):
    """单个子文件 stat 失败时跳过它，而不是让整次扫描失败。"""
    now = time.time()
    runs = tmp_path / "runs"
    target = _make(runs, "cli_0123456789ab", 30, now)

    from pathlib import Path

    original = Path.stat

    def fake_stat(self, *args, **kwargs):
        if self.name == "data.csv":
            raise OSError("locked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    found = find_session_dirs(runs)
    assert [c.path for c in found] == [target]


def test_find_session_dirs_skips_unscannable_dir(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    _make(runs, "cli_0123456789ab", 30, now)

    def boom(_path):
        raise OSError("denied")

    monkeypatch.setattr(retention, "_scan", boom)
    assert find_session_dirs(runs) == []


def test_prune_rejects_negative_arguments(tmp_path):
    with pytest.raises(ValueError, match="max_age_days"):
        prune_runs(tmp_path, max_age_days=-1)
    with pytest.raises(ValueError, match="keep_latest"):
        prune_runs(tmp_path, max_age_days=1, keep_latest=-1)


def test_prune_records_failed_deletions(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    target = _make(runs, "cli_ffffffffffff", 30, now)

    def boom(path):
        raise PermissionError("in use")

    monkeypatch.setattr(retention.shutil, "rmtree", boom)
    result = prune_runs(runs, max_age_days=7, now=now)
    assert result.failed == [target]
    assert result.removed == []


@pytest.mark.parametrize(
    "size,expected",
    [(0, "0 B"), (1023, "1023 B"), (2048, "2.0 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.0 GB")],
)
def test_format_bytes(size, expected):
    assert format_bytes(size) == expected


def test_clean_include_web_and_abort_without_confirmation(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    web = _make(runs, "api_abcdefabcdef", 30, now)
    result = CliRunner().invoke(
        cli_module.app,
        ["clean", "--runs-dir", str(runs), "--keep", "0", "--include-web"],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "包含 Web 会话" in result.output
    assert web.exists()


def test_clean_lists_at_most_15_dirs_and_reports_failures(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    for i in range(17):
        _make(runs, f"cli_{i:012x}", 30 + i, now)

    def busy(_src, _dst):
        raise OSError("busy")

    # clean 默认走回收站（os.replace），这里让移动失败以覆盖错误分支
    monkeypatch.setattr(retention.os, "replace", busy)
    result = CliRunner().invoke(
        cli_module.app, ["clean", "--runs-dir", str(runs), "--keep", "0", "--yes"]
    )
    assert "另有 2 个" in result.output
    # 删除失败是运行期错误（1），与参数错误（2）区分开
    assert result.exit_code == 1
    assert "17 个目录清理失败" in result.output


def test_clean_reads_runs_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_RUNS_DIR", str(tmp_path / "custom-runs"))
    result = CliRunner().invoke(cli_module.app, ["clean"])
    assert result.exit_code == 0, result.output
    # rich 会按终端宽度折行，长临时路径可能被拆开，比较前去掉换行
    assert "custom-runs" in result.output.replace("\n", "")


@pytest.mark.parametrize("value", ["", "0", "abc"])
def test_auto_prune_disabled_or_invalid(tmp_path, monkeypatch, value):
    now = time.time()
    runs = tmp_path / "runs"
    old = _make(runs, "cli_999999999999", 30, now)
    monkeypatch.setenv(cli_module.RETENTION_ENV, value)
    monkeypatch.delenv(cli_module.RETENTION_MAX_ENV, raising=False)
    cli_module._auto_prune(runs)
    assert old.exists()


def test_auto_prune_removes_old_dirs_but_keeps_latest_10(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    dirs = [_make(runs, f"cli_{i:012x}", 10 + i, now) for i in range(12)]
    monkeypatch.setenv(cli_module.RETENTION_ENV, "7")
    cli_module._auto_prune(runs)
    remaining = sorted(
        d.name for d in runs.iterdir() if d.is_dir() and d.name != "_trash"
    )
    assert remaining == sorted(d.name for d in dirs[:10])
    # 自动清理同样走回收站，误清理可在保留期内恢复
    trashed = sorted(
        item.name
        for batch in (runs / "_trash").iterdir()
        if batch.is_dir()
        for item in batch.iterdir()
        if item.is_dir()
    )
    assert trashed == sorted(d.name for d in dirs[10:])


def test_prune_trash_writes_manifest_and_is_recoverable(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    target = _make(runs, "cli_0123456789ab", 30, now)

    result = prune_runs(
        runs, max_age_days=7, keep_latest=0, use_trash=True, now=now, trash_days=3
    )

    assert not target.exists()
    assert result.trash_dir is not None
    moved = result.trash_dir / target.name
    assert (moved / "input" / "data.csv").is_file()
    manifest = json.loads((result.trash_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"][0]["name"] == target.name
    assert "移回" in manifest["recovery"]
    assert "3" in manifest["recovery"]
    # 回收站不参与会话目录扫描，不会被重复清理
    assert find_session_dirs(runs) == []

    # 手工移回即可恢复
    os.replace(moved, runs / target.name)
    assert (runs / target.name / "input" / "data.csv").is_file()


def test_prune_skips_in_use_directories(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    busy = _make(runs, "api_abcdefabcdef", 30, now)
    idle = _make(runs, "api_111111111111", 30, now)

    result = prune_runs(
        runs,
        max_age_days=7,
        keep_latest=0,
        prefixes=("api",),
        is_protected=lambda path: path.name == busy.name,
        use_trash=True,
        now=now,
    )

    assert busy.exists()
    assert [item.path.name for item in result.skipped_active] == [busy.name]
    assert not idle.exists()
    assert [item.path.name for item in result.removed] == [idle.name]


def test_prune_trash_not_created_when_nothing_moved(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    _make(runs, "cli_0123456789ab", 1, now)

    result = prune_runs(runs, max_age_days=7, keep_latest=0, use_trash=True, now=now)

    assert result.removed == []
    assert result.trash_dir is None
    assert not (runs / "_trash").exists()


def test_purge_trash_removes_only_expired_batches(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    old_batch = runs / "_trash" / "old"
    new_batch = runs / "_trash" / "new"
    for batch in (old_batch, new_batch):
        (batch / "cli_0123456789ab").mkdir(parents=True)
        (batch / "cli_0123456789ab" / "data.csv").write_text("a\n1\n", encoding="utf-8")
    old_ts = now - 10 * DAY
    for path in (old_batch / "cli_0123456789ab" / "data.csv", old_batch / "cli_0123456789ab", old_batch):
        os.utime(path, (old_ts, old_ts))

    preview = purge_trash(runs, older_than_days=7, now=now, dry_run=True)
    assert [p.name for p in preview.removed] == ["old"]
    assert old_batch.exists()

    result = purge_trash(runs, older_than_days=7, now=now)
    assert [p.name for p in result.removed] == ["old"]
    assert result.freed_bytes > 0
    assert not old_batch.exists()
    assert new_batch.exists()
    assert result.kept == 1


def test_purge_trash_handles_missing_dir_and_negative_days(tmp_path):
    assert purge_trash(tmp_path / "none").removed == []
    with pytest.raises(ValueError, match="older_than_days"):
        purge_trash(tmp_path, older_than_days=-1)


def test_prune_logs_actions_and_summary(tmp_path, caplog):
    now = time.time()
    runs = tmp_path / "runs"
    _make(runs, "cli_0123456789ab", 30, now)

    with caplog.at_level(logging.INFO, logger="data_agent.retention"):
        prune_runs(runs, max_age_days=7, keep_latest=0, use_trash=True, now=now)

    assert "moved to trash" in caplog.text
    assert "cleaned 1 dirs" in caplog.text


def test_clean_permanent_deletes_without_trash(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    target = _make(runs, "cli_eeeeeeeeeeee", 30, now)
    result = CliRunner().invoke(
        cli_module.app,
        ["clean", "--runs-dir", str(runs), "--keep", "0", "--permanent", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "已永久删除 1 个目录" in result.output
    assert not target.exists()
    assert not (runs / "_trash").exists()


def test_prune_max_runs_caps_even_recent_dirs(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    dirs = [_make(runs, f"cli_{i:012x}", i + 1, now) for i in range(5)]

    result = prune_runs(runs, max_age_days=30, keep_latest=1, max_runs=2, now=now)

    assert len(result.removed) == 3
    assert [d.name for d in dirs[:2] if d.exists()] == [dirs[0].name, dirs[1].name]
    assert not dirs[2].exists()


def test_prune_rejects_negative_max_runs(tmp_path):
    with pytest.raises(ValueError, match="max_runs"):
        prune_runs(tmp_path, max_age_days=1, max_runs=-1)


def test_clean_max_runs_without_age_limit(tmp_path):
    now = time.time()
    runs = tmp_path / "runs"
    for i in range(3):
        _make(runs, f"cli_{i:012x}", i + 1, now)
    result = CliRunner().invoke(
        cli_module.app,
        ["clean", "--runs-dir", str(runs), "--days", "30", "--keep", "0", "--max-runs", "1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "最多保留 1 个" in result.output
    assert "已移动 2 个目录到回收站" in result.output


def test_clean_max_runs_wins_over_default_keep(tmp_path):
    """--max-runs 是硬上限：默认 --keep 20 不能把上限变成"至少保留 20 个"。"""
    now = time.time()
    runs = tmp_path / "runs"
    for i in range(5):
        _make(runs, f"cli_{i:012x}", i + 1, now)
    result = CliRunner().invoke(
        cli_module.app,
        ["clean", "--runs-dir", str(runs), "--days", "30", "--max-runs", "3", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "另外保留最新 3 个" in result.output
    assert "已移动 2 个目录到回收站" in result.output
    remaining = [p.name for p in runs.iterdir() if p.name.startswith("cli_")]
    assert len(remaining) == 3


def test_clean_max_runs_zero_falls_back_to_keep(tmp_path):
    """--max-runs 0 按帮助语义只看 --keep，而不是删空。"""
    now = time.time()
    runs = tmp_path / "runs"
    for i in range(4):
        _make(runs, f"cli_{i:012x}", i + 1, now)
    result = CliRunner().invoke(
        cli_module.app,
        [
            "clean",
            "--runs-dir",
            str(runs),
            "--days",
            "30",
            "--keep",
            "2",
            "--max-runs",
            "0",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "已移动 2 个目录到回收站" in result.output
    remaining = [p.name for p in runs.iterdir() if p.name.startswith("cli_")]
    assert len(remaining) == 2


def test_auto_prune_by_count_only(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    dirs = [_make(runs, f"cli_{i:012x}", 1 + i, now) for i in range(3)]
    monkeypatch.setenv(cli_module.RETENTION_ENV, "")
    monkeypatch.setenv(cli_module.RETENTION_MAX_ENV, "1")
    cli_module._auto_prune(runs)
    remaining = sorted(
        d.name for d in runs.iterdir() if d.is_dir() and d.name != "_trash"
    )
    assert remaining == [dirs[0].name]


def test_clean_protects_active_session_dirs(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    active = _make(runs, "cli_0123456789ab", 30, now)
    monkeypatch.setattr(cli_module, "_active_workspace_paths", lambda: {active})

    result = CliRunner().invoke(
        cli_module.app, ["clean", "--runs-dir", str(runs), "--keep", "0", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert active.exists()
    assert "正在使用的目录已跳过" in result.output


def test_auto_prune_invalid_max_env_is_ignored(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    old = _make(runs, "cli_999999999999", 30, now)
    monkeypatch.delenv(cli_module.RETENTION_ENV, raising=False)
    monkeypatch.setenv(cli_module.RETENTION_MAX_ENV, "abc")
    cli_module._auto_prune(runs)
    assert old.exists()


def test_find_session_dirs_returns_empty_for_missing_dir(tmp_path):
    assert find_session_dirs(tmp_path / "missing") == []


def test_prune_tolerates_failing_empty_trash_cleanup(tmp_path, monkeypatch):
    """空批次删不掉（如被占用）时不能影响清理结果，也不能留下 trash_dir。"""
    now = time.time()
    runs = tmp_path / "runs"
    kept = _make(runs, "cli_aaaaaaaaaaaa", 0, now)
    batch = runs / "_trash" / "20260101T000000-deadbe"
    batch.mkdir(parents=True)
    (batch / "occupied.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(retention, "_new_trash_batch", lambda *args, **kwargs: batch)

    result = prune_runs(runs, max_age_days=30, keep_latest=10, use_trash=True, now=now)

    assert result.removed == []
    assert result.trash_dir is None
    assert kept.exists()


def test_purge_trash_skips_non_dirs_and_records_scan_failures(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    base = runs / "_trash"
    base.mkdir(parents=True)
    (base / "stray.txt").write_text("x", encoding="utf-8")
    broken = base / "20200101T000000-broken"
    broken.mkdir()

    def boom(path):
        raise OSError("scan failed")

    monkeypatch.setattr(retention, "_scan", boom)
    result = purge_trash(runs, older_than_days=0, now=time.time())

    assert result.removed == []
    assert result.failed == [broken]


def test_purge_trash_tolerates_unreadable_directory(tmp_path, monkeypatch):
    from pathlib import Path

    runs = tmp_path / "runs"
    (runs / "_trash").mkdir(parents=True)
    monkeypatch.setattr(
        Path, "iterdir", lambda self: (_ for _ in ()).throw(OSError("denied"))
    )
    result = purge_trash(runs)
    assert result.removed == [] and result.kept == 0


def test_purge_trash_logs_rmtree_failure(tmp_path, monkeypatch, caplog):
    runs = tmp_path / "runs"
    batch = runs / "_trash" / "20200101T000000-old"
    batch.mkdir(parents=True)
    old = time.time() - 30 * DAY
    os.utime(batch, (old, old))

    def boom(path):
        raise OSError("locked")

    monkeypatch.setattr(retention.shutil, "rmtree", boom)
    with caplog.at_level(logging.WARNING, logger="data_agent.retention"):
        result = purge_trash(runs, older_than_days=7)

    assert result.failed == [batch]
    assert "failed to purge" in caplog.text

