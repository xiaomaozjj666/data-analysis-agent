"""裁决台账：每次 compare 的结论、指标与依据都留痕。

台账用哈希链串起来（``prev`` + 条目内容 → ``hash``），任何一条被悄悄
改写都会在 ``verify_ledger`` 里断开。目的不是防内部人，而是让「这次
改动到底凭什么被接受」在几周后仍可复核、可回滚。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_agent.evalkit.harness import EvaluationReport


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entry_hash(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    last = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    if not last:
        return ""
    try:
        return str(json.loads(last).get("hash", ""))
    except json.JSONDecodeError:
        return ""


def append_ledger(
    path: str | Path,
    report: EvaluationReport,
    *,
    suite_fingerprint: str,
    git_sha: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """追加一条裁决记录并返回写入的条目（含哈希）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha,
        "suite_fingerprint": suite_fingerprint,
        "decision": report.decision,
        "metrics": report.metrics,
        "reasons": report.reasons,
        "policy": report.policy.to_dict(),
        "baseline_variant": report.baseline_variant,
        "candidate_variant": report.candidate_variant,
        "notes": notes,
        "prev": _last_hash(target),
    }
    entry["hash"] = _entry_hash(entry)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(entry) + "\n")
    return entry


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line_no, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target}:{line_no} 不是合法 JSON：{exc}") from exc
    return entries


def verify_ledger(path: str | Path) -> dict[str, Any]:
    """校验哈希链，返回 ``{"ok", "entries", "errors"}``。"""
    entries = read_ledger(path)
    errors: list[str] = []
    previous = ""
    for index, entry in enumerate(entries, start=1):
        if entry.get("prev", "") != previous:
            errors.append(f"第 {index} 条的 prev 与上一条 hash 不一致。")
        expected = _entry_hash(entry)
        if entry.get("hash") != expected:
            errors.append(f"第 {index} 条的 hash 校验失败。")
        previous = str(entry.get("hash", ""))
    return {"ok": not errors, "entries": len(entries), "errors": errors}
