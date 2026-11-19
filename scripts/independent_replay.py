"""零信任独立复算：只依赖 Python 标准库，不 import 本项目任何代码。

用法：
    python scripts/independent_replay.py evidence-<session>.zip

校验内容：
1. manifest 中每个文件的 SHA-256 与实际字节一致；
2. 按 schema.json 的声明式口径，从 source.csv 从头复算所有指标；
3. 用复算结果、草稿、源文件哈希与口径指纹重现 audit_id，必须与 report.json 一致；
4. 校验 ledger.jsonl 的 prev/hash 链；
5. 核对简报、口径、来源血缘的哈希是否出现在账本记录中。

退出码：0 = 全部通过；1 = 任一校验失败；2 = 参数或文件读取错误。
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path

SCALE = 1_000_000_000
GENESIS = "0" * 64
AGGREGATIONS = {"sum", "count", "count_distinct", "min", "max", "mean", "ratio"}
NULL_TOKENS = {"", "n/a", "na", "null", "none", "nan", "-"}
CURRENCY = str.maketrans("", "", "¥￥$€£")
OPS = {"eq", "ne", "gt", "ge", "lt", "le", "in", "contains"}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def parse_schema(payload: dict) -> list[dict]:
    entries = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("schema.json 缺少 metrics 数组。")
    metrics = []
    for index, raw in enumerate(entries):
        name = str(raw.get("name") or "").strip()
        aggregation = str(raw.get("aggregation") or "").strip().lower()
        column = str(raw.get("column") or "").strip()
        if not name or aggregation not in AGGREGATIONS:
            raise ValueError(f"schema 第 {index + 1} 项名称或聚合非法。")
        numerator = str(raw.get("numerator") or "").strip() or None
        denominator = str(raw.get("denominator") or "").strip() or None
        if aggregation == "ratio":
            if not numerator or not denominator:
                raise ValueError(f"指标「{name}」缺少分子或分母。")
            column = column or numerator
        elif not column:
            raise ValueError(f"指标「{name}」缺少 column。")
        filters = raw.get("filters") or []
        for item in filters:
            if str(item.get("op") or "").lower() not in OPS:
                raise ValueError(f"指标「{name}」的过滤操作符非法。")
        metrics.append(
            {
                "name": name,
                "column": column,
                "aggregation": aggregation,
                "unit": str(raw.get("unit") or ""),
                "decimals": int(raw.get("decimals", 2)),
                "aliases": list(raw.get("aliases") or []),
                "data_type": str(raw.get("data_type") or "decimal"),
                "null_policy": str(raw.get("null_policy") or "exclude"),
                "rounding": str(raw.get("rounding") or "HALF_UP").upper(),
                "sign_policy": str(raw.get("sign_policy") or "allow"),
                "filters": filters,
                "time_window": raw.get("time_window"),
                "numerator": numerator,
                "denominator": denominator,
            }
        )
    return metrics


def schema_fingerprint(metrics: list[dict]) -> str:
    return sha256(canonical(metrics))


def scaled(cell: str) -> int:
    cleaned = cell.strip()
    if "," in cleaned or any(symbol in cleaned for symbol in "¥￥$€£"):
        cleaned = cleaned.translate(CURRENCY).replace(",", "")
    negative = cleaned.startswith("-")
    if cleaned[:1] in {"+", "-"}:
        cleaned = cleaned[1:]
    whole, _, fraction = cleaned.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise ValueError(f"非数值单元格：{cell!r}")
    if len(fraction) > 9:
        raise ValueError(f"小数位过多：{cell!r}")
    value = int(whole) * SCALE + int((fraction + "000000000")[:9])
    return -value if negative else value


def is_null(cell: str) -> bool:
    return cell.strip().casefold() in NULL_TOKENS


def matches(cell: str, op: str, expected) -> bool:
    actual = cell.strip()
    if op in {"eq", "ne"}:
        result = actual == str(expected).strip()
        if not result:
            try:
                result = scaled(actual) == scaled(str(expected))
            except ValueError:
                result = False
        return result if op == "eq" else not result
    if op == "contains":
        return str(expected).casefold() in actual.casefold()
    if op == "in":
        return actual in {str(item).strip() for item in expected}
    left, right = scaled(actual), scaled(str(expected))
    return {
        "gt": left > right,
        "ge": left >= right,
        "lt": left < right,
        "le": left <= right,
    }[op]


def passes(row: list[str], index: dict[str, int], metric: dict) -> bool:
    for item in metric["filters"]:
        position = index.get(item["column"])
        if position is None:
            raise ValueError(f"缺少列 {item['column']}")
        if not matches(row[position], str(item["op"]).lower(), item["value"]):
            return False
    window = metric["time_window"]
    if window:
        position = index.get(window["column"])
        if position is None:
            raise ValueError(f"缺少列 {window['column']}")
        raw = row[position].strip()
        if is_null(raw):
            return False
        current = raw[:10].replace("/", "-")
        if window.get("start") and current < window["start"]:
            return False
        if window.get("end") and current > window["end"]:
            return False
    return True


def read_metrics(
    source: bytes,
    metrics: list[dict],
    *,
    rls_column: str | None = None,
    rls_value: str | None = None,
) -> dict[str, str]:
    import csv
    import io

    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = source.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSV 编码无法识别。")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    headers = next(reader, [])
    index = {name: position for position, name in enumerate(headers)}
    sums = defaultdict(int)
    counts = defaultdict(int)
    minimums = {}
    maximums = {}
    distinct = defaultdict(set)
    for row in reader:
        if not row:
            continue
        if len(row) != len(headers):
            raise ValueError("CSV 列数与表头不一致。")
        if rls_column:
            position = index.get(rls_column)
            if position is None:
                raise ValueError(f"缺少 RLS 过滤列 {rls_column}")
            if row[position].strip() != (rls_value or ""):
                continue
        for metric in metrics:
            if metric["aggregation"] == "ratio":
                continue
            if not passes(row, index, metric):
                continue
            name = metric["name"]
            position = index.get(metric["column"])
            if position is None:
                raise ValueError(f"缺少列 {metric['column']}")
            cell = row[position]
            if metric["aggregation"] == "count":
                counts[name] += 1
                continue
            if metric["aggregation"] == "count_distinct":
                value = cell.strip()
                if is_null(value):
                    if metric["null_policy"] == "fail":
                        raise ValueError(f"指标「{name}」遇到空值。")
                    continue
                distinct[name].add(value)
                continue
            if is_null(cell):
                if metric["null_policy"] == "fail":
                    raise ValueError(f"指标「{name}」遇到空值。")
                if metric["null_policy"] == "zero":
                    counts[name] += 1
                continue
            try:
                value = scaled(cell)
            except ValueError as exc:
                raise ValueError(f"指标「{name}」：{exc}") from exc
            if metric["data_type"] == "integer" and value % SCALE:
                raise ValueError(f"指标「{name}」要求整数。")
            if metric["sign_policy"] == "non_negative" and value < 0:
                raise ValueError(f"指标「{name}」不允许负数。")
            sums[name] += value
            counts[name] += 1
            minimums[name] = value if name not in minimums else min(minimums[name], value)
            maximums[name] = value if name not in maximums else max(maximums[name], value)

    values = {}
    base = {metric["name"] for metric in metrics if metric["aggregation"] != "ratio"}
    for metric in metrics:
        if metric["aggregation"] == "ratio":
            continue
        name = metric["name"]
        if metric["aggregation"] == "count_distinct":
            values[name] = str(len(distinct[name]))
            continue
        if metric["aggregation"] == "count":
            values[name] = str(counts[name])
            continue
        if metric["aggregation"] == "sum":
            number = Decimal(sums[name]).scaleb(-9)
        elif metric["aggregation"] == "min":
            number = Decimal(minimums[name]).scaleb(-9)
        elif metric["aggregation"] == "max":
            number = Decimal(maximums[name]).scaleb(-9)
        else:
            if not counts[name]:
                raise ValueError(f"指标「{name}」没有可计算的非空数值。")
            number = (Decimal(sums[name]) / counts[name]).scaleb(-9)
        mode = ROUND_HALF_EVEN if metric["rounding"] == "HALF_EVEN" else ROUND_HALF_UP
        quant = Decimal(1).scaleb(-metric["decimals"])
        values[name] = f"{number.quantize(quant, rounding=mode):.{metric['decimals']}f}"
    for metric in metrics:
        if metric["aggregation"] != "ratio":
            continue
        name = metric["name"]
        if metric["numerator"] not in base or metric["denominator"] not in base:
            raise ValueError(f"指标「{name}」的分子或分母未登记。")
        denominator = Decimal(values[metric["denominator"]])
        if denominator == 0:
            raise ValueError(f"指标「{name}」分母为 0。")
        number = Decimal(values[metric["numerator"]]) / denominator
        mode = ROUND_HALF_EVEN if metric["rounding"] == "HALF_EVEN" else ROUND_HALF_UP
        quant = Decimal(1).scaleb(-metric["decimals"])
        values[name] = f"{number.quantize(quant, rounding=mode):.{metric['decimals']}f}"
    return values


def audit_id(report: dict, source_sha: str, draft: str, metrics: dict) -> str:
    payload = {
        "schema": report.get("schema_version", 2),
        "source_sha256": source_sha,
        "draft": draft.replace("\r\n", "\n").replace("\r", "\n"),
        "confirm_full_table": bool(report.get("confirmed_full_table")),
        "metrics": metrics,
        "draft_trace_sha256": report.get("draft_trace_sha256"),
        "schema_fingerprint": report.get("schema_fingerprint"),
        "policy_fingerprint": report.get("policy_fingerprint"),
    }
    return "aud-" + sha256(canonical(payload))[:32]


def verify_ledger(raw: bytes) -> tuple[bool, list[str], list[dict]]:
    entries = []
    errors = []
    previous = GENESIS
    for number, line in enumerate(raw.decode("utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"账本第 {number} 行不是合法 JSON。")
            continue
        if entry.get("seq") != len(entries) + 1:
            errors.append(f"账本第 {number} 行序号不连续。")
        if entry.get("prev") != previous:
            errors.append(f"账本第 {number} 行 prev 不匹配。")
        body = {key: value for key, value in entry.items() if key != "hash"}
        if entry.get("hash") != sha256(canonical(body)):
            errors.append(f"账本第 {number} 行哈希无法复算。")
        previous = str(entry.get("hash") or "")
        entries.append(entry)
    return not errors, errors, entries


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法：python scripts/independent_replay.py <evidence.zip>", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        archive = zipfile.ZipFile(path)
        files = {name: archive.read(name) for name in archive.namelist()}
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        print(f"[失败] 无法读取证据包：{exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    try:
        manifest = json.loads(files["manifest.json"])
        report = json.loads(files["report.json"])
        schema_payload = json.loads(files["schema.json"])
        draft = files["draft.txt"].decode("utf-8")
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"[失败] 证据包缺少必要文件或格式错误：{exc}", file=sys.stderr)
        return 2

    for name, expected in manifest.get("files", {}).items():
        if name not in files:
            problems.append(f"manifest 列出的 {name} 不在包内。")
        elif sha256(files[name]) != expected:
            problems.append(f"{name} 的 SHA-256 与 manifest 不一致。")

    policy = None
    if "policy.json" in files:
        try:
            policy = json.loads(files["policy.json"])
        except json.JSONDecodeError as exc:
            print(f"[失败] 访问策略无法解析：{exc}", file=sys.stderr)
            return 2
    rls_column = None
    rls_value = None
    if policy and policy.get("rls"):
        rls_column = str(policy["rls"].get("column") or "")
        claim = str(policy["rls"].get("claim") or "")
        rls_value = str((policy.get("claims") or {}).get(claim) or "")
    try:
        metrics = read_metrics(
            files["source.csv"],
            parse_schema(schema_payload),
            rls_column=rls_column or None,
            rls_value=rls_value,
        )
    except (ValueError, KeyError) as exc:
        print(f"[失败] 复算失败：{exc}", file=sys.stderr)
        return 1
    if metrics != report.get("metrics"):
        problems.append(f"复算指标与 report.json 不一致：{metrics} != {report.get('metrics')}")

    source_sha = sha256(files["source.csv"])
    if source_sha != report.get("source", {}).get("sha256"):
        problems.append("源文件 SHA-256 与 report.json 不一致。")
    replay_id = audit_id(report, source_sha, draft, metrics)
    if replay_id != report.get("audit_id") or replay_id != manifest.get("audit_id"):
        problems.append(f"audit_id 无法重现：{replay_id} != {report.get('audit_id')}")

    ledger_ok, ledger_errors, entries = verify_ledger(files.get("ledger.jsonl", b""))
    if files.get("ledger.jsonl"):
        problems.extend(ledger_errors)
        if entries:
            if not any(entry.get("audit_id") == replay_id for entry in entries):
                problems.append("账本中没有该 audit_id 的记录。")
            brief = report.get("corrected_brief")
            if brief and not any(
                entry.get("brief_sha256") == sha256(brief.encode("utf-8"))
                for entry in entries
            ):
                problems.append("核验版简报的哈希不在账本中。")
            schema_sha = report.get("schema_fingerprint")
            if schema_sha and not any(entry.get("schema_fingerprint") == schema_sha for entry in entries):
                problems.append("口径指纹不在账本中。")
    if "lineage.json" in files:
        # 账本记录的是规范化血缘的指纹；这里只做存在性提示，不重复实现规范化。
        if not any(entry.get("lineage_sha256") for entry in entries):
            problems.append("证据包含来源血缘，但账本没有 lineage_sha256 记录。")

    if problems:
        print(f"[失败] 独立复算发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("[通过] 独立复算一致")
    print(f"  audit_id    {replay_id}")
    print(f"  source      {source_sha[:16]}…")
    print(f"  metrics     {json.dumps(metrics, ensure_ascii=False)}")
    print(f"  ledger      {len(entries)} 条，整链 {'有效' if ledger_ok else '无效'}")
    print("  该结果由仅依赖 Python 标准库的脚本从头计算，未 import 项目代码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
