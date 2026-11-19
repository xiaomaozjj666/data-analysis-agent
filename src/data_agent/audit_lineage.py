"""上传 CSV 的目录血缘与新鲜度：回答「这份数据来自哪张治理表、是否已过期」。

企业审计不只问 CSV 的哈希，还会问上游是哪张仓库表、何时抽取、谁负责、
是否超过新鲜度 SLA。这里把这些问题落成一个可版本化的小文件
``audit_lineage.json``，并在核验结果、账本与离线验签中绑定其指纹。

字段::

    {"catalog_urn": "urn:li:dataset:(warehouse,dwd.orders_daily,PROD)",
     "upstream_table": "dwd.orders_daily",
     "extracted_at": "2026-11-19T08:00:00+08:00",
     "owner": "data-platform",
     "freshness_sla_hours": 24}

``catalog_urn`` 与 ``upstream_table`` 至少提供一个；``extracted_at`` 必须带
时区，否则跨时区团队无法判断新鲜度。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_agent._workspace_bundles import _atomic_write_text

LINEAGE_FILENAME = "audit_lineage.json"
MAX_TEXT = 300


def _text(raw: Any, field: str, *, required: bool = False) -> str:
    value = str(raw or "").strip()
    if len(value) > MAX_TEXT:
        raise ValueError(f"{field} 最多 {MAX_TEXT} 个字符。")
    if required and not value:
        raise ValueError(f"缺少 {field}。")
    return value


def parse_lineage(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("来源血缘必须是对象。")
    catalog_urn = _text(payload.get("catalog_urn"), "catalog_urn")
    upstream_table = _text(payload.get("upstream_table"), "upstream_table")
    if not catalog_urn and not upstream_table:
        raise ValueError("catalog_urn 与 upstream_table 至少填写一个。")
    extracted_raw = str(payload.get("extracted_at") or "").strip()
    if not extracted_raw:
        raise ValueError("缺少 extracted_at。")
    try:
        extracted_at = datetime.fromisoformat(extracted_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("extracted_at 必须是带时区的 ISO 8601 时间。") from exc
    if extracted_at.tzinfo is None:
        raise ValueError("extracted_at 必须带时区，例如 2026-11-19T08:00:00+08:00。")
    try:
        sla = int(payload.get("freshness_sla_hours", 24))
    except (TypeError, ValueError) as exc:
        raise ValueError("freshness_sla_hours 必须是整数。") from exc
    if sla < 1 or sla > 8760:
        raise ValueError("freshness_sla_hours 必须在 1–8760 之间。")
    return {
        "catalog_urn": catalog_urn or None,
        "upstream_table": upstream_table or None,
        "extracted_at": extracted_at.isoformat(),
        "owner": _text(payload.get("owner"), "owner"),
        "freshness_sla_hours": sla,
    }


def lineage_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_lineage(root: str | Path) -> dict[str, Any] | None:
    path = Path(root) / LINEAGE_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取来源血缘 {LINEAGE_FILENAME}：{exc}") from exc
    return parse_lineage(payload)


def save_lineage(root: str | Path, payload: Any) -> dict[str, Any]:
    lineage = parse_lineage(payload)
    _atomic_write_text(
        Path(root) / LINEAGE_FILENAME,
        json.dumps(lineage, ensure_ascii=False, indent=2),
    )
    return lineage


def freshness(
    lineage: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """计算抽取距今小时数与是否超过 SLA。"""
    extracted = datetime.fromisoformat(str(lineage["extracted_at"]))
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    age_hours = max(0.0, (moment - extracted).total_seconds() / 3600)
    sla = int(lineage["freshness_sla_hours"])
    return {
        "age_hours": round(age_hours, 2),
        "freshness_sla_hours": sla,
        "stale": age_hours > sla,
    }


def describe(lineage: dict[str, Any]) -> str:
    source = lineage.get("upstream_table") or lineage.get("catalog_urn") or "未命名上游"
    owner = lineage.get("owner") or "未登记负责人"
    return f"{source} · {owner}"
