"""JSON 安全序列化工具：将 Python/Pandas/NumPy 对象转为合法 JSON。

核心问题：
- pandas DataFrame/Series 不能直接 json.dumps。
- NumPy 标量（np.int64、np.float64）不是 JSON 原生类型。
- NaN / Infinity 在 JSON 规范中非法，必须转为 null。
- datetime / Timestamp / Path 需要转为字符串。

本模块提供两个函数：
- ``to_jsonable(value)``: 递归转换为 JSON 兼容的 Python 对象。
- ``json_text(value)``: to_jsonable + json.dumps，返回格式化 JSON 字符串。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def to_jsonable(value: Any) -> Any:
    """递归将任意 Python/Pandas/NumPy 对象转为 JSON 兼容类型。

    转换规则：
    - None / str / int / bool → 原样返回
    - float → NaN/Infinity 转为 None
    - np.integer → int
    - np.floating → float（NaN/Infinity 转 None）
    - datetime / date / Timestamp → ISO 8601 字符串
    - Path → 字符串
    - DataFrame → list[dict]（records 方向）
    - Series → dict
    - ndarray → list
    - dict → 递归转换键值
    - list / tuple / set → 递归转换元素
    - 其他 → str() 回退

    Args:
        value: 任意 Python 对象。

    Returns:
        可被 json.dumps 安全序列化的对象。
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if not np.isfinite(value) else value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return None if not np.isfinite(number) else number
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.DataFrame):
        return [to_jsonable(row) for row in value.to_dict(orient="records")]
    if isinstance(value, pd.Series):
        return {str(k): to_jsonable(v) for k, v in value.to_dict().items()}
    if isinstance(value, np.ndarray):
        return [to_jsonable(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)


def json_text(value: Any) -> str:
    """将任意对象序列化为格式化的 JSON 字符串。

    等价于 ``json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)``。
    工具函数统一使用此函数返回结果，确保 LLM 能读到中文而非 unicode 转义。

    Args:
        value: 任意 Python 对象。

    Returns:
        缩进 2 空格的 JSON 字符串，中文不转义。
    """
    return json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)

