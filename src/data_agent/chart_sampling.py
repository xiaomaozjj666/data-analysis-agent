"""图表数据抽样：迷你图与大数据 HTML 嵌入共用的降采样工具。

从 ``routers/artifacts.py`` 的迷你图抽样器迁出（它们原本只为缩略图
服务，如今大数据图表的 HTML 嵌入降采样也复用同一套逻辑），本模块只
依赖标准库，供 ``tools/builder`` / ``echarts_engine`` / ``routers``
三层引用而不产生循环导入。

两个使用场景：
- 迷你图（``*_for_thumb``）：产物卡片 209×131 迷你图，上限 2500 点。
- HTML 嵌入（``*_for_embed``）：交互式图表 HTML 的嵌入数据上限
  ``_EMBED_MAX_POINTS``。独立 HTML 仍可离线打开、声明抽样事实；
  完整数据保留在同名 ``.plotly.json`` / ``.echarts.json`` 产物里，
  不丢任何信息。
"""

from __future__ import annotations

import copy
import math
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


def sample_echarts_option_for_embed(
    option: dict[str, Any], max_points: int = _EMBED_MAX_POINTS
) -> tuple[dict[str, Any], int, int]:
    """大数据图表的 HTML 嵌入降采样（等距步进），返回 (抽样副本, 原始点数, 抽样后点数)。

    与迷你图抽样不同，这里必须保持「类目轴 ↔ 系列数据」的对齐：
    - 类目轴（line/area 的 x）：xAxis.data 与对齐系列用同一步长抽样，
      data[i] 仍对应 categories[i]；散点（数值轴 [x, y, ...] 点对）
      逐系列抽样，天然对齐；
    - 柱状图类目数受语义护栏约束（高基数被拒），跟随轴步长即可；
    - markPoint（max/min）/markLine（average）由 ECharts 按抽样后的
      数据现算，无需迁移。

    原始 option 不被修改；点数取「类目轴长度与最长系列长度」的较大者。
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

    before = max(_series_lens(series) or [0])
    axes = sampled.get("xAxis") if isinstance(sampled, dict) else None
    cat_axis = None
    if isinstance(axes, list) and axes and isinstance(axes[0], dict) and axes[0].get("type") == "category":
        cat_axis = axes[0]
    axis_step = 0
    if cat_axis and isinstance(cat_axis.get("data"), list):
        before = max(before, len(cat_axis["data"]))
        if len(cat_axis["data"]) > max_points:
            axis_step = math.ceil(len(cat_axis["data"]) / max_points)
            cat_axis["data"] = cat_axis["data"][::axis_step]

    if isinstance(series, list):
        for item in series:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            if not isinstance(data, list):
                continue
            stype = item.get("type")
            if stype in {"line", "bar"}:
                # 类目轴对齐系列：跟随轴步长（轴未抽样则保持原样）
                if axis_step > 1:
                    item["data"] = data[::axis_step]
            elif stype == "scatter":
                if len(data) > max_points:
                    step = math.ceil(len(data) / max_points)
                    item["data"] = data[::step]
    after = max(_series_lens(series) or [0])
    if cat_axis and isinstance(cat_axis.get("data"), list):
        after = max(after, len(cat_axis["data"]))
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


def _sample_plotly_figure_for_thumb(figure: dict[str, Any], max_points: int = _THUMB_MAX_POINTS) -> None:
    """就地抽样 Plotly figure 的按点数据数组，供迷你图渲染。

    每个 trace 先取所有数组字段的最大长度 n（x/y/z/text/customdata/
    marker.color 等），n 超过上限时统一按同一等距步长抽样——同一下标
    规则保证 x、y、颜色、悬浮文本一致，不会错位。只处理按点图型；
    heatmap（z 矩阵）/pie/sunburst 等非按点图型不动。
    """
    if not isinstance(figure, dict):
        return
    data = figure.get("data")
    if not isinstance(data, list):
        return
    for trace in data:
        if not isinstance(trace, dict) or trace.get("type") not in _PLOTLY_SAMPLE_TRACE_TYPES:
            continue
        n = 0
        for value in trace.values():
            if isinstance(value, list) and len(value) > n:
                n = len(value)
        if n <= max_points:
            continue
        step = math.ceil(n / max_points)

        def _sample(value: Any, _n: int = n, _step: int = step) -> Any:
            # 递归抽样：散点的 marker.color/marker.size 等嵌套按点数组
            # 与 x/y 同一步长，保证颜色/大小/悬浮文本不错位。
            # 默认参数绑定循环变量（ruff B023）。
            if isinstance(value, list):
                return value[::_step] if len(value) == _n else value
            if isinstance(value, dict):
                return {key: _sample(item, _n, _step) for key, item in value.items()}
            return value

        for key, value in list(trace.items()):
            trace[key] = _sample(value)


def sample_plotly_figure_for_embed(
    figure: dict[str, Any], max_points: int = _EMBED_MAX_POINTS
) -> tuple[dict[str, Any], int, int]:
    """大数据图表的 HTML 嵌入降采样，返回 (抽样副本, 原始点数, 抽样后点数)。

    ``figure`` 通常是 ``json.loads(fig.to_json())`` 的产物（含 numpy
    typed-array 编码）。解码会重建全部 dict/list，因此抽样就地修改
    不会影响传入的原始结构——完整数据由调用方保留在 ``.plotly.json``
    产物中。
    """
    decoded = _decode_plotly_typed_arrays(figure)

    def _max_points_per_trace(fig: dict[str, Any]) -> int:
        best = 0
        for trace in fig.get("data") or []:
            if not isinstance(trace, dict) or trace.get("type") not in _PLOTLY_SAMPLE_TRACE_TYPES:
                continue
            n = max((len(value) for value in trace.values() if isinstance(value, list)), default=0)
            best = max(best, n)
        return best

    before = _max_points_per_trace(decoded)
    _sample_plotly_figure_for_thumb(decoded, max_points)
    after = _max_points_per_trace(decoded)
    return decoded, before, after


def sampling_note(engine: str, original: int, embedded: int) -> str:
    """抽样声明文案：写入图表解读区，让抽样事实对用户可见。"""
    if embedded >= original:
        return ""
    return (
        f"\n\n注：数据量较大（{original:,} 行），为让预览秒开，图表按等距抽样至 "
        f"{embedded:,} 点渲染；完整数据保留在同名 {'Plotly' if engine == 'plotly' else 'ECharts'} "
        "JSON 产物中，可随时下载查看。"
    )
