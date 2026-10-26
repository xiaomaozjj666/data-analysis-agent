"""分析溯源：为一次分析建立可复核的证据链。

解决的问题：报告里的数字、图表与工具调用之间没有可验证的绑定关系。
事后要回答「这个结论是怎么来的、文件有没有被改过」，只能靠翻聊天记录。

做法：

- 会话根目录写 ``provenance.jsonl``：每条记录包含数据集、工具调用、产物、
  最终报告，``hash = sha256(prev + canonical(entry))`` 串成哈希链。
- 同时写 ``provenance.json`` 摘要（根哈希、条目数、创建时间）。
- ``verify`` 重新计算链，并对照磁盘上的数据集 / 产物文件与 ``session.json``
  里的报告正文，任何不一致都报出来。

哈希链只证明「记录之间没有被改写」，不提供签名或身份认证；它防的是
静默漂移与手滑编辑，不是有权限的对手。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROVENANCE_FILE = "provenance.jsonl"
SUMMARY_FILE = "provenance.json"
VERIFIER_VERSION = 1


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _make_entry(
    seq: int,
    prev: str,
    entry_type: str,
    name: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "seq": seq,
        "type": entry_type,
        "name": name,
        "data": data,
        "prev": prev,
    }
    entry["hash"] = _entry_hash(entry)
    return entry


def _relative_to_root(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def record_analysis(
    root: str | Path,
    *,
    task: str,
    response: str,
    trace: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    dataset_path: str | Path | None = None,
    provider: str = "",
    model: str = "",
    code_version: str = "",
) -> dict[str, Any]:
    """写入最近一次分析的溯源链，返回摘要。

    覆盖写是刻意的：会话只保留最近一次分析结果，溯源链与之一一对应。
    调用方应将异常吞掉并记日志——溯源失败不应让一次成功的分析变成 500。
    """
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    previous = ""

    def add(entry_type: str, name: str, data: dict[str, Any]) -> None:
        nonlocal previous
        entry = _make_entry(len(entries), previous, entry_type, name, data)
        entries.append(entry)
        previous = entry["hash"]

    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    add(
        "meta",
        "analysis",
        {
            "task_sha256": sha256_text(task),
            "task_chars": len(task),
            "provider": provider,
            "model": model,
            "code_version": code_version,
            "created_at": created_at,
        },
    )

    if dataset_path:
        dataset = Path(dataset_path)
        if dataset.is_file():
            data: dict[str, Any] = {
                "sha256": sha256_file(dataset),
                "size": dataset.stat().st_size,
            }
            relative = _relative_to_root(dataset, base)
            if relative is not None:
                data["path"] = relative
            else:
                data["external"] = True
                data["path"] = str(dataset.resolve())
            add("dataset", dataset.name, data)

    tool_calls = 0
    for item in trace or []:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail", ""))
        kind = str(item.get("type", ""))
        if kind == "tool_call":
            tool_calls += 1
        add(
            "tool",
            str(item.get("name", "tool")),
            {
                "kind": kind,
                "detail": detail,
                "detail_sha256": sha256_text(detail),
                "chars": len(detail),
            },
        )

    for item in artifacts or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "artifact"))
        data: dict[str, Any] = {
            "kind": str(item.get("kind", "")),
            "description": str(item.get("description", "")),
        }
        raw_path = item.get("path")
        if raw_path:
            path = Path(str(raw_path))
            if path.is_file():
                relative = _relative_to_root(path, base)
                if relative is not None:
                    data.update(
                        {
                            "path": relative,
                            "sha256": sha256_file(path),
                            "size": path.stat().st_size,
                        }
                    )
                else:
                    data.update(
                        {
                            "external": True,
                            "path": str(path.resolve()),
                            "sha256": sha256_file(path),
                            "size": path.stat().st_size,
                        }
                    )
        add("artifact", name, data)

    add(
        "report",
        "analysis-report",
        {"sha256": sha256_text(response), "chars": len(response)},
    )

    payload = "".join(_canonical(entry) + "\n" for entry in entries)
    target = base / PROVENANCE_FILE
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)

    summary: dict[str, Any] = {
        "verifier_version": VERIFIER_VERSION,
        "entries": len(entries),
        "root_hash": previous,
        "created_at": created_at,
        "provider": provider,
        "model": model,
        "code_version": code_version,
        "tool_calls": tool_calls,
        "artifacts": sum(1 for entry in entries if entry["type"] == "artifact"),
        "report_sha256": entries[-1]["data"]["sha256"],
    }
    summary_target = base / SUMMARY_FILE
    summary_tmp = summary_target.with_suffix(summary_target.suffix + ".tmp")
    summary_tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_tmp.replace(summary_target)
    return summary


def load_summary(root: str | Path) -> dict[str, Any] | None:
    path = Path(root) / SUMMARY_FILE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_entries(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"第 {line_no} 条不是合法 JSON。")
            continue
        if not isinstance(raw, dict):
            errors.append(f"第 {line_no} 条不是 JSON 对象。")
            continue
        entries.append(raw)
    return entries, errors


def verify(root: str | Path) -> dict[str, Any]:
    """复核溯源链与磁盘内容，返回 ``ok / entries / root_hash / errors``。"""
    base = Path(root)
    path = base / PROVENANCE_FILE
    if not path.is_file():
        return {
            "ok": False,
            "exists": False,
            "entries": 0,
            "root_hash": "",
            "checks": {},
            "errors": [f"{PROVENANCE_FILE} 不存在。"],
        }

    entries, errors = _read_entries(path)
    previous = ""
    for index, entry in enumerate(entries, start=1):
        if entry.get("prev", "") != previous:
            errors.append(f"第 {index} 条的 prev 与上一条 hash 不一致。")
        if entry.get("hash") != _entry_hash(entry):
            errors.append(f"第 {index} 条的 hash 校验失败。")
        previous = str(entry.get("hash", ""))
    checks: dict[str, Any] = {"chain": not errors}

    summary = load_summary(base)
    if summary is None:
        errors.append(f"{SUMMARY_FILE} 缺失或不是合法 JSON。")
    else:
        if summary.get("entries") != len(entries):
            errors.append("摘要条目数与实际条目数不一致。")
        if summary.get("root_hash") != previous:
            errors.append("摘要根哈希与链尾不一致。")

    for entry in entries:
        if entry.get("type") == "dataset":
            data = entry.get("data", {})
            name = str(entry.get("name", ""))
            relative = data.get("path")
            if relative:
                dataset = Path(str(relative)) if data.get("external") else base / str(relative)
            else:
                dataset = next(
                    (item for item in (base / "input" / name, base / name) if item.is_file()),
                    None,
                )
            if dataset is None or not dataset.is_file():
                errors.append(f"数据集文件缺失：{name}")
            elif sha256_file(dataset) != data.get("sha256"):
                errors.append(f"数据集哈希不匹配：{name}")
        elif entry.get("type") == "artifact":
            data = entry.get("data", {})
            relative = data.get("path")
            if not relative:
                continue
            artifact = Path(str(relative)) if data.get("external") else base / str(relative)
            if not artifact.is_file():
                errors.append(f"产物文件缺失：{relative}")
            elif sha256_file(artifact) != data.get("sha256"):
                errors.append(f"产物哈希不匹配：{relative}")

    session_path = base / "session.json"
    report_entries = [entry for entry in entries if entry.get("type") == "report"]
    if report_entries and session_path.is_file():
        expected = report_entries[-1].get("data", {}).get("sha256")
        try:
            manifest = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        response = (manifest.get("last_result") or {}).get("response")
        if isinstance(response, str) and expected != sha256_text(response):
            errors.append("报告正文与溯源记录不一致。")
    checks["report_compared"] = bool(report_entries and session_path.is_file())

    return {
        "ok": not errors,
        "exists": True,
        "entries": len(entries),
        "root_hash": previous,
        "checks": checks,
        "errors": errors,
        "summary": summary,
    }


def export_payload(root: str | Path) -> dict[str, Any]:
    """导出自包含的溯源 JSON（摘要 + 全部条目 + 校验结果）。"""
    base = Path(root)
    path = base / PROVENANCE_FILE
    entries, errors = _read_entries(path) if path.is_file() else ([], [])
    return {
        "summary": load_summary(base),
        "entries": entries,
        "verification": verify(base),
        "read_errors": errors,
    }
