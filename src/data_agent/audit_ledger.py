"""经营简报核验的只追加哈希链账本。

每次「复算核验」和「确认发布」都写一条记录，记录内容包含源 CSV、草稿与核验版
简报的 SHA-256，并用前一条记录的哈希串起来。任何一条被改动，后续哈希全部对不上。

设计取舍：
- 账本按会话存放在工作区根目录，随会话一起删除；它是演示级的篡改可见性，
  不是不可否认的签名基础设施（没有私钥，也没做外部时间戳）。
- 只支持单进程追加（uvicorn 单 worker）；模块内用线程锁串行化写入。
- 追加前先校验整条链：如果账本已被破坏，宁可报错也不继续往上写。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_NAME = "audit_ledger.jsonl"
GENESIS_HASH = "0" * 64
_DEFAULT_LIMIT = 50
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class LedgerError(RuntimeError):
    """账本不可用（损坏、无法写入或链校验失败）。"""


def ledger_path(root: Path) -> Path:
    return Path(root) / LEDGER_NAME


def _lock_key(path: Path) -> str:
    """同一账本路径必须始终得到同一把锁。

    不能用 ``Path.resolve()``：Windows 上文件不存在时返回普通绝对路径，
    文件存在后返回带 ``\\\\?\\`` 前缀的扩展路径，同一文件会映射成两个键，
    并发首写就会各持一把锁把链写坏。``abspath + normcase`` 不受文件是否存在影响。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def _lock_for(path: Path) -> threading.Lock:
    key = _lock_key(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


_SIGNATURE_FIELDS = ("hash", "signature", "signature_key_id")


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {key: value for key, value in entry.items() if key not in _SIGNATURE_FIELDS}
    return hashlib.sha256(_canonical(body)).hexdigest()


def read_entries(path: Path) -> list[dict[str, Any]]:
    """逐行读取账本；空文件返回空列表。行解析失败也算账本损坏。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LedgerError(f"无法读取账本：{exc}") from exc
    entries = []
    # 只按 \n 切行：字段里合法的 U+2028/U+2029/U+0085 会被 str.splitlines()
    # 误当成换行，把一条完整 JSON 记录拆成两半。
    for number, line in enumerate(raw.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"账本第 {number} 行不是合法 JSON：{exc}") from exc
        if not isinstance(entry, dict):
            raise LedgerError(f"账本第 {number} 行不是 JSON 对象。")
        entries.append(entry)
    return entries


def verify_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """校验整条链：序号连续、prev 指向前一条、hash 可复算。"""
    errors: list[str] = []
    previous = GENESIS_HASH
    for index, entry in enumerate(entries, start=1):
        if entry.get("seq") != index:
            errors.append(f"第 {index} 条记录序号为 {entry.get('seq')!r}，应为 {index}。")
        if entry.get("prev") != previous:
            errors.append(f"第 {index} 条记录的 prev 与上一条哈希不一致。")
        expected = _entry_hash(entry)
        if entry.get("hash") != expected:
            errors.append(f"第 {index} 条记录的哈希无法复算。")
        previous = str(entry.get("hash", ""))
    return {
        "valid": not errors,
        "entries": len(entries),
        "signed": sum(1 for entry in entries if entry.get("signature")),
        "tip": previous if entries else GENESIS_HASH,
        "errors": errors,
    }


def verify_ledger(path: Path) -> dict[str, Any]:
    try:
        entries = read_entries(path)
    except LedgerError as exc:
        return {"valid": False, "entries": 0, "tip": GENESIS_HASH, "errors": [str(exc)]}
    return verify_entries(entries)


def append_entry(
    root: Path,
    *,
    kind: str,
    session_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """向账本追加一条记录并返回它；链已损坏时拒绝追加。"""
    path = ledger_path(root)
    lock = _lock_for(path)
    with lock:
        entries = read_entries(path)
        verification = verify_entries(entries)
        if not verification["valid"]:
            raise LedgerError("账本链校验失败，拒绝继续追加：" + "；".join(verification["errors"]))
        entry: dict[str, Any] = {
            "seq": len(entries) + 1,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "session_id": session_id,
            "prev": entries[-1]["hash"] if entries else GENESIS_HASH,
        }
        entry.update(payload)
        entry["hash"] = _entry_hash(entry)
        from data_agent.audit_signing import sign_hash

        try:
            entry.update(sign_hash(root, entry["hash"]))
        except OSError as exc:
            raise LedgerError(f"无法写入账本：签名密钥不可用（{exc}）") from exc
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise LedgerError(f"无法写入账本：{exc}") from exc
        return entry


def recent_entries(root: Path, *, limit: int = _DEFAULT_LIMIT) -> dict[str, Any]:
    entries = read_entries(ledger_path(root))
    verification = verify_entries(entries)
    return {
        "ledger": LEDGER_NAME,
        "verification": verification,
        "entries": entries[-limit:],
    }


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
