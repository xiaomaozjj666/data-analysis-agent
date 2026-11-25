"""runs/ 保留策略：只删会话目录、尊重保留数与时间阈值、CLI clean 命令可用。"""

from __future__ import annotations

import os
import time

import pytest
from typer.testing import CliRunner

from data_agent import cli as cli_module
from data_agent import retention
from data_agent.retention import find_session_dirs, format_bytes, prune_runs

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
    assert "已删除 1 个目录" in real.output
    assert not target.exists()


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
    def busy(path):
        raise OSError("busy")

    monkeypatch.setattr(retention.shutil, "rmtree", busy)
    result = CliRunner().invoke(
        cli_module.app, ["clean", "--runs-dir", str(runs), "--keep", "0", "--yes"]
    )
    assert "另有 2 个" in result.output
    # 删除失败是运行期错误（1），与参数错误（2）区分开
    assert result.exit_code == 1
    assert "17 个目录删除失败" in result.output


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
    cli_module._auto_prune(runs)
    assert old.exists()


def test_auto_prune_removes_old_dirs_but_keeps_latest_10(tmp_path, monkeypatch):
    now = time.time()
    runs = tmp_path / "runs"
    dirs = [_make(runs, f"cli_{i:012x}", 10 + i, now) for i in range(12)]
    monkeypatch.setenv(cli_module.RETENTION_ENV, "7")
    cli_module._auto_prune(runs)
    remaining = sorted(d.name for d in runs.iterdir())
    assert remaining == sorted(d.name for d in dirs[:10])
