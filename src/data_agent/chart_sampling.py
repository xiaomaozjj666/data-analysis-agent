"""图表数据抽样：迷你图与大数据 HTML 嵌入共用的降采样工具。

从 ``routers/artifacts.py`` 的迷你图抽样器迁出（它们原本只为缩略图
服务，如今大数据图表的 HTML 嵌入降采样也复用同一套逻辑），本模块除
numpy（已是项目依赖，且只在真正降采样时惰性导入）外不引入新依赖，
供 ``tools/builder`` / ``echarts_engine`` / ``routers`` 三层引用而不
产生循环导入。

两个使用场景：
- 迷你图（``*_for_thumb``）：产物卡片 209×131 迷你图，上限 2500 点。
- HTML 嵌入（``*_for_embed``）：交互式图表 HTML 的嵌入数据上限
  ``_EMBED_MAX_POINTS``。独立 HTML 仍可离线打开、声明抽样事实；
  完整数据保留在同名 ``.plotly.json`` / ``.echarts.json`` 产物里，
  不丢任何信息。

降采样方法按图型分流：
- 折线/面积（数值轴）走 ``_lttb_indices`` 的 LTTB 保峰降采样。等距步进
  ``data[::k]`` 会系统性地跳过落在采样格之间的短促尖峰——30 万行里几处
  尖峰渲染成一条平滑带，峰值直接消失，这是"大数据不可读"的最后一块；
- 散点与其它按点图型仍用等距步进，点语义（每个点是一次观测）不变；
  大散点另有服务端密度视图路径，不经过本模块。
"""

from __future__ import annotations

import copy
import math
from collections.abc import Sequence
from typing import Any

#: 迷你图数据上限：产物卡片的缩略图只需要"看得出分布形状"，全量点云
#: 会让 JSON 体积和渲染成本随数据行数线性暴涨（30 万行散点的
#: .echarts.json 实测约 11.7MB、Plotly 约 3.3MB）。等距抽样到该上限，
#: 形状保留且卡片渲染恒定 O(1)。
_THUMB_MAX_POINTS = 2500

#: 交互式图表 HTML 的嵌入数据上限。散点/折线在 5 万点以上：
#: - 视觉上：1400×850 绘图区约 120 万像素，5 万个 3.5px 半透明点已
#:   密度过采样，更多点不增加可读信息；
#: - 成本上：HTML 体积、传输、iframe JSON 解析、tab 内存全部随点数
#:   线性增长（30 万行 ECharts 散点的 HTML 约 12MB）。
#: 完整数据仍写入 .plotly.json / .echarts.json 产物（下载/重建可用）。
_EMBED_MAX_POINTS = 50_000


# === LTTB（Largest-Triangle-Three-Buckets）保峰降采样 ===
#
# 时间预算（本机 3.13.14 / numpy 2.5.3，取 3 次最快值）：
#   30 万点 → 5 万点（单 trace 的嵌入上限，最坏一档）约 0.18s；
#   30 万点 → 5 千点约 0.02s；6 万点 → 2.5 万点约 0.08s。
# 端到端 sample_plotly_figure_for_embed（解码 typed array + 抽样 30 万点）
# 约 0.24s，远低于 1s 预算。
# 之所以能做到：桶边界与桶均值一次性向量化（bincount/searchsorted），
# Python 循环只跑"桶数（≤ threshold）"遍、每遍一次 numpy 切片运算，
# 30 万点全序列不出现逐点循环。

def _lttb_indices(x: Sequence[float], y: Sequence[float], threshold: int) -> list[int]:
    """LTTB 下采样下标：返回**严格递增**的下标列表，长度恰为 ``threshold``。

    首尾点恒定保留；中间把候选点（去掉首尾）均分成 ``threshold-2`` 个桶，
    每桶取"与上一个选中点、下一个桶均值构成的三角形面积最大"的点。
    等距步进 ``data[::k]`` 只保留下标能被 k 整除的点，尖峰若落在两个保留
    点之间就永久消失；LTTB 每个桶都必留一个点（含桶内极值），因此短促
    尖峰、单点毛刺都能活下来——这正是本函数存在的理由。

    退化输入不抛异常：``threshold`` 不小于点数时返回全部下标；``threshold``
    为 1/2 时退化为取首尾；空序列或 ``threshold<=0`` 返回空表；x/y 不等长
    按较短者截断；全 NaN 的 y 把 NaN 面积视作 -1（仅当整桶皆 NaN 时才可能
    被选中）。x 必须可转数值且**不递减**（调用方负责判定，见
    ``_plotly_curve`` / ``_echarts_curve``），本函数不重排点序。
    """
    n = min(len(x), len(y))
    if n == 0 or threshold <= 0:
        return []
    if threshold >= n:
        return list(range(n))
    if threshold <= 2:
        return [0, n - 1][:threshold]

    import numpy as np

    xs = np.asarray(x[:n], dtype=float)
    ys = np.asarray(y[:n], dtype=float)
    every = (n - 2) / (threshold - 2)
    nb = threshold - 2
    # 候选点（下标 1..n-2）的桶号：第 j 个候选点（j = 下标-1）落在
    # floor(j / every) 桶，与参考实现的 [floor(i*every), floor((i+1)*every))
    # 区间完全一致。
    candidates = np.arange(1, n - 1)
    bucket = np.minimum(((candidates - 1) / every).astype(np.intp), nb - 1)
    counts = np.maximum(np.bincount(bucket, minlength=nb), 1)
    avg_x = np.bincount(bucket, weights=xs[1:-1], minlength=nb) / counts
    avg_y = np.bincount(bucket, weights=ys[1:-1], minlength=nb) / counts
    # 每个桶在候选数组里的半开区间，+1 换成原始下标（候选下标 = 桶内偏移 + 1）
    starts = np.searchsorted(bucket, np.arange(nb), side="left") + 1
    ends = np.searchsorted(bucket, np.arange(nb), side="right") + 1

    picked = [0]
    append = picked.append
    # 桶内面积用 numpy 切片算（每桶一次 numpy 运算，不出现逐点循环）；
    # y 无缺失值时整轮跳过 nan_to_num——5 万次桶循环里每次省一遍全桶扫描。
    has_nan = not bool(np.isfinite(ys).all())
    for k in range(nb):
        previous = picked[-1]
        a_x = float(xs[previous])
        a_y = float(ys[previous])
        if k + 1 < nb:
            # 三角形第三点：下一个桶的均值（参考实现口径）
            c_x = float(avg_x[k + 1])
            c_y = float(avg_y[k + 1])
        else:
            c_x = float(xs[n - 1])
            c_y = float(ys[n - 1])
        start = int(starts[k])
        end = int(ends[k])
        if start >= end:
            # threshold < n 时 every > 1，每桶至少 1 个候选，这里只是浮点
            # 兜底：退化为桶起点，绝不抛异常。
            append(min(start, n - 2))
            continue
        seg_x = xs[start:end]
        seg_y = ys[start:end]
        area = (a_x - c_x) * (seg_y - a_y) - (a_x - seg_x) * (c_y - a_y)
        np.abs(area, out=area)
        if has_nan:
            # NaN 候选（y 缺失）不参与竞争：仅当整桶皆 NaN 时才会被选中
            np.nan_to_num(area, copy=False, nan=-1.0)
        append(start + int(np.argmax(area)))
    picked.append(n - 1)
    return [int(index) for index in picked]


def _sample_echarts_option_for_thumb(option: dict[str, Any], max_points: int = _THUMB_MAX_POINTS) -> None:
    """就地抽样 ECharts option 的散点系列，供迷你图渲染。

    只处理 ``type=="scatter"`` 系列：折线/柱状/直方图数据量由类别或
    bin 数决定（天然很小），热力图的格子抽样会产生缺格破图，散点矩阵
    与 3D 散点在生成阶段已采样。等距步进抽样（data[::k]），保留
    整体分布形状与两端特征。
    """
    if not isinstance(option, dict):
        return
    series = option.get("series")
    if not isinstance(series, list):
        return
    for item in series:
        if not isinstance(item, dict) or item.get("type") != "scatter":
            continue
        data = item.get("data")
        if not isinstance(data, list) or len(data) <= max_points:
            continue
        step = math.ceil(len(data) / max_points)
        item["data"] = data[::step]


def _echarts_curve(data: list[Any], n: int) -> tuple[Any, Any] | None:
    """把 ECharts 折线/面积系列数据规整成 (x, y) 数值曲线，供 LTTB 使用。

    支持两种主流写法：``[[x, y, ...], ...]`` 点对，以及 ``[y, ...]``
    （x 取数据下标——ECharts 在数值轴上同样按下标定位）。任一前提不满足
    就返回 ``None``，调用方退回等距步进：

    - 元素非数值（时间字符串、``"-"`` 缺失标记等）→ numpy 转换报错；
    - 点对维数不足 2、长度与 ``n`` 不符、嵌套不规则；
    - x 含 NaN/Inf 或**不递减**（乱序 x 会让"相邻桶"假设失效）。

    全程 numpy 向量化，30 万点不出现 Python 级逐点循环。
    """
    import numpy as np

    if not data:
        return None
    try:
        arr = np.asarray(data, dtype=float)
    except (TypeError, ValueError):
        return None
    if arr.ndim == 2:
        if arr.shape[0] != n or arr.shape[1] < 2:
            return None
        xs, ys = arr[:, 0], arr[:, 1]
    elif arr.ndim == 1:
        if arr.shape[0] != n:
            return None
        xs, ys = np.arange(n, dtype=float), arr
    else:
        return None
    if not np.all(np.isfinite(xs)):
        return None
    if n > 1 and bool(np.any(xs[1:] < xs[:-1])):
        return None
    return xs, ys


def _series_y_values(item: dict[str, Any], n: int) -> Any:
    """取一个类目轴系列在轴上的 y 数值数组（长度 n），取不到时返回 ``None``。

    类目轴上的数据有两种写法：``[y, ...]`` 与 ``[[类别, y], ...]``；后者
    第二列才是数值。非数值（时间字符串、``"-"``）一律返回 ``None``。
    """
    import numpy as np

    data = item.get("data")
    if not isinstance(data, list) or len(data) != n or n == 0:
        return None
    try:
        arr = np.asarray(data, dtype=float)
    except (TypeError, ValueError):
        return None
    if arr.ndim == 2:
        if arr.shape[1] < 2:
            return None
        arr = arr[:, 1]
    elif arr.ndim != 1:
        return None
    return arr


def _category_envelope(line_items: list[dict[str, Any]], n: int) -> Any:
    """类目轴上多个折线系列的"上包络"（逐下标取最大值），供 LTTB 选点。

    多系列共享同一组 LTTB 下标（否则 xAxis.data 与各系列会错位），因此选点
    依据必须同时照顾所有系列：用逐点最大值当曲线，任一系列自己的尖峰都会
    出现在包络上，不会被等距步进抹掉。
    """
    import numpy as np

    envelope = None
    for item in line_items:
        values = _series_y_values(item, n)
        if values is None:
            return None
        finite = np.where(np.isfinite(values), values, -np.inf)
        envelope = finite if envelope is None else np.maximum(envelope, finite)
    if envelope is None:
        return None
    return np.where(np.isfinite(envelope), envelope, 0.0)


def sample_echarts_option_for_embed(
    option: dict[str, Any], max_points: int = _EMBED_MAX_POINTS
) -> tuple[dict[str, Any], int, int]:
    """大数据图表的 HTML 嵌入降采样，返回 (抽样副本, 原始行数, 抽样后行数)。

    与迷你图抽样不同，这里必须保持「类目轴 ↔ 系列数据」的对齐：
    - 类目轴（line/area 的 x）：xAxis.data 与对齐系列用同一步长抽样，
      data[i] 仍对应 categories[i]；
    - 数值轴折线/面积：按 (x, y) 走 LTTB 保峰降采样（等距步进会把落在
      采样格之间的短促尖峰整段跳过），点对写法下 x/y 同行取用，天然对齐；
    - 数值轴散点（[x, y, ...] 点对）：继续逐系列等距抽样点对，点语义
      不变——散点的每个点是一次观测，换点等于改数据；
    - 以上路径的总预算都按系列数分摊（多系列各自独立套上限会突破承诺
      总量），下限 2000 点保形状；
    - markPoint（max/min）/markLine（average）由 ECharts 按抽样后的
      数据现算，无需迁移。

    行数口径：类目轴图取轴长度，数值轴逐点系列取各系列点数之和。原始
    option 不被修改。
    """
    sampled = copy.deepcopy(option)
    series = sampled.get("series") if isinstance(sampled, dict) else None

    def _series_lens(items: Any) -> list[int]:
        if not isinstance(items, list):
            return []
        return [
            len(item["data"])
            for item in items
            if isinstance(item, dict) and isinstance(item.get("data"), list)
        ]

    axes = sampled.get("xAxis") if isinstance(sampled, dict) else None
    cat_axis = None
    if isinstance(axes, list) and axes and isinstance(axes[0], dict) and axes[0].get("type") == "category":
        cat_axis = axes[0]
    axis_step = 0
    axis_before = 0
    cat_indices: list[int] | None = None
    if cat_axis and isinstance(cat_axis.get("data"), list):
        axis_before = len(cat_axis["data"])
        if axis_before > max_points:
            # 纯折线/面积的类目轴也走 LTTB：ECharts 的折线图（含本项目的
            # "月度趋势"）一律用具名类目轴，若这类图仍用等距步进，界面上
            # 的尖峰照样会被抹掉——这正是"包络 + 同下标裁剪"要解决的问题。
            # 只要轴上还挂着柱状/散点等"每个类目一个观测"的系列，就必须
            # 退回等距步进（抽掉一个类目等于抽掉一根柱子）。
            series_items = [item for item in (series or []) if isinstance(item, dict)]
            line_items = [item for item in series_items
                          if item.get("type") in {"line", "area", None}
                          and isinstance(item.get("data"), list)]
            others = [item for item in series_items
                      if item.get("type") not in {"line", "area", None}]
            if line_items and not others:
                envelope = _category_envelope(line_items, axis_before)
                if envelope is not None:
                    cat_indices = _lttb_indices(
                        list(range(axis_before)), envelope, max_points
                    )
                    cat_axis["data"] = [cat_axis["data"][index] for index in cat_indices]
            if cat_indices is None:
                axis_step = math.ceil(axis_before / max_points)
                cat_axis["data"] = cat_axis["data"][::axis_step]

    def _value_axis(index: Any) -> bool:
        """系列绑定的 xAxis 是否为数值轴。

        ECharts 的 xAxis 默认是类目轴，未显式声明 type 时按类目处理，
        因此只认 value/log——否则会把"每格一个类别"的数据当成曲线重排。
        """
        if not isinstance(axes, list) or not isinstance(index, int):
            return False
        if index < 0 or index >= len(axes) or not isinstance(axes[index], dict):
            return False
        return axes[index].get("type") in {"value", "log"}

    before = axis_before
    line_items: list[dict[str, Any]] = []
    if isinstance(series, list):
        for item in series:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            if not isinstance(data, list):
                continue
            stype = item.get("type")
            if stype in {"line", "bar"} and cat_axis is not None:
                # 类目轴对齐系列：LTTB 下标（纯折线）或等距步长（其余），
                # 两者都必须与 xAxis.data 用同一套下标，否则 data[i] 会
                # 对应到别的类目上。
                if cat_indices is not None:
                    item["data"] = [data[index] for index in cat_indices if index < len(data)]
                elif axis_step > 1:
                    item["data"] = data[::axis_step]
            elif stype in {"line", "area"} and _value_axis(item.get("xAxisIndex", 0)):
                line_items.append(item)  # 数值轴折线/面积：下方按 LTTB 保峰
            # 散点系列在下方按总预算分摊抽样；其余类型不动

    scatter_items = [
        item
        for item in (series if isinstance(series, list) else [])
        if isinstance(item, dict) and item.get("type") == "scatter" and isinstance(item.get("data"), list)
    ]
    line_before = sum(len(item["data"]) for item in line_items)
    scatter_before = sum(len(item["data"]) for item in scatter_items)
    if line_items and not cat_axis:
        before = line_before
        if line_before > max_points:
            per_cap = max(2000, max_points // len(line_items))
            for item in line_items:
                data = item["data"]
                if len(data) <= per_cap:
                    continue
                curve = _echarts_curve(data, len(data))
                if curve is None:
                    # 非数值/乱序 x（时间字符串、缺失标记等）：退回等距步进
                    item["data"] = data[:: math.ceil(len(data) / per_cap)]
                else:
                    indices = _lttb_indices(curve[0], curve[1], per_cap)
                    item["data"] = [data[index] for index in indices]
    if scatter_items and not cat_axis:
        before = scatter_before + line_before
        if scatter_before > max_points:
            per_cap = max(2000, max_points // len(scatter_items))
            for item in scatter_items:
                data = item["data"]
                if len(data) > per_cap:
                    item["data"] = data[:: math.ceil(len(data) / per_cap)]

    after = len(cat_axis["data"]) if cat_axis and isinstance(cat_axis.get("data"), list) else 0
    if (line_items or scatter_items) and not cat_axis:
        after = sum(len(item["data"]) for item in [*line_items, *scatter_items])
    if before == 0:
        before = max(_series_lens(series) or [0])
        after = max(_series_lens(series) or [0])
    return sampled, before, after


#: 按点一维数组的图型（轨迹数据是"每行一个点"的长数组）；矩阵型
#: （heatmap 的 z 是二维网格）与类别型（bar 的 x/y 是类别维度）排除。
_PLOTLY_SAMPLE_TRACE_TYPES = {
    "scatter", "scattergl", "scatter3d", "scatterternary", "scatterpolar",
    "scatterpolargl", "line", "box", "violin",
}


def _decode_plotly_typed_arrays(value: Any) -> Any:
    """递归解码 Plotly typed-array 序列化，返回普通 Python list。

    plotly.py 的 ``fig.to_json()`` 默认把 numpy 数组压缩成
    ``{"dtype": "f4", "bdata": "<base64>"}``（30 万行散点的 x/y 都是
    这种形式，直接当 dict 处理无法统计/抽样）。解码后用标准 JSON
    数组返回，等距抽样与 json.dumps 都按普通列表工作；
    plotly.js 对普通数组同样支持。
    """
    if isinstance(value, dict):
        dtype = value.get("dtype")
        bdata = value.get("bdata")
        if (
            isinstance(dtype, str)
            and isinstance(bdata, str)
            and set(value) == {"dtype", "bdata"}
            and dtype != "object"
        ):
            try:
                import base64

                import numpy as np

                return np.frombuffer(base64.b64decode(bdata), dtype=np.dtype(dtype)).tolist()
            except Exception:
                return value
        return {key: _decode_plotly_typed_arrays(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_plotly_typed_arrays(item) for item in value]
    return value


def _trace_point_count(trace: dict[str, Any]) -> int:
    """trace 的点数：所有数组字段（x/y/z/text/customdata/marker.color…）的最大长度。"""
    return max((len(value) for value in trace.values() if isinstance(value, list)), default=0)


def _resample_trace_arrays(
    trace: dict[str, Any], n: int, *, indices: Sequence[int] | None = None, step: int = 1
) -> None:
    """就地重采样 trace 的全部"按点数组"，``indices`` 与 ``step`` 二选一。

    凡是长度等于 ``n`` 的数组（x/y/text/marker.color/customdata 等）都用
    同一套下标，保证坐标、颜色、悬浮文本不错位；长度不等于 ``n`` 的字段
    （图例文本、布局类数组）原样保留。

    - ``indices``：LTTB 下标（升序），用于折线/面积保峰；
    - ``step``：等距步长 ``value[::step]``，其余图型沿用。
    """
    def _pick(value: Any, _n: int = n, _idx: Any = indices, _step: int = step) -> Any:
        # 递归抽样：散点的 marker.color/marker.size 等嵌套按点数组与 x/y
        # 同一套下标。默认参数绑定外层变量（ruff B023）。
        if isinstance(value, list):
            if len(value) != _n:
                return value
            return value[::_step] if _idx is None else [value[index] for index in _idx]
        if isinstance(value, dict):
            return {key: _pick(item) for key, item in value.items()}
        return value

    for key, value in list(trace.items()):
        trace[key] = _pick(value)


def _sample_plotly_figure_for_thumb(figure: dict[str, Any], max_points: int = _THUMB_MAX_POINTS) -> None:
    """就地抽样 Plotly figure 的按点数据数组，供迷你图渲染。

    每个 trace 先取所有数组字段的最大长度 n（x/y/z/text/customdata/
    marker.color 等），n 超过上限时统一按同一等距步长抽样——同一下标
    规则保证 x、y、颜色、悬浮文本一致，不会错位。只处理按点图型；
    heatmap（z 矩阵）/pie/sunburst 等非按点图型不动。

    迷你图只需"看得出分布形状"，因此不用 LTTB（保峰是嵌入图的可读性
    需求，缩略图尺度的峰谷本就不可辨）。
    """
    if not isinstance(figure, dict):
        return
    data = figure.get("data")
    if not isinstance(data, list):
        return
    for trace in data:
        if not isinstance(trace, dict) or trace.get("type") not in _PLOTLY_SAMPLE_TRACE_TYPES:
            continue
        n = _trace_point_count(trace)
        if n <= max_points:
            continue
        _resample_trace_arrays(trace, n, step=math.ceil(n / max_points))


def _datetime_to_epoch(values: list[Any]) -> Any:
    """把时间字符串数组转成 epoch 秒（无法解析时返回 ``None``）。

    时间轴的大折线图（按分钟/秒的时序数据）此前因为 x 是字符串而退回等距
    步进，尖峰一样会被抹掉。这里用 pandas 的向量化解析（30 万条约 0.1s），
    解析失败或结果不有限就返回 ``None`` 让调用方退回等距——不猜语义。
    """
    import numpy as np

    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas 是核心依赖，缺了其它功能也不可用
        return None
    try:
        parsed = pd.to_datetime(pd.Series(values), errors="coerce", format="mixed")
    except (TypeError, ValueError):  # pragma: no cover - 老版本 pandas 无 format="mixed"
        return None
    if parsed.isna().any():
        return None
    try:
        epoch = parsed.astype("int64").to_numpy(dtype=float) / 1e9
    except (TypeError, ValueError):  # pragma: no cover - 混合时区（tz-aware 与 naive 混用）
        try:
            epoch = parsed.dt.tz_convert("UTC").astype("int64").to_numpy(dtype=float) / 1e9
        except (TypeError, ValueError, AttributeError):  # pragma: no cover - 无法归一化
            return None
    if not np.all(np.isfinite(epoch)):
        return None
    return epoch


def _plotly_curve(trace: dict[str, Any], n: int) -> tuple[Any, Any] | None:
    """取 trace 的 (x, y) 数值曲线，供 LTTB 使用；不满足前提时返回 ``None``。

    LTTB 要求一条"按 x 有序的数值曲线"，任一条不满足就退回等距步进：

    - 缺 x 或 y，或长度与 trace 点数不符（同一 trace 的各数组必须等长，
      否则下标对不上）；
    - y 非数值（``"-"`` 缺失标记、混合类型）→ 保持原有等距行为；
    - x 是时间字符串时**先解析成 epoch 秒**（时间轴的大折线图同样需要保峰），
      解析不出来才退回等距；其余字符串（类目名）照旧退回；
    - x 含 NaN/Inf 或**不递减**（乱序 x 的"下一个桶"不再是右侧邻域，
      LTTB 的几何假设失效）。

    x/y 是两个独立数组时（如图上叠加的参考线）长度也可能不同，因此
    必须逐条核对而不是只看最大长度。
    """
    import numpy as np

    x = trace.get("x")
    y = trace.get("y")
    if not isinstance(x, list) or not isinstance(y, list):
        return None
    if len(x) != n or len(y) != n:
        return None

    def _as_numeric(values: list[Any]) -> Any:
        try:
            array = np.asarray(values, dtype=float)
        except (TypeError, ValueError):
            return None
        return array if array.ndim == 1 else None

    xs = _as_numeric(x)
    if xs is None:
        xs = _datetime_to_epoch(x)
        if xs is None:
            return None
    ys = _as_numeric(y)
    if ys is None:
        return None
    if not np.all(np.isfinite(xs)):
        return None
    if n > 1 and bool(np.any(xs[1:] < xs[:-1])):
        return None
    return xs, ys


def _plotly_draws_lines(trace: dict[str, Any]) -> bool:
    """trace 是否画线（折线/面积）——只有画线的 trace 才能用 LTTB 换点。

    - 显式 ``mode`` 含 ``"lines"``（``px.line`` 产出 ``lines+markers``）→ 画线；
    - 未声明 mode：plotly.js 对 scatter/scattergl 的默认 mode 是
      ``lines+markers``，即默认画线；
    - 纯 markers（``px.scatter`` 产出 ``mode="markers"``）→ 不画线，点的
      集合语义不能换（大散点已由服务端密度视图接管，见
      ``sample_plotly_figure_for_embed`` 的调用方）。
    """
    ttype = trace.get("type")
    if ttype == "line":
        return True
    if ttype not in {"scatter", "scattergl"}:
        return False
    mode = trace.get("mode")
    if isinstance(mode, str):
        return "lines" in mode
    return True


def _sample_trace_for_embed(trace: dict[str, Any], max_points: int) -> None:
    """单 trace 的嵌入降采样：折线/面积优先 LTTB 保峰，其余等距步进。

    两条路径都通过 ``_resample_trace_arrays`` 落地，因此"哪些数组属于
    同一个点"的对应关系在两种抽样下都成立（颜色/悬浮文本不会错位）。
    """
    n = _trace_point_count(trace)
    if n <= max_points:
        return
    indices = None
    if _plotly_draws_lines(trace):
        curve = _plotly_curve(trace, n)
        if curve is not None:
            indices = _lttb_indices(curve[0], curve[1], max_points)
    if indices is None:
        _resample_trace_arrays(trace, n, step=math.ceil(n / max_points))
    else:
        _resample_trace_arrays(trace, n, indices=indices)


def sample_plotly_figure_for_embed(
    figure: dict[str, Any], max_points: int = _EMBED_MAX_POINTS
) -> tuple[dict[str, Any], int, int]:
    """大数据图表的 HTML 嵌入降采样，返回 (抽样副本, 原始总点数, 抽样后总点数)。

    ``figure`` 通常是 ``json.loads(fig.to_json())`` 的产物（含 numpy
    typed-array 编码）。解码会重建全部 dict/list，因此抽样就地修改
    不会影响传入的原始结构——完整数据由调用方保留在 ``.plotly.json``
    产物中。

    预算按 trace 总量分摊：多系列（如自动配色的 3~8 个分组）时若各自
    独立套用上限，总点数会突破承诺（3 系列 × 5 万 = 15 万），这里先数
    出按点图型的 trace 数，把总预算均分到每个 trace（下限 2000 保形状）。

    抽样方法按 trace 分流（见 ``_sample_trace_for_embed``）：折线/面积
    用 LTTB 保峰，散点等其余图型沿用等距步进。
    """
    decoded = _decode_plotly_typed_arrays(figure)

    traces = [
        trace
        for trace in (decoded.get("data") or [])
        if isinstance(trace, dict) and trace.get("type") in _PLOTLY_SAMPLE_TRACE_TYPES
    ]
    total_before = sum(_trace_point_count(trace) for trace in traces)
    if traces and total_before > max_points:
        per_trace_cap = max(2000, max_points // len(traces))
        for trace in traces:
            _sample_trace_for_embed(trace, per_trace_cap)
    after = sum(_trace_point_count(trace) for trace in traces)
    return decoded, total_before, after


def sampling_note(engine: str, original: int, embedded: int) -> str:
    """抽样声明文案：写入图表解读区，让抽样事实对用户可见。

    文案按"抽样规则"而不是"本次实际走了哪条分支"描述（本函数拿不到
    分支信息）：散点是等距抽样，折线/面积按 LTTB 保峰（数值轴与类目轴
    都是——类目轴的折线共享一组 LTTB 下标同时裁轴与系列）——两种说法在
    大图上都成立，不会对用户谎报。
    """
    if embedded >= original:
        return ""
    return (
        f"\n\n注：数据量较大（{original:,} 行），为让预览秒开，图表降采样至 "
        f"{embedded:,} 点渲染（散点等距抽样；折线/面积按 LTTB 保峰）；"
        f"完整数据保留在同名 {'Plotly' if engine == 'plotly' else 'ECharts'} "
        "JSON 产物中，可随时下载查看。"
    )
