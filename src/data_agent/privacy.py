"""把进模型的样例行做脱敏，不改活动数据集。

规划 / inspect 会把 ``profile.sample`` 发给云端模型。样例最多 20 行，
但足够带出手机号、邮箱、身份证。这里只改送出的副本。
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

_SENSITIVE_NAME = re.compile(
    r"(phone|mobile|tel|email|mail|id[_-]?card|password|secret|token|"
    r"手机|电话|邮箱|身份证|密码|证件)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_CN_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CN_ID = re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")


def _mask_text(value: str) -> str:
    text = _EMAIL.sub("[已脱敏-邮箱]", value)
    text = _CN_ID.sub("[已脱敏-证件]", text)
    text = _CN_MOBILE.sub("[已脱敏-手机]", text)
    return text


def mask_cell(column: str, value: Any) -> Any:
    """对单个单元格脱敏；无法识别的类型原样返回。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return value
    text = str(value)
    if _SENSITIVE_NAME.search(str(column)):
        if not text:
            return value
        if len(text) <= 2:
            return "*" * len(text)
        return text[0] + "*" * (len(text) - 2) + text[-1]
    masked = _mask_text(text)
    return masked if masked != text else value


def mask_sample_records(frame: pd.DataFrame, sample_rows: int) -> list[dict[str, Any]]:
    """返回脱敏后的样例记录（``orient=records``）。"""
    sample = frame.head(max(1, min(int(sample_rows), 20)))
    records: list[dict[str, Any]] = []
    for row in sample.to_dict(orient="records"):
        records.append({str(key): mask_cell(str(key), value) for key, value in row.items()})
    return records
