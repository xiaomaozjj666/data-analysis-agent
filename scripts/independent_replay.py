"""零信任独立复算：只依赖 Python 标准库，不 import 本项目任何代码。

用法：
    python scripts/independent_replay.py evidence-<session>.zip

校验内容：
1. manifest 中每个文件的 SHA-256 与实际字节一致；
2. 按 schema.json 的声明式口径，从 source.csv 从头复算所有指标；
3. 用复算结果、草稿、源文件哈希与口径指纹重现 audit_id，必须与 report.json 一致；
4. 校验 ledger.jsonl 的 prev/hash 链，且账本不能为空；
5. 每条账本记录都必须带 Ed25519 签名，并用包内 public_key.pem 逐条验证；
6. 账本中必须有与复算 audit_id 匹配的 confirm 记录（确认门），
   简报、口径、访问策略、来源血缘、起草轨迹的指纹都必须出现在账本中；
7. 包内 schema.json / lineage.json / policy.json / draft_trace.json 的指纹
   必须与 report.json 及账本记录一致（防止替换证据文件）。

退出码：0 = 全部通过；1 = 任一校验失败；2 = 参数或文件读取错误。
"""

from __future__ import annotations

import base64
import csv
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


# 内置销售口径：与 src/data_agent/routers/audit_bundle.py 的 _BUILTIN_SCHEMA 一致。
# 内置口径不写口径指纹，若不复算这份标准，替换 schema.json 的 unit/aliases
# 这类展示元数据就不会被账本抓到。改内置口径时必须同步修改这里。
_BUILTIN_SCHEMA = {
    "metrics": [
        {
            "name": "销售额合计",
            "column": "sales",
            "aggregation": "sum",
            "unit": "元",
            "decimals": 2,
        },
        {
            "name": "销量合计",
            "column": "quantity",
            "aggregation": "sum",
            "unit": "件",
            "decimals": 0,
            "data_type": "integer",
        },
        {
            "name": "订单数",
            "column": "order_id",
            "aggregation": "count_distinct",
            "unit": "笔",
        },
        {
            "name": "客单价",
            "aggregation": "ratio",
            "numerator": "销售额合计",
            "denominator": "订单数",
            "unit": "元",
            "decimals": 2,
        },
    ]
}
BUILTIN_FINGERPRINT = schema_fingerprint(parse_schema(_BUILTIN_SCHEMA))


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



_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    if x & 1:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _xrecover(_BY)
_B = [_BX % _P, _BY % _P, 1, _BX * _BY % _P]


def _edwards(point: list[int], other: list[int]) -> list[int]:
    x1, y1, z1, t1 = point
    x2, y2, z2, t2 = other
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    dd = z1 * 2 * z2 % _P
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return [e * f % _P, g * h % _P, f * g % _P, e * h % _P]


def _scalarmult(point: list[int], scalar: int) -> list[int]:
    result = [0, 1, 1, 0]
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards(result, addend)
        addend = _edwards(addend, addend)
        scalar >>= 1
    return result


def _encode_point(point: list[int]) -> bytes:
    x, y, z, _ = point
    inverse = pow(z, _P - 2, _P)
    x = x * inverse % _P
    y = y * inverse % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(raw: bytes) -> list[int]:
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (raw[31] >> 7):
        x = _P - x
    return [x, y, 1, x * y % _P]


def _public_from_pem(pem: bytes) -> bytes:
    body = "".join(
        line
        for line in pem.decode("ascii", "replace").splitlines()
        if line and not line.startswith("-----")
    )
    return base64.b64decode(body)[-32:]


def verify_ed25519(public_pem: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64:
        return False
    public = _public_from_pem(public_pem)
    r = signature[:32]
    scalar = int.from_bytes(signature[32:], "little")
    if scalar >= _L:
        return False
    try:
        left = _scalarmult(_B, scalar)
        digest = int.from_bytes(
            hashlib.sha512(r + public + message).digest(), "little"
        ) % _L
        right = _edwards(_decode_point(r), _scalarmult(_decode_point(public), digest))
        return _encode_point(left) == _encode_point(right)
    except (ValueError, ZeroDivisionError):
        return False


def verify_entries_signatures(entries: list[dict], pem: bytes) -> list[str]:
    errors = []
    for index, entry in enumerate(entries, start=1):
        signature = entry.get("signature")
        if not signature:
            errors.append(f"第 {index} 条记录没有 Ed25519 签名。")
            continue
        try:
            raw = base64.b64decode(signature)
        except (ValueError, TypeError):
            errors.append(f"第 {index} 条记录的签名编码无效。")
            continue
        if not verify_ed25519(pem, str(entry.get("hash", "")).encode("ascii"), raw):
            errors.append(f"第 {index} 条记录的 Ed25519 签名无效。")
    return errors


def verify_ledger(raw: bytes) -> tuple[bool, list[str], list[dict]]:
    entries = []
    errors = []
    previous = GENESIS
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, ["账本不是 UTF-8 文本。"], []
    for number, line in enumerate(text.split("\n"), 1):
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
        body = {
            key: value
            for key, value in entry.items()
            if key not in ("hash", "signature", "signature_key_id")
        }
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
            policy = json.loads(files["policy.json"].decode("utf-8"))
            if not isinstance(policy, dict):
                raise ValueError("访问策略必须是 JSON 对象。")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            print(f"[失败] 访问策略无法解析：{exc}", file=sys.stderr)
            return 2
    rls_column = None
    rls_value = None
    if policy and policy.get("rls"):
        rls_column = str(policy["rls"].get("column") or "")
        claim = str(policy["rls"].get("claim") or "")
        rls_value = str((policy.get("claims") or {}).get(claim) or "")

    try:
        metric_specs = parse_schema(schema_payload)
    except ValueError as exc:
        print(f"[失败] 核验口径无法解析：{exc}", file=sys.stderr)
        return 1
    schema_sha = schema_fingerprint(metric_specs)
    report_schema_sha = report.get("schema_fingerprint")
    if report_schema_sha is not None:
        if schema_sha != report_schema_sha:
            problems.append(
                "schema.json 复算出的口径指纹与 report.json 不一致（口径文件被替换）。"
            )
    elif schema_sha != BUILTIN_FINGERPRINT:
        problems.append(
            "report 声明为内置口径，但 schema.json 与内置口径不一致（口径文件被替换）。"
        )

    try:
        metrics = read_metrics(
            files["source.csv"],
            metric_specs,
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

    ledger_raw = files.get("ledger.jsonl")
    entries: list[dict] = []
    ledger_ok = False
    if ledger_raw is None:
        problems.append("证据包缺少 ledger.jsonl：无法证明任何一次审计与确认。")
    else:
        ledger_ok, ledger_errors, entries = verify_ledger(ledger_raw)
        problems.extend(ledger_errors)
        if not entries:
            problems.append("账本为空：无法证明任何一次审计与确认。")

    signed = sum(1 for entry in entries if entry.get("signature"))
    signature_ok = False
    if entries:
        unsigned = len(entries) - signed
        if unsigned:
            problems.append(
                f"账本有 {unsigned} 条记录缺少 Ed25519 签名（签名可能被剥离）。"
            )
        if signed:
            if "public_key.pem" not in files:
                problems.append(
                    "账本包含 Ed25519 签名，但证据包缺少 public_key.pem：无法验签。"
                )
            else:
                try:
                    signature_errors = verify_entries_signatures(
                        entries, files["public_key.pem"]
                    )
                except ValueError as exc:
                    problems.append(f"公钥无法解析：{exc}")
                else:
                    problems.extend(signature_errors)
                    signature_ok = not unsigned and not signature_errors

        # 确认门锚定：必须有一条与本次复算 audit_id 匹配的 confirm 记录。
        # 这三项检查此前缩进在“账本里有签名”分支内，导致删光签名或清空账本
        # 反而跳过全部锚定检查并判通过（2026-11-21 审查 blocking）。
        if not any(
            entry.get("kind") == "confirm" and entry.get("audit_id") == replay_id
            for entry in entries
        ):
            problems.append(
                "账本中没有与本次复算 audit_id 匹配的 confirm 记录（确认门未通过）。"
            )
        brief = report.get("corrected_brief")
        if brief and not any(
            entry.get("brief_sha256") == sha256(brief.encode("utf-8"))
            for entry in entries
        ):
            problems.append("核验版简报的哈希不在账本中。")
        if report_schema_sha:
            if not any(
                entry.get("schema_fingerprint") == report_schema_sha
                for entry in entries
            ):
                problems.append("口径指纹不在账本中。")
        elif any(
            entry.get("audit_id") == replay_id and entry.get("schema_fingerprint")
            for entry in entries
        ):
            problems.append("账本记录了口径指纹，但 report 声明为内置口径（证据不一致）。")
        policy_sha = report.get("policy_fingerprint")
        if policy_sha and not any(
            entry.get("policy_sha256") == policy_sha for entry in entries
        ):
            problems.append("访问策略指纹不在账本中。")

    if policy is not None:
        if not report.get("policy_fingerprint"):
            problems.append("证据包含访问策略，但 report.json 没有记录策略指纹。")
        elif sha256(canonical(policy)) != report["policy_fingerprint"]:
            problems.append(
                "policy.json 复算出的策略指纹与 report.json 不一致（策略文件被替换）。"
            )
    elif report.get("policy_fingerprint"):
        problems.append("report.json 记录了访问策略指纹，但证据包缺少 policy.json。")

    if "lineage.json" in files:
        try:
            lineage_payload = json.loads(files["lineage.json"].decode("utf-8"))
            if not isinstance(lineage_payload, dict):
                raise ValueError("来源血缘必须是 JSON 对象。")
            lineage_sha = sha256(canonical(lineage_payload))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            problems.append(f"lineage.json 无法解析：{exc}")
        else:
            if not any(
                entry.get("lineage_sha256") == lineage_sha for entry in entries
            ):
                problems.append("来源血缘的指纹不在账本中（血缘文件被替换）。")
    elif any(entry.get("lineage_sha256") for entry in entries):
        problems.append("账本记录了来源血缘，但证据包缺少 lineage.json。")

    trace_sha = report.get("draft_trace_sha256")
    trace_raw = files.get("draft_trace.json")
    if trace_sha:
        if trace_raw is None:
            problems.append("report 绑定了起草轨迹，但证据包缺少 draft_trace.json。")
        else:
            if sha256(trace_raw) != trace_sha:
                problems.append(
                    "draft_trace.json 的 SHA-256 与 report.json 不一致（轨迹被替换）。"
                )
            if not any(
                entry.get("draft_trace_sha256") == trace_sha
                and entry.get("audit_id") == replay_id
                for entry in entries
            ):
                problems.append("起草轨迹哈希不在与该 audit_id 匹配的账本记录中。")
            try:
                trace = json.loads(trace_raw.decode("utf-8"))
                if not isinstance(trace, dict):
                    raise ValueError("起草轨迹必须是 JSON 对象。")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                problems.append(f"draft_trace.json 无法解析：{exc}")
            else:
                accepted = {sha256(files["draft.txt"])}
                accepted.update(
                    entry.get("draft_sha256")
                    for entry in entries
                    if entry.get("audit_id") == replay_id
                )
                if trace.get("parsed_draft_sha256") not in accepted:
                    problems.append(
                        "起草轨迹记录的草稿哈希与 draft.txt 及账本记录都不一致。"
                    )
    elif trace_raw is not None:
        problems.append("证据包含起草轨迹，但 report.json 没有绑定其哈希。")

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
    print(f"  confirm     {sum(1 for entry in entries if entry.get('kind') == 'confirm')} 条")
    print(
        f"  signature   {signed}/{len(entries)} 条，"
        f"Ed25519 {'有效' if signature_ok else '无效'}"
    )
    if report_schema_sha:
        print(f"  schema      {report_schema_sha[:16]}…")
    else:
        print("  schema      内置口径（账本无口径指纹，指标仍由本脚本复算）")
    print("  该结果由仅依赖 Python 标准库的脚本从头计算，未 import 项目代码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
