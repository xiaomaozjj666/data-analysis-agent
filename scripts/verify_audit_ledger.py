"""离线校验经营简报的哈希链账本与导出文件。

用法示例：
    python scripts/verify_audit_ledger.py runs/<session>/audit_ledger.jsonl
    python scripts/verify_audit_ledger.py runs/<session>/audit_ledger.jsonl \
        --brief 经营简报-核验版.md --source runs/<session>/input/sales.csv

校验内容：
1. 账本每一行的 seq / prev / hash 是否首尾相接、可复算；
2. 若给出 --brief，则要求它的 SHA-256 出现在某条 confirm 记录里；
3. 若给出 --source，则要求它的 SHA-256 出现在某条 audit/confirm 记录里。

退出码：0 = 通过；1 = 校验失败；2 = 参数或文件读取错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from data_agent.audit_ledger import read_entries, verify_entries  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验经营简报核验账本的哈希链。")
    parser.add_argument("ledger", type=Path, help="audit_ledger.jsonl 的路径")
    parser.add_argument("--brief", type=Path, help="待核对的核验版简报文件")
    parser.add_argument("--source", type=Path, help="待核对的原始 CSV")
    parser.add_argument("--trace", type=Path, help="待核对的快速起草轨迹 JSON")
    parser.add_argument("--schema", type=Path, help="待核对的核验口径 audit_schema.json")
    parser.add_argument("--lineage", type=Path, help="待核对的来源血缘 audit_lineage.json")
    parser.add_argument("--audit-id", help="只查看该 audit_id 的记录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    try:
        entries = read_entries(args.ledger)
    except FileNotFoundError:
        print(f"[失败] 账本不存在：{args.ledger}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 需要把任何读取错误转成退出码
        print(f"[失败] 无法读取账本：{exc}", file=sys.stderr)
        return 2

    verification = verify_entries(entries)
    checks: dict[str, object] = {}

    if args.audit_id:
        matched = [entry for entry in entries if entry.get("audit_id") == args.audit_id]
        checks["audit_id"] = {"value": args.audit_id, "records": len(matched)}
        if not matched:
            verification["errors"].append(f"账本中没有 audit_id={args.audit_id} 的记录。")
            verification["valid"] = False

    if args.brief:
        try:
            brief_sha = _sha256(args.brief)
        except OSError as exc:
            print(f"[失败] 无法读取简报：{exc}", file=sys.stderr)
            return 2
        matched = [
            entry
            for entry in entries
            if entry.get("kind") == "confirm" and entry.get("brief_sha256") == brief_sha
        ]
        checks["brief"] = {
            "path": str(args.brief),
            "sha256": brief_sha,
            "matched_seq": [entry["seq"] for entry in matched],
        }
        if not matched:
            verification["valid"] = False
            verification["errors"].append(
                "简报内容与账本中的任何一条确认记录都不匹配（文件被改动过）。"
            )

    if args.trace:
        try:
            trace_raw = args.trace.read_bytes()
            trace_sha = hashlib.sha256(trace_raw).hexdigest()
            trace = json.loads(trace_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"[失败] 无法读取起草轨迹：{exc}", file=sys.stderr)
            return 2
        matched = [
            entry for entry in entries if entry.get("draft_trace_sha256") == trace_sha
        ]
        checks["trace"] = {
            "path": str(args.trace),
            "sha256": trace_sha,
            "matched_seq": [entry["seq"] for entry in matched],
        }
        if not matched:
            verification["valid"] = False
            verification["errors"].append(
                "起草轨迹与账本中的任何一条记录都不匹配（轨迹或账本被改动过）。"
            )
        else:
            # 轨迹内部的草稿哈希必须与账本记录的草稿哈希一致，否则是「轨迹被换过」。
            recorded = {entry.get("draft_sha256") for entry in matched}
            if trace.get("parsed_draft_sha256") not in recorded:
                verification["valid"] = False
                verification["errors"].append(
                    "起草轨迹记录的草稿哈希与账本记录的草稿哈希不一致。"
                )

    if args.schema:
        try:
            from data_agent.audit_schema import parse_schema, schema_fingerprint

            schema_payload = json.loads(args.schema.read_text(encoding="utf-8"))
            fingerprint = schema_fingerprint(parse_schema(schema_payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"[失败] 无法读取核验口径：{exc}", file=sys.stderr)
            return 2
        matched = [
            entry for entry in entries if entry.get("schema_fingerprint") == fingerprint
        ]
        checks["schema"] = {
            "path": str(args.schema),
            "fingerprint": fingerprint,
            "matched_seq": [entry["seq"] for entry in matched],
        }
        if not matched:
            verification["valid"] = False
            verification["errors"].append(
                "核验口径与账本中的任何一条记录都不匹配（口径文件被改动过）。"
            )

    if args.lineage:
        try:
            from data_agent.audit_lineage import lineage_fingerprint, parse_lineage

            lineage_payload = json.loads(args.lineage.read_text(encoding="utf-8"))
            lineage_sha = lineage_fingerprint(parse_lineage(lineage_payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"[失败] 无法读取来源血缘：{exc}", file=sys.stderr)
            return 2
        matched = [
            entry for entry in entries if entry.get("lineage_sha256") == lineage_sha
        ]
        checks["lineage"] = {
            "path": str(args.lineage),
            "fingerprint": lineage_sha,
            "matched_seq": [entry["seq"] for entry in matched],
        }
        if not matched:
            verification["valid"] = False
            verification["errors"].append(
                "来源血缘与账本中的任何一条记录都不匹配（血缘文件被改动过）。"
            )

    if args.source:
        try:
            source_sha = _sha256(args.source)
        except OSError as exc:
            print(f"[失败] 无法读取原始 CSV：{exc}", file=sys.stderr)
            return 2
        matched = [
            entry
            for entry in entries
            if entry.get("source_sha256") == source_sha
        ]
        checks["source"] = {
            "path": str(args.source),
            "sha256": source_sha,
            "matched_seq": [entry["seq"] for entry in matched],
        }
        if not matched:
            verification["valid"] = False
            verification["errors"].append(
                "原始 CSV 与账本中的任何一条记录都不匹配（数据被替换过）。"
            )

    payload = {"verification": verification, "checks": checks}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        status = "通过" if verification["valid"] else "失败"
        print(f"[{status}] 账本共 {verification['entries']} 条记录，链尾 {verification['tip'][:16]}…")
        for name, check in checks.items():
            if name == "brief":
                print(
                    f"  简报 {check['path']}：SHA-256 {check['sha256'][:16]}…，"
                    f"命中记录 {check['matched_seq'] or '无'}"
                )
            elif name == "lineage":
                print(
                    f"  来源血缘 {check['path']}：指纹 {check['fingerprint'][:16]}…，"
                    f"命中记录 {check['matched_seq'] or '无'}"
                )
            elif name == "schema":
                print(
                    f"  核验口径 {check['path']}：指纹 {check['fingerprint'][:16]}…，"
                    f"命中记录 {check['matched_seq'] or '无'}"
                )
            elif name == "trace":
                print(
                    f"  起草轨迹 {check['path']}：SHA-256 {check['sha256'][:16]}…，"
                    f"命中记录 {check['matched_seq'] or '无'}"
                )
            elif name == "source":
                print(
                    f"  原始 CSV {check['path']}：SHA-256 {check['sha256'][:16]}…，"
                    f"命中记录 {check['matched_seq'] or '无'}"
                )
            elif name == "audit_id":
                print(f"  audit_id {check['value']}：{check['records']} 条记录")
        for error in verification["errors"]:
            print(f"  - {error}")

    return 0 if verification["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
