"""runs/ 会话目录的保留策略。

CLI（``cli_*``）与 MCP（``mcp_*``）每次运行都会在 runs_dir 下新建一个会话目录，
且没有 ``session.json``，既不会出现在历史侧栏、也不受 SessionRegistry 的 TTL
裁剪，长期使用后会无限堆积。Web 会话（``api_*``）由注册表在内存里按 TTL 管理，
但进程重启后磁盘上的目录同样不会被清理。

本模块只识别「前缀 + 12 位十六进制」的会话目录，永远不会动 ``_bundles``、
自动化任务目录、记忆文件、日志等其它内容。
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

SESSION_DIR_RE = re.compile(r"^(cli|mcp|api)_[0-9a-f]{12}$")
#: 默认只清理一次性运行产生的目录；api_ 会话出现在 Web 历史里，需显式纳入
DEFAULT_PREFIXES: tuple[str, ...] = ("cli", "mcp")
ALL_PREFIXES: tuple[str, ...] = ("cli", "mcp", "api")


@dataclass
class PruneCandidate:
    path: Path
    last_modified: float
    size_bytes: int


@dataclass
class PruneResult:
    removed: list[PruneCandidate] = field(default_factory=list)
    kept: int = 0
    failed: list[Path] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(item.size_bytes for item in self.removed)


def _scan(path: Path) -> tuple[float, int]:
    """返回目录内最新的修改时间与总字节数（目录本身的 mtime 也计入）。"""
    latest = path.stat().st_mtime
    total = 0
    for child in path.rglob("*"):
        try:
            stat = child.stat()
        except OSError:
            continue
        latest = max(latest, stat.st_mtime)
        if child.is_file():
            total += stat.st_size
    return latest, total


def find_session_dirs(runs_dir: Path, prefixes: Iterable[str] = DEFAULT_PREFIXES) -> list[PruneCandidate]:
    """列出 runs_dir 下属于指定前缀的会话目录，按最近修改时间从新到旧排序。"""
    wanted = set(prefixes)
    if not runs_dir.is_dir():
        return []
    found: list[PruneCandidate] = []
    for entry in runs_dir.iterdir():
        match = SESSION_DIR_RE.match(entry.name)
        if not match or match.group(1) not in wanted or not entry.is_dir() or entry.is_symlink():
            continue
        try:
            latest, size = _scan(entry)
        except OSError:
            continue
        found.append(PruneCandidate(entry, latest, size))
    found.sort(key=lambda item: item.last_modified, reverse=True)
    return found


def prune_runs(
    runs_dir: Path,
    *,
    max_age_days: float,
    keep_latest: int = 0,
    prefixes: Iterable[str] = DEFAULT_PREFIXES,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneResult:
    """删除超过 ``max_age_days`` 未修改的会话目录，但总保留最新的 ``keep_latest`` 个。

    ``dry_run=True`` 时只返回将被删除的目录，不做任何改动。
    """
    if max_age_days < 0:
        raise ValueError("max_age_days 不能为负数。")
    if keep_latest < 0:
        raise ValueError("keep_latest 不能为负数。")
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    candidates = find_session_dirs(runs_dir, prefixes)
    result = PruneResult()
    for index, item in enumerate(candidates):
        if index < keep_latest or item.last_modified >= cutoff:
            result.kept += 1
            continue
        if dry_run:
            result.removed.append(item)
            continue
        try:
            # 扫描与删除之间目录可能被替换成链接/文件：删除前复查，
            # 避免 rmtree 跟着链接删到输出目录之外。
            if item.path.is_symlink() or not item.path.is_dir():
                result.failed.append(item.path)
                continue
            shutil.rmtree(item.path)
        except OSError:
            result.failed.append(item.path)
        else:
            result.removed.append(item)
    return result


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - 循环内已返回
