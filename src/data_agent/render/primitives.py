"""ECharts 渲染中性层：颜色/数值/分箱/坐标轴等与具体图型无关的纯函数。

从 ``echarts_engine.py`` 拆出。本模块只依赖常量与 ``tools/_helpers`` 这类
无业务依赖的叶子模块，任何图表族模块都可以引用它，不会造成循环导入。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from data_agent.echarts_constants import (
    _ECHARTS_AXIS_LABEL_FORMATTER_JS,
    _ECHARTS_BASE_AXIS,
    _ECHARTS_PALETTE,
    _ECHARTS_X_AXIS_NAME,
    _ECHARTS_Y_AXIS_NAME,
    _TIME_LABEL_PATTERN,
)
from data_agent.tools._helpers import _nice_axis_formatter, _nice_ticks


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """将 #RRGGBB 转为 rgba(r,g,b,a)，用于面积填充透明度控制。"""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _build_linear_gradient(color: str, vertical: bool = True) -> dict[str, Any]:
    """生成柔和的垂直/水平线性渐变，用于面积图、柱状图填充。"""
    coords = (0, 0, 0, 1) if vertical else (0, 0, 1, 0)
    return {
        "type": "linear",
        "x": coords[0],
        "y": coords[1],
        "x2": coords[2],
        "y2": coords[3],
        "colorStops": [
            {"offset": 0, "color": _hex_to_rgba(color, 0.55)},
            {"offset": 1, "color": _hex_to_rgba(color, 0.08)},
        ],
    }


def _safe_value(value: Any) -> Any:
    """把 numpy/pandas 标量转为 JSON 可序列化的 Python 原生类型。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _rows_data(df: pd.DataFrame, cols: list[str]) -> list[list[Any]]:
    """按列抽取构建 series data：itertuples 比 iterrows 快一个数量级，
    大数据散点图的生成耗时从秒级降到毫秒级。"""
    return [[_safe_value(v) for v in row] for row in df[cols].itertuples(index=False, name=None)]


def _format_number(value: Any, *, precision: int = 2) -> str:
    """数字格式化：整数加千分位，浮点保留 precision 位，None 显示 —。"""
    if value is None:
        return "—"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(f):
        return "—"
    if abs(f) >= 10000:
        return f"{f:,.0f}"
    return f"{f:,.{precision}f}"


def _series_color(index: int) -> str:
    """按索引取主色板颜色，超出循环复用。"""
    return _ECHARTS_PALETTE[index % len(_ECHARTS_PALETTE)]


def _stacked_totals(
    df: pd.DataFrame,
    *,
    x: str,
    y: str | None,
    color: str | None,
) -> list[float] | None:
    """按 x 汇总各 color 分组的 y 之和，供堆叠柱/堆叠面积的轴范围使用。"""
    if not (x and y and color) or x not in df.columns or y not in df.columns:
        return None
    frame = df[[x, y]].copy()
    frame[y] = pd.to_numeric(frame[y], errors="coerce")
    totals = frame.dropna().groupby(x, dropna=False)[y].sum()
    return [float(value) for value in totals.tolist()]


def _echarts_value_axis(
    df: pd.DataFrame,
    y: str | None,
    *,
    name: str,
    scale: bool = True,
    values: list[float] | None = None,
    zero_base: bool = False,
    is_x: bool = False,
) -> dict[str, Any]:
    """构建数值型 yAxis：应用 nice ticks 对齐刻度到 1/2/5/10 倍数，
    并用大数值自适应 formatter（万/亿）让坐标轴可读。

    values 可覆盖取值来源：堆叠图的轴范围必须按各类目堆叠总和计算，
    而非单列 min/max，否则堆叠后的线/面会冲出绘图区顶部。

    ``zero_base=True`` 强制包含 0：柱状图靠**长度**表达数值，截断基线会
    把 1%~2% 的差异放大成肉眼上的成倍差距（实测：三地区销售额 66~68 万，
    轴从 21 万起，看起来差了三四倍）。折线图不受此限——折线的斜率才是
    信息，截断基线是常规做法。
    """
    base: dict[str, Any] = {
        **_ECHARTS_BASE_AXIS,
        "type": "value",
        "name": name,
        "scale": scale,
        # 轴名位置分方向：Y 轴放顶端偏左（与刻度同一视觉列），X 轴居中放轴下方
        # ——数值轴当 X 用时若沿用 Y 的 "end" 定位，轴名会贴到绘图区最右侧被裁掉
        # （实测散点图右边缘只剩"销售"两个字）。
        **(_ECHARTS_X_AXIS_NAME if is_x else _ECHARTS_Y_AXIS_NAME),
    }
    numeric = pd.Series(dtype=float)
    if values is not None:
        numeric = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    elif y and y in df.columns:
        numeric = pd.to_numeric(df[y], errors="coerce").dropna()
    if len(numeric) > 0:
        vmin, vmax = float(numeric.min()), float(numeric.max())
        if zero_base and vmin > 0:
            # 从 0 起算 nice 刻度（含全负数据时保留负向范围）
            nice_min, nice_max, _ = _nice_ticks(min(0.0, vmin), vmax, n=5)
            nice_min = min(0.0, nice_min)
        else:
            nice_min, nice_max, _ = _nice_ticks(vmin, vmax, n=5)
        base["min"] = nice_min
        base["max"] = nice_max
        # 不固定 interval：dataZoom/滚轮缩放后 ECharts 会按新范围自动
        # 重算圆数刻度；固定步长会让放大后的坐标轴只剩零星刻度甚至
        # 一个都没有，无法读取数值做比较。min/max 仍保留初始 nice 范围。
        # axisLabel formatter 用 JS 函数字符串注入 _nice_axis_formatter 逻辑。
        # ECharts option 是 JSON，但 formatter 字段支持函数字符串（前端 eval）。
        base["axisLabel"] = {
            **_ECHARTS_BASE_AXIS["axisLabel"],
            "formatter": _ECHARTS_AXIS_LABEL_FORMATTER_JS,
        }
        return base
    # 无数据或非数值：仅加 formatter 以备数据更新
    base["axisLabel"] = {
        **_ECHARTS_BASE_AXIS["axisLabel"],
        "formatter": _ECHARTS_AXIS_LABEL_FORMATTER_JS,
    }
    return base


def _format_time_categories(values: pd.Series) -> list[str] | None:
    """x 为时间列时按数据粒度智能格式化类目标签，非时间列返回 None。

    对齐主流时间轴做法（ECharts time 轴 / Tableau 日期轴）：标签只保留
    有效粒度——全为月初的月度数据显示 "YYYY-MM"，整日数据显示
    "YYYY-MM-DD"，带时间才显示到时分，避免 str(Timestamp) 产生的
    "2026-01-01 00:00:00" 冗余后缀占满轴线。

    仅改变展示标签，不影响多系列对齐（reindex 用原始 x 值）。
    """
    if pd.api.types.is_datetime64_any_dtype(values):
        ts = pd.Series(values).reset_index(drop=True)
    elif pd.api.types.is_object_dtype(values) or pd.api.types.is_string_dtype(values):
        # pandas 3.0 起纯字符串列默认为 str dtype（非 object），两种都要识别
        sample = [str(v) for v in values.dropna().head(20).tolist()]
        if not sample or not all(_TIME_LABEL_PATTERN.match(v) for v in sample):
            return None
        ts = pd.to_datetime(values, errors="coerce").reset_index(drop=True)
        # 抽样通过但全量解析失败（混入非日期值）时保持原样，不强行格式化
        if bool((ts.isna() & pd.Series(values).reset_index(drop=True).notna()).any()):
            return None
    else:
        return None
    valid = ts.dropna()
    if valid.empty:
        return None
    midnight = bool((valid.dt.normalize() == valid).all())
    if midnight and bool((valid.dt.day == 1).all()):
        fmt = "%Y-%m"
    elif midnight:
        fmt = "%Y-%m-%d"
    elif bool((valid.dt.second == 0).all()):
        fmt = "%Y-%m-%d %H:%M"
    else:
        fmt = "%Y-%m-%d %H:%M:%S"
    return ["" if pd.isna(v) else v.strftime(fmt) for v in ts]


def _round_significant(values: np.ndarray, sig: int = 6) -> np.ndarray:
    """按有效位数取整（压缩 hover 数据里的分箱边界）。

    格子数据要带 x0/x1/y0/y1 四个边界（ECharts 的 heatmap 拿不到分箱区间，
    必须随数据项带过去），6 位有效数字足以还原区间，却能把 JSON 体积压掉
    三成——一万多个格子的 option 体积是这次改造的核心指标之一。
    """
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return array
    with np.errstate(divide="ignore", invalid="ignore"):
        exponent = np.floor(np.log10(np.abs(array)))
        factor = np.power(10.0, sig - 1 - exponent)
        rounded = np.round(array * factor) / factor
    return np.where(np.isfinite(rounded), rounded, array)


def _snap_edges(edges: np.ndarray) -> np.ndarray:
    """把浮点噪声级的分箱边界归零。

    ``np.linspace`` 造出来的边界在跨零点时会留下 -100 + k×5.97… 的残差
    （实测 -2.00e-07），轴标签与悬浮提示会显示成 "-2.00e-07" 这种刺眼的值。
    判定阈值取格宽的百万分之一：真实的非零边界远大于它，只有数值噪声会被
    归零（窄区间数据的小数值不受影响）。
    """
    values = np.asarray(edges, dtype=float)
    if values.size < 2:
        return values
    width = float(np.median(np.abs(np.diff(values))))
    if not math.isfinite(width) or width <= 0:
        return values
    return np.where(np.abs(values) < width * 1e-6, 0.0, values)


def _bin_labels(edges: np.ndarray) -> list[str]:
    """分箱边界 → 类目轴短标签（"1.2万" / "1,200"）。

    ECharts 的 heatmap 两个维度都必须是类目轴，连续数值必须先在 Python 侧
    格式化成短标签；这里复用 ``_nice_axis_formatter``——它是本文件数值轴 JS
    formatter 的 Python 孪生实现，保证密度图的类目标签与其它图的数值刻度
    同一口径（万/亿优先）。154 个格子挨着排时紧凑格式可能撞车（100.0 与
    100.6 都写成 "101"），检测到大量重复就按格宽补足小数位。
    """
    values = _snap_edges(np.asarray(edges, dtype=float))
    if values.size < 2:
        return []
    labels = [_nice_axis_formatter(float(value)) for value in values[:-1]]
    if len(set(labels)) >= max(2, int(len(labels) * 0.6)):
        return labels
    widths = np.abs(np.diff(values))
    width = float(np.median(widths)) if widths.size else 0.0
    if not math.isfinite(width) or width <= 0:
        return labels
    digits = max(0, min(6, int(math.ceil(-math.log10(width))) + 1))
    precise = [f"{float(value):,.{digits}f}" for value in values[:-1]]
    return precise if len(set(precise)) > len(set(labels)) else labels


def _bin_index(value: float, edges: np.ndarray, *, nearest: bool = False) -> int:
    """数值 → 类目下标（默认取所属格子，``nearest=True`` 取最近的格子中心）。

    ECharts 的类目轴只接受整数下标：实测 ``convertToPixel(3.5)`` 与 ``(4)``
    返回同一像素，小数坐标会被取整。因此密度图上一切"按数值定位"的东西
    （最外围记录、趋势线、均值参考线）都必须先映射成格子下标，单点定位
    精度是半格（约 3px），在 1080px 宽的面板上与原始坐标不可区分。
    """
    if edges.size < 2 or not math.isfinite(value):
        return 0
    low, high = float(edges[0]), float(edges[-1])
    width = (high - low) / (edges.size - 1)
    if width <= 0:
        return 0
    position = (value - low) / width
    index = int(round(position - 0.5)) if nearest else int(math.floor(position))
    return max(0, min(edges.size - 2, index))


def _within_range(value: Any, bounds: tuple[float, float]) -> bool:
    """数值是否落在可视范围内（用于决定"这条参考线画不画"）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and bounds[0] <= number <= bounds[1]


def _axis_label_interval(count: int, target: int) -> int:
    """类目轴刻度抽稀步长（ECharts 的 interval 是"每隔 N 个类目显示一个"）。

    154×80 的网格逐个显示标签必然糊成一团：主面板留 ~14 个、分面留 ~6 个，
    精确区间由悬浮提示给出。
    """
    if count <= target:
        return 0
    return max(0, int(math.ceil(count / target)) - 1)
