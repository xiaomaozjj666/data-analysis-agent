"""runs/ 会话目录的保留策略。

CLI（``cli_*``）与 MCP（``mcp_*``）每次运行都会在 runs_dir 下新建一个会话目录，
且没有 ``session.json``，既不会出现在历史侧栏、也不受 SessionRegistry 的 TTL
裁剪，长期使用后会无限堆积。Web 会话（``api_*``）由注册表在内存里按 TTL 管理，
但进程重启后磁盘上的目录同样不会被清理。

安全约束（默认即安全）：

- **默认不自动删除**：只有在显式配置 ``DATA_AGENT_CLI_RETENTION_DAYS`` 或手动
  执行 ``data-agent clean`` 时才会清理，且默认只覆盖 ``cli_*`` / ``mcp_*``；
  用户数据（``api_*``）需要 ``--include-web`` 显式纳入。
- **只识别会话目录**：名字必须是「前缀 + 12 位十六进制」，永远不会动
  ``_bundles``、``_trash``、自动化任务目录、记忆文件、日志等其它内容。
- **可恢复**：默认「移入 runs/_trash/<时间戳>/」而不是直接删除，目录内写
  ``MANIFEST.json`` 记录原始路径与恢复说明；超过 ``trash`` 保留期后才会永久
  删除。需要立刻释放空间时可用 ``--permanent``（明确选择不可恢复）。
- **不误删在用目录**：调用方可传入 ``is_protected`` 判定（CLI 会保护注册表中
  正在分析 / 取消中的会话）；扫描与移动之间还会复查符号链接与目录类型。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

SESSION_DIR_RE = re.compile(r"^(cli|mcp|api)_[0-9a-f]{12}$")
#: 默认只清理一次性运行产生的目录；api_ 会话出现在 Web 历史里，需显式纳入
DEFAULT_PREFIXES: tuple[str, ...] = ("cli", "mcp")
ALL_PREFIXES: tuple[str, ...] = ("cli", "mcp", "api")

#: 软删除批次目录名（下划线开头，不会被 SESSION_DIR_RE 匹配）。
TRASH_DIRNAME = "_trash"
#: 回收站默认保留天数：期间可随时手动移回恢复。
DEFAULT_TRASH_DAYS = 7.0

RecoveryCheck = Callable[[Path], bool]


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
    #: 因为「正在使用」而被跳过的目录（不计入 kept，单独回报）。
    skipped_active: list[PruneCandidate] = field(default_factory=list)
    #: 软删除批次目录；``None`` 表示没有使用回收站（永久删除或没有候选）。
    trash_dir: Path | None = None

    @property
    def freed_bytes(self) -> int:
        return sum(item.size_bytes for item in self.removed)


@dataclass
class TrashPurgeResult:
    removed: list[Path] = field(default_factory=list)
    kept: int = 0
    failed: list[Path] = field(default_factory=list)
    freed_bytes: int = 0


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


def _new_trash_batch(runs_dir: Path, timestamp: float, trash_dirname: str) -> Path:
    base = runs_dir / trash_dirname
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(timestamp))
    batch = base / f"{stamp}-{uuid4().hex[:6]}"
    batch.mkdir()
    return batch


def _write_manifest(
    batch: Path, runs_dir: Path, entries: list[dict[str, object]], trash_days: float
) -> None:
    payload = {
        "created_at": time.time(),
        "runs_dir": str(runs_dir),
        "recovery": (
            f"将本目录下的会话目录移回 {runs_dir} 即可恢复；"
            f"超过 {trash_days:g} 天后 _trash 会整体永久删除。"
        ),
        "entries": entries,
    }
    try:
        (batch / "MANIFEST.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - 磁盘只读等极端情况
        logger.warning("retention: failed to write trash manifest at %s", batch)


def prune_runs(
    runs_dir: Path,
    *,
    max_age_days: float,
    keep_latest: int = 0,
    max_runs: int | None = None,
    prefixes: Iterable[str] = DEFAULT_PREFIXES,
    dry_run: bool = False,
    now: float | None = None,
    use_trash: bool = False,
    is_protected: RecoveryCheck | None = None,
    trash_dirname: str = TRASH_DIRNAME,
    trash_days: float = DEFAULT_TRASH_DAYS,
) -> PruneResult:
    """清理超过 ``max_age_days`` 未修改的会话目录，但总保留最新的 ``keep_latest`` 个。

    额外提供 ``max_runs``：无论多新，候选目录总数超过该值时清理较旧的部分，
    用于「只按数量保留」的机器/CI 环境。``max_runs=None`` 表示不限制。
    ``dry_run=True`` 时只返回将被删除的目录，不做任何改动。
    ``use_trash=True`` 时移动到 ``runs/_trash/<时间戳>/`` 并写恢复清单（推荐）。
    ``is_protected(path)`` 返回 True 的目录会被跳过并计入 ``skipped_active``。
    """
    if max_age_days < 0:
        raise ValueError("max_age_days 不能为负数。")
    if keep_latest < 0:
        raise ValueError("keep_latest 不能为负数。")
    if max_runs is not None and max_runs < 0:
        raise ValueError("max_runs 不能为负数。")
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    candidates = find_session_dirs(runs_dir, prefixes)
    result = PruneResult()

    batch: Path | None = None
    if use_trash and not dry_run:
        batch = _new_trash_batch(runs_dir, time.time() if now is None else now, trash_dirname)
        result.trash_dir = batch
    manifest_entries: list[dict[str, object]] = []

    for index, item in enumerate(candidates):
        if is_protected is not None and is_protected(item.path):
            result.skipped_active.append(item)
            logger.info(
                "retention: skip active %s (%s)", item.path, format_bytes(item.size_bytes)
            )
            continue
        beyond_cap = max_runs is not None and index >= max_runs
        too_old = item.last_modified < cutoff
        if index < keep_latest or (not beyond_cap and not too_old):
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
                logger.warning("retention: skip changed entry %s", item.path)
                continue
            if batch is not None:
                os.replace(item.path, batch / item.path.name)
            else:
                shutil.rmtree(item.path)
        except OSError as exc:
            result.failed.append(item.path)
            logger.warning("retention: failed to clean %s: %s", item.path, exc)
        else:
            result.removed.append(item)
            manifest_entries.append(
                {
                    "name": item.path.name,
                    "size_bytes": item.size_bytes,
                    "last_modified": item.last_modified,
                }
            )
            action = "moved to trash" if batch is not None else "deleted"
            logger.info(
                "retention: %s %s (%s, last_modified=%s)",
                action,
                item.path,
                format_bytes(item.size_bytes),
                time.strftime("%Y-%m-%d", time.gmtime(item.last_modified)),
            )

    if batch is not None:
        if manifest_entries:
            _write_manifest(batch, runs_dir, manifest_entries, trash_days)
        else:  # 没有真正移动任何目录时不要把空批次留在磁盘上
            try:
                batch.rmdir()
                batch.parent.rmdir()  # 连空的 _trash 一起收掉
            except OSError:
                pass
            result.trash_dir = None

    if result.removed:
        logger.info(
            "retention: cleaned %d dirs (%s), kept %d, skipped_active %d, failed %d",
            len(result.removed),
            format_bytes(result.freed_bytes),
            result.kept,
            len(result.skipped_active),
            len(result.failed),
        )
    return result


def purge_trash(
    runs_dir: Path,
    *,
    older_than_days: float = DEFAULT_TRASH_DAYS,
    now: float | None = None,
    dry_run: bool = False,
    trash_dirname: str = TRASH_DIRNAME,
) -> TrashPurgeResult:
    """永久删除超过保留期的回收站批次（``runs/_trash/<批>``）。

    空目录 / 符号链接一律跳过；删除失败只记日志并计入 ``failed``，
    不影响其余批次。默认保留 7 天，期间可随时手动把会话目录移回 runs/ 恢复。
    """
    if older_than_days < 0:
        raise ValueError("older_than_days 不能为负数。")
    result = TrashPurgeResult()
    base = runs_dir / trash_dirname
    if not base.is_dir() or base.is_symlink():
        return result
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            latest, size = _scan(entry)
        except OSError:
            result.failed.append(entry)
            continue
        if latest >= cutoff:
            result.kept += 1
            continue
        if dry_run:
            result.removed.append(entry)
            result.freed_bytes += size
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            result.failed.append(entry)
            logger.warning("retention: failed to purge trash %s: %s", entry, exc)
        else:
            result.removed.append(entry)
            result.freed_bytes += size
            logger.info(
                "retention: purged trash batch %s (%s)", entry, format_bytes(size)
            )
    return result


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - 循环内已返回
