"""数据清洗辅助函数：列名规范化、文本修剪、数值解析、缺失值与离群值处理。

这些函数被 ``builder.build_tools`` 中的 ``clean_data`` 工具调用，本身不
绑定 workspace，对传入的 DataFrame 原地或拷贝修改。
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .charts import _numeric_columns


def _normalize_column_names(
    df: pd.DataFrame,
    columns: list[str] | None,
    datetime_columns: list[str] | None,
) -> tuple[pd.DataFrame, list[str] | None, list[str] | None, bool]:
    """Lowercase and sanitize column names; remap caller-provided selections."""
    original = list(df.columns)
    normalized: list[str] = []
    seen: dict[str, int] = {}
    for value in original:
        base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(value).strip().lower()).strip("_") or "column"
        seen[base] = seen.get(base, 0) + 1
        normalized.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    if normalized == original:
        return df, columns, datetime_columns, False
    df = df.copy()
    df.columns = normalized
    mapping = dict(zip(original, normalized, strict=True))
    if columns:
        columns = [mapping.get(value, value) for value in columns]
    if datetime_columns:
        datetime_columns = [mapping.get(value, value) for value in datetime_columns]
    return df, columns, datetime_columns, True


def _trim_string_columns(df: pd.DataFrame) -> int:
    """Trim leading/trailing whitespace on object/string columns in place."""
    string_columns = list(df.select_dtypes(include=["object", "string"]).columns)
    for column in string_columns:
        df[column] = df[column].map(lambda value: value.strip() if isinstance(value, str) else value)
    return len(string_columns)


def _parse_numeric_columns(df: pd.DataFrame, threshold: float = 0.8) -> list[str]:
    """Coerce object/string columns whose numeric ratio meets ``threshold``."""
    converted: list[str] = []
    for column in df.select_dtypes(include=["object", "string"]).columns:
        non_empty = df[column].dropna()
        if non_empty.empty:
            continue
        candidate = pd.to_numeric(df[column], errors="coerce")
        if candidate.notna().sum() / len(non_empty) >= threshold:
            df[column] = candidate
            converted.append(str(column))
    return converted


def _apply_missing_strategy(
    df: pd.DataFrame,
    selected: list[str],
    strategy: str,
) -> None:
    """Apply the requested missing-value strategy in place."""
    if strategy == "drop":
        old_len = len(df)
        df.dropna(subset=selected, inplace=True)
        df.reset_index(drop=True, inplace=True)
        dropped = old_len - len(df)
        if dropped > 0 and dropped / max(old_len, 1) > 0.5:
            raise ValueError(
                f"拒绝高比例删行：将删除 {dropped}/{old_len} 行。"
                "请改用填充策略，或先让用户确认后分批处理。"
            )
        return
    if strategy in {"forward_fill", "backward_fill"}:
        method = "ffill" if strategy == "forward_fill" else "bfill"
        df[selected] = getattr(df[selected], method)()
        return
    for column in selected:
        series = df[column]
        if not series.isna().any():
            continue
        if strategy in {"mean", "median"}:
            if not pd.api.types.is_numeric_dtype(series):
                raise ValueError(f"列 {column} 不是数值列，不能使用 {strategy} 填充。")
            fill_value = getattr(series, strategy)()
        else:
            modes = series.mode(dropna=True)
            if modes.empty:
                continue
            fill_value = modes.iloc[0]
        df[column] = series.fillna(fill_value)


def _handle_outliers(
    df: pd.DataFrame,
    selected: list[str],
    method: str,
    action: str,
) -> tuple[int, dict[str, tuple[float, float]]]:
    """Detect and optionally cap/remove outliers on numeric selected columns."""
    numeric = _numeric_columns(df, selected)
    masks: dict[str, pd.Series] = {}
    bounds: dict[str, tuple[float, float]] = {}
    for column in numeric:
        series = df[column]
        if method == "iqr":
            q1, q3 = series.quantile([0.25, 0.75])
            spread = q3 - q1
            lower, upper = float(q1 - 1.5 * spread), float(q3 + 1.5 * spread)
        else:
            mean, std = float(series.mean()), float(series.std(ddof=0))
            if std == 0 or not np.isfinite(std):
                continue
            lower, upper = mean - 3 * std, mean + 3 * std
        bounds[column] = (lower, upper)
        masks[column] = series.lt(lower) | series.gt(upper)
    count = int(pd.DataFrame(masks).any(axis=1).sum()) if masks else 0
    if action == "remove" and masks:
        if count / max(len(df), 1) > 0.3:
            raise ValueError(
                f"拒绝一次删除过多离群记录：将删除 {count}/{len(df)} 行。"
                "请改用 cap，或缩小需要检查的列。"
            )
        df.drop(pd.DataFrame(masks).any(axis=1).loc[lambda s: s].index, inplace=True)
        df.reset_index(drop=True, inplace=True)
    elif action == "cap":
        for column, (lower, upper) in bounds.items():
            df[column] = df[column].clip(lower, upper)
    return count, bounds
