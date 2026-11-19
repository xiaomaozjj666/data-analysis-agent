"""会话级访问策略：PII 脱敏列与行级权限（RLS）。

企业审计里，核验器不能把主体无权查看的行算进指标，也不能把 PII 原值
写进证据预览。策略文件 ``audit_policy.json``::

    {"principal_id": "u_east",
     "claims": {"region": "华东"},
     "pii_columns": ["phone"],
     "rls": {"column": "region", "claim": "region"}}

- ``pii_columns``：证据预览中的这些列显示为“已脱敏”，不输出原值；
- ``rls``：按主体声明（claims）过滤行，过滤发生在聚合之前；
- 主体缺少对应 claim 时拒绝核验（403），而不是静默放行。

策略指纹与主体标识写入账本，离线验签时可核对“这次核验以谁的身份、
在什么过滤条件下完成”。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from data_agent._workspace_bundles import _atomic_write_text

POLICY_FILENAME = "audit_policy.json"
MAX_COLUMNS = 20
MAX_CLAIMS = 20


def _strings(raw: Any, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{field} 必须是字符串数组。")
    values = tuple(str(item).strip() for item in raw if str(item).strip())
    if len(values) > MAX_COLUMNS:
        raise ValueError(f"{field} 最多 {MAX_COLUMNS} 项。")
    return values


def parse_policy(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("访问策略必须是对象。")
    principal_id = str(payload.get("principal_id") or "").strip()
    if not principal_id:
        raise ValueError("缺少 principal_id。")
    claims_raw = payload.get("claims") or {}
    if not isinstance(claims_raw, dict):
        raise ValueError("claims 必须是对象。")
    claims = {str(key).strip(): str(value).strip() for key, value in claims_raw.items()}
    if len(claims) > MAX_CLAIMS:
        raise ValueError(f"claims 最多 {MAX_CLAIMS} 项。")
    rls_raw = payload.get("rls")
    rls = None
    if rls_raw is not None:
        if not isinstance(rls_raw, dict):
            raise ValueError("rls 必须是对象。")
        column = str(rls_raw.get("column") or "").strip()
        claim = str(rls_raw.get("claim") or "").strip()
        if not column or not claim:
            raise ValueError("rls 必须同时提供 column 与 claim。")
        if claim not in claims:
            raise ValueError(f"主体缺少 RLS 所需的 claim：{claim}。")
        rls = {"column": column, "claim": claim}
    return {
        "principal_id": principal_id,
        "claims": claims,
        "pii_columns": _strings(payload.get("pii_columns"), "pii_columns"),
        "rls": rls,
    }


def policy_fingerprint(policy: dict[str, Any]) -> str:
    canonical = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rls_filter_hash(column: str, value: str) -> str:
    canonical = json.dumps(
        {"column": column, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_policy(root: str | Path) -> dict[str, Any] | None:
    path = Path(root) / POLICY_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取访问策略 {POLICY_FILENAME}：{exc}") from exc
    return parse_policy(payload)


def save_policy(root: str | Path, payload: Any) -> dict[str, Any]:
    policy = parse_policy(payload)
    _atomic_write_text(
        Path(root) / POLICY_FILENAME,
        json.dumps(policy, ensure_ascii=False, indent=2),
    )
    return policy


def rls_value(policy: dict[str, Any]) -> tuple[str | None, str | None]:
    """返回 (过滤列, 主体对应的值)；没有 RLS 时返回 (None, None)。"""
    rls = policy.get("rls")
    if not rls:
        return None, None
    claim = str(rls["claim"])
    value = policy.get("claims", {}).get(claim)
    if value is None:
        raise ValueError(f"主体缺少 RLS 所需的 claim：{claim}。")
    return str(rls["column"]), str(value)
