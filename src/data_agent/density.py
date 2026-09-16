"""大数据点云密度聚合：把几十万散点聚合成可读的密度网格（双引擎共享）。

为什么需要它
------------
"每点一个标记"的散点渲染在 10 万行以上会彻底失效：预览绘图区约
1080×560 px，3.5px 半透明标记只够铺约 2 万个点，再多就是同一像素上
几十个点叠加——alpha 混合迅速饱和成一团均匀色块：品类色互相覆盖成
灰、密度差异被抹平、聚集与离群全部看不出来（用户实测反馈"所有的点
和数据都堆在一起，根本看不出来什么"）。

业界做法一致（datashader 的像素级光栅化 + 计数着色、2D 直方图 /
hexbin 密度图、seaborn ``jointplot(kind="hex")``）：**把点聚合到网格，
用颜色表达每格的记录数**，再补上边缘分布（marginal）与结构锚点。这样
"哪里挤、哪里空、几组之间分布是否不同"才读得出来，而且网格只有几千
个格子——HTML 体积不再随行数线性膨胀（30 万行散点 HTML 从 12MB 降到
几百 KB）。

两个刻意的设计选择
------------------
1. **档位化（classed）而不是连续渐变**：记录数天然跨数量级（密集格上
   千、边缘格只有一两条），线性映射会把非密集区全压成一个颜色。这里
   用 1-2-5 系列切成 5~7 档（等同 choropleth 的分级图例、datashader 的
   ``eq_hist`` 思路），图例直接标出每档的记录数区间——用户不需要理解
   "对数色标"就能读懂深浅。
2. **零记录格透明**（datashader 同样规定 0 值必须全透明）：点云的实际
   覆盖范围才是信息，背景不能参与表达。低档位仍用"可见但很浅"的颜色
   （对应 datashader 的 ``min_alpha``），避免稀疏区整片消失。

本模块只做纯 numpy 聚合与文案（除 numpy 外零依赖），Plotly 与 ECharts
两个引擎渲染同一份网格与同一套档位，保证双引擎视觉一致。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: 触发密度视图的行数阈值。绘图区约 1080×560 px，3.5px 标记的覆盖面积
#: 约 38 px²，即"每像素一个点"的饱和点约 600k/38 ≈ 1.6 万行；取 2 万行
#: 作为切换点：低于此值原始点仍能看出结构（WebGL 渲染，Plotly 官方称
#: scattergl 可到百万级），高于此值密度视图的信息量严格更多。
DENSITY_MIN_POINTS = 20_000

#: 网格单格的目标屏幕尺寸（px）。datashader 的默认网格是"一格一像素"，
#: 但那会丢掉格子边界、且稀疏格细到看不见；7px 既保留结构细节，又等价
#: 于 datashader ``dynspread`` 的"让稀疏区域可见"效果。
BIN_TARGET_PX = 7.0

#: 单轴网格数上限：200×200 = 4 万格已是 JSON 体积与渲染成本上限。
MAX_BINS_PER_AXIS = 200

#: 分面数量上限（按记录数取前 N 类，其余合并为一个"其他"面板）。
DENSITY_MAX_PANELS = 6

#: 档位边界候选（1-2-5 系列）：记录数分档只用这些圆整值，
#: 图例上"1 / 2–4 / 5–9 / 10–24 / ≥25"比"1.7~3.2"可读得多。
_BAND_EDGE_STEPS: tuple[int, ...] = (
    1, 2, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000,
    10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000,
)

#: 档位默认数量上限（含最高一档"≥X"）。
DEFAULT_MAX_BANDS = 7

#: 密度色板：由浅到深（浅色主题）/ 由暗到亮（暗色主题），每档一个颜色。
#: 单一色相渐进、亮度单调变化——保证"深浅"只表达记录数一个维度，
#: 不会被读成数值大小或类别（matplotlib 关于感知均匀色板的建议）。
DENSITY_BAND_COLORS_LIGHT: tuple[str, ...] = (
    "#DCE8F2",
    "#BDD5E8",
    "#9BBEDC",
    "#78A5CE",
    "#4E79A7",
    "#35618A",
    "#1E4468",
)
DENSITY_BAND_COLORS_DARK: tuple[str, ...] = (
    "#2C3B4D",
    "#34536D",
    "#3D6E8E",
    "#488CAE",
    "#5AABC1",
    "#7FC9C2",
    "#B9E0AE",
)


def should_use_density(row_count: int) -> bool:
    """行数是否达到密度视图阈值。"""
    return row_count >= DENSITY_MIN_POINTS


def bin_shape(panel_width: float = 1080.0, panel_height: float = 560.0,
              target_px: float = BIN_TARGET_PX) -> tuple[int, int]:
    """按面板像素尺寸算网格数：(nx, ny)。

    网格数跟着面板大小走，保证每个格子在任何布局下都接近正方形
    （分面后单格面板变窄，网格同步变少），既不会出现拉伸的"长条格"，
    也不会在大面板上糊成马赛克。
    """
    nx = int(round(panel_width / target_px))
    ny = int(round(panel_height / target_px))
    nx = max(16, min(MAX_BINS_PER_AXIS, nx))
    ny = max(12, min(MAX_BINS_PER_AXIS, ny))
    return nx, ny


def density_bands(max_count: int, *, max_bands: int = DEFAULT_MAX_BANDS
                  ) -> list[tuple[int, int | None]]:
    """把记录数切成可读档位，返回闭区间 ``[(lo, hi | None), ...]``。

    最高一档 ``hi`` 为 ``None``（开区间"≥ lo"）。整体是 1-2-5 系列，
    档数受 ``max_bands`` 限制（档太多图例就读不动了）。
    """
    if max_count <= 1:
        return [(1, None)]
    # 1 一定在候选边界里，而 max_count >= 2，因此 starts 恒非空。
    starts = [edge for edge in _BAND_EDGE_STEPS if edge <= max_count]
    if len(starts) > max_bands:
        index = np.linspace(0, len(starts) - 1, max_bands).round().astype(int)
        starts = sorted({starts[i] for i in index})
    bands: list[tuple[int, int | None]] = []
    for position, start in enumerate(starts):
        if position + 1 < len(starts):
            bands.append((start, starts[position + 1] - 1))
        else:
            bands.append((start, None))
    return bands


def band_label(band: tuple[int, int | None]) -> str:
    """档位文案："1" / "2–4" / "≥25"。"""
    low, high = band
    if high is None:
        return f"≥{low:,}"
    if high <= low:
        return f"{low:,}"
    return f"{low:,}–{high:,}"


def band_indexes(counts: np.ndarray, bands: Sequence[tuple[int, int | None]]) -> np.ndarray:
    """把计数矩阵映射成档位序号（float 数组，空格为 NaN）。"""
    values = counts.astype(float)
    result = np.full(values.shape, np.nan, dtype=float)
    for index, (low, high) in enumerate(bands):
        mask = values >= low
        if high is not None:
            mask &= values <= high
        mask &= values > 0
        result[mask] = float(index)
    return result


def colorscale_from_bands(colors: Sequence[str]) -> list[list[Any]]:
    """把档位颜色展开成 Plotly 的硬边界 colorscale（每档一段纯色）。

    两个约束必须同时满足，否则 plotly.js 会**静默丢弃**整个色标并回退到
    内置彩虹色（实测踩过）：首档必须正好从 0 开始、末档必须正好到 1 结束；
    相邻档之间用完全相同的边界位置（重复位置 plotly.js 接受，用
    ``1 - 1e-6`` 之类的近似值反而不行）。
    """
    total = len(colors)
    if total == 0:
        return []
    scale: list[list[Any]] = []
    for index, color in enumerate(colors):
        scale.append([index / total, color])
        scale.append([(index + 1) / total, color])
    return scale


def band_legend(bands: Sequence[tuple[int, int | None]], colors: Sequence[str]
                ) -> list[dict[str, Any]]:
    """档位图例（ECharts piecewise visualMap 的 pieces / 前端小图例共用）。"""
    legend = []
    for index, band in enumerate(bands):
        legend.append({
            "label": band_label(band),
            "low": band[0],
            "high": band[1],
            "color": colors[index] if index < len(colors) else colors[-1],
        })
    return legend


@dataclass
class DensityGrid:
    """一个面板的密度网格（记录数矩阵 + 分箱边界）。"""

    x_edges: np.ndarray
    y_edges: np.ndarray
    counts: np.ndarray  # shape (ny, nx)
    total: int

    @property
    def x_centers(self) -> np.ndarray:
        return (self.x_edges[:-1] + self.x_edges[1:]) / 2.0

    @property
    def y_centers(self) -> np.ndarray:
        return (self.y_edges[:-1] + self.y_edges[1:]) / 2.0

    @property
    def max_count(self) -> int:
        return int(self.counts.max()) if self.counts.size else 0

    @property
    def occupied_cells(self) -> int:
        return int(np.count_nonzero(self.counts))

    @property
    def empty_share(self) -> float:
        """空格占比：值越大说明点云越"抱团"（分布越集中）。"""
        if self.counts.size == 0:
            return 0.0
        return float(1.0 - self.occupied_cells / self.counts.size)

    @property
    def half_mass_share(self) -> float:
        """装下一半记录所需的最小网格面积占比。

        比"空格占比"更能表达"分散程度"：一个极紧的簇只占几个格子就能装下
        一半记录（面积占比接近 0），而弥散的云要铺满大半个网格。空格占比
        在"簇很紧但整体铺得很开"时反而会给出高值，容易把两组说反。
        """
        if self.counts.size == 0 or self.total <= 0:
            return 0.0
        flat = np.sort(self.counts.ravel())[::-1]
        cumulative = np.cumsum(flat)
        needed = int(np.searchsorted(cumulative, self.total * 0.5) + 1)
        return min(1.0, needed / self.counts.size)

    @property
    def peak_share(self) -> float:
        """最密单格装了多少比例的记录（"多抱团"的直觉指标）。"""
        return self.max_count / self.total if self.total else 0.0

    @property
    def mean_occupied(self) -> float:
        """非空网格的平均记录数。"""
        occupied = self.counts[self.counts > 0]
        return float(occupied.mean()) if occupied.size else 0.0

    @property
    def concentration_ratio(self) -> float:
        """峰值格 / 非空格均值。"""
        mean = self.mean_occupied
        return self.max_count / mean if mean > 0 else 0.0

    @property
    def noise_peak_ratio(self) -> float:
        """均匀随机点云（泊松过程）下"峰值 / 均值"的期望上限。

        非空网格数 N、平均每格 λ 时，最大格计数 ≈ λ + sqrt(2λ·ln N)，
        因此纯统计涨落能造出的峰均比约 1 + sqrt(2·ln N / λ)。格数越多、
        每格越空，"最密格"就越是假象——判断是否真有聚集必须先扣掉它。
        """
        cells = self.occupied_cells
        mean = self.mean_occupied
        if cells <= 1 or mean <= 0:
            return 1.0
        return 1.0 + math.sqrt(2.0 * math.log(cells) / mean)

    @property
    def cluster_threshold(self) -> float:
        """判定"真有聚集"的峰均比门槛（随机涨落上限 + 30% 余量）。"""
        return max(1.8, 1.3 * self.noise_peak_ratio)

    @property
    def has_clusters(self) -> bool:
        """是否存在真实聚集（而非均匀分布 + 采样涨落）。"""
        return self.concentration_ratio >= self.cluster_threshold

    def marginal_x(self) -> np.ndarray:
        return self.counts.sum(axis=0)

    def marginal_y(self) -> np.ndarray:
        return self.counts.sum(axis=1)

    def hotspots(self, top: int = 3, min_separation: int = 3) -> list[dict[str, Any]]:
        """最密集的若干个区域（贪心 + 最小间隔，避免报出相邻的同一团）。

        返回 ``[{"x0","x1","y0","y1","count","share"}, ...]``，按记录数
        降序。热点是"哪里挤"的白话锚点——纯密度图上用户仍需一句话告诉他
        最集中的地方在哪。
        """
        if self.counts.size == 0 or self.total <= 0:
            return []
        flat = self.counts.ravel()
        order = np.argsort(flat)[::-1]
        ny, nx = self.counts.shape
        picked: list[tuple[int, int]] = []
        result: list[dict[str, Any]] = []
        for index in order:
            count = int(flat[index])
            if count <= 0:
                break
            iy, ix = divmod(int(index), nx)
            if any(abs(iy - py) <= min_separation and abs(ix - px) <= min_separation
                   for py, px in picked):
                continue
            picked.append((iy, ix))
            result.append({
                "x0": float(self.x_edges[ix]),
                "x1": float(self.x_edges[ix + 1]),
                "y0": float(self.y_edges[iy]),
                "y1": float(self.y_edges[iy + 1]),
                "count": count,
                "share": count / self.total,
            })
            if len(result) >= top:
                break
        return result

    def peak(self) -> dict[str, Any] | None:
        """记录数最高的单格（结构锚点：密度峰值标注）。"""
        spots = self.hotspots(top=1, min_separation=0)
        return spots[0] if spots else None


@dataclass
class DensityPanel:
    """一个分面：某个分组（或全部记录）的密度网格。"""

    name: str
    grid: DensityGrid
    share: float  # 该分面记录数 / 参与绘图的总记录数
    levels: tuple[str, ...] = ()  # 该面板覆盖的原始分组取值（"其他"面板会有多个）


@dataclass
class DensityView:
    """密度视图的完整聚合结果（渲染层只消费这里的数据）。"""

    panels: list[DensityPanel]
    total: int
    rows_used: int
    x_range: tuple[float, float]
    y_range: tuple[float, float]
    bin_shape: tuple[int, int]
    dropped_rows: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def max_count(self) -> int:
        return max((panel.grid.max_count for panel in self.panels), default=0)

    @property
    def bands(self) -> list[tuple[int, int | None]]:
        """所有面板共用的档位（颜色含义跨面板一致才可横向比较）。"""
        return density_bands(self.max_count)

    def band_colors(self, *, dark: bool = False) -> list[str]:
        palette = DENSITY_BAND_COLORS_DARK if dark else DENSITY_BAND_COLORS_LIGHT
        return _fit_palette(palette, len(self.bands))

    @property
    def overall(self) -> DensityGrid | None:
        """所有分面合并后的整体网格（用于全局热点描述）。"""
        if not self.panels:
            return None
        if len(self.panels) == 1:
            return self.panels[0].grid
        merged = np.zeros_like(self.panels[0].grid.counts)
        for panel in self.panels:
            merged = merged + panel.grid.counts
        return DensityGrid(
            x_edges=self.panels[0].grid.x_edges,
            y_edges=self.panels[0].grid.y_edges,
            counts=merged,
            total=int(merged.sum()),
        )

    @property
    def is_faceted(self) -> bool:
        return len(self.panels) > 1

    @property
    def bin_label(self) -> str:
        nx, ny = self.bin_shape
        return f"{nx}×{ny}"

    @property
    def has_real_clusters(self) -> bool:
        """是否存在真实聚集（而非均匀分布 + 采样涨落）。"""
        grid = self.overall
        return bool(grid and grid.has_clusters)

    def busiest_panel(self) -> DensityPanel | None:
        """最集中的分面（单格装了最多比例的记录）。"""
        return max(self.panels, key=lambda panel: panel.grid.peak_share) if self.panels else None

    def spread_panel(self) -> DensityPanel | None:
        """最分散的分面（装下一半记录要铺最多的网格面积）。"""
        return (max(self.panels, key=lambda panel: panel.grid.half_mass_share)
                if self.panels else None)


def _fit_palette(palette: Sequence[str], count: int) -> list[str]:
    """把色板适配到档数：不够时在末尾重复最深的颜色（极端重尾数据）。"""
    if count <= 0:
        return []
    if count <= len(palette):
        return list(palette[:count])
    return list(palette) + [palette[-1]] * (count - len(palette))


def _finite_range(values: np.ndarray) -> tuple[float, float] | None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    # finite 只保留过有限值，min/max 必然有限，无需二次校验。
    low = float(finite.min())
    high = float(finite.max())
    if high <= low:
        # 常数列：给一个对称的可视区间，避免 np.histogram2d 报边界相等。
        pad = abs(low) * 0.05 + 0.5
        return low - pad, high + pad
    return low, high


def _expand_range(bounds: tuple[float, float]) -> tuple[float, float]:
    """把范围向外放宽极小比例，避免最大值/最小值正好落在最后一个边界上。"""
    low, high = bounds
    pad = (high - low) * 1e-9 or 1e-9
    return low - pad, high + pad


def build_density_view(
    x_values: Sequence[float] | np.ndarray,
    y_values: Sequence[float] | np.ndarray,
    *,
    groups: Sequence[Any] | None = None,
    panel_size: tuple[float, float] = (1080.0, 560.0),
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
    max_panels: int = DENSITY_MAX_PANELS,
) -> DensityView | None:
    """把点云聚合成分面密度视图；数据不足或不适合聚合时返回 ``None``。

    ``groups`` 为每个点的分面标签（通常是颜色分组列），为 ``None`` 时
    只出一个整体面板。``x_range`` / ``y_range`` 传入轴的实际显示范围
    （例如"主体尺度"鲁棒范围），落在范围外的记录不计入网格——与用户在
    坐标轴上看到的内容保持一致。
    """
    x = np.asarray(x_values, dtype=float)
    y = np.asarray(y_values, dtype=float)
    if x.shape != y.shape:
        raise ValueError("x_values 与 y_values 长度必须一致")
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]
    labels = np.asarray(list(groups), dtype=object)[mask] if groups is not None else None
    if labels is not None:
        # 分组值缺失的记录无法归入任何分面，与 Plotly/ECharts 的着色语义
        # 一致地排除掉（否则会凭空多出一个 "nan" 面板）。
        valid = np.array([
            item is not None and not (isinstance(item, float) and math.isnan(item))
            for item in labels.tolist()
        ], dtype=bool)
        x, y, labels = x[valid], y[valid], labels[valid]
    if x.size < 2:
        return None

    bounds_x = _expand_range(x_range) if x_range else _finite_range(x)
    bounds_y = _expand_range(y_range) if y_range else _finite_range(y)
    if bounds_x is None or bounds_y is None:
        return None

    inside = (x >= bounds_x[0]) & (x <= bounds_x[1]) & (y >= bounds_y[0]) & (y <= bounds_y[1])
    dropped = int(x.size - int(inside.sum()))
    x, y = x[inside], y[inside]
    if labels is not None:
        labels = labels[inside]
    if x.size < 2:
        return None

    nx, ny = bin_shape(panel_size[0], panel_size[1])
    edges_x = np.linspace(bounds_x[0], bounds_x[1], nx + 1)
    edges_y = np.linspace(bounds_y[0], bounds_y[1], ny + 1)

    def _grid(sub_x: np.ndarray, sub_y: np.ndarray) -> DensityGrid:
        counts, _, _ = np.histogram2d(sub_x, sub_y, bins=[edges_x, edges_y])
        # histogram2d 返回 (nx, ny)，转置成 (ny, nx)：行号即 y 档位，与
        # Plotly 的 z 行序、ECharts y 轴类别顺序一致。
        matrix = counts.T.astype(np.int64)
        return DensityGrid(x_edges=edges_x, y_edges=edges_y, counts=matrix,
                           total=int(matrix.sum()))

    if labels is None or len(set(labels.tolist())) <= 1:
        only = "" if labels is None else str(next(iter(set(labels.tolist()))))
        grid = _grid(x, y)
        return DensityView(
            panels=[DensityPanel(name=only, grid=grid, share=1.0,
                                 levels=() if labels is None else (only,))],
            total=int(x.size),
            rows_used=int(x.size),
            x_range=bounds_x,
            y_range=bounds_y,
            bin_shape=(nx, ny),
            dropped_rows=dropped,
        )

    unique, counts = np.unique(labels.astype(str), return_counts=True)
    # 稳定排序：记录数相同时按取值排序，保证面板顺序可复现（否则同一个
    # 数据集两次生成的图表面板次序可能不同，用户会以为图变了）。
    order = np.argsort(counts, kind="stable")[::-1]
    unique = unique[order]
    counts = counts[order]
    total = int(counts.sum())
    text_labels = labels.astype(str)
    panels: list[DensityPanel] = []
    if len(unique) <= max_panels:
        selected: list[tuple[str, np.ndarray]] = [(str(level), text_labels == level) for level in unique]
    else:
        selected = [(str(level), text_labels == level) for level in unique[: max_panels - 1]]
        rest = np.isin(text_labels, unique[max_panels - 1:].tolist())
        selected.append((f"其他 {len(unique) - max_panels + 1} 类", rest))
    for name, panel_mask in selected:
        # 面板掩码由 np.unique 的取值构造，必然至少命中一条记录。
        sub_x, sub_y = x[panel_mask], y[panel_mask]
        grid = _grid(sub_x, sub_y)
        panels.append(DensityPanel(
            name=name,
            grid=grid,
            share=grid.total / total if total else 0.0,
            levels=tuple(sorted(set(text_labels[panel_mask].tolist()))),
        ))
    return DensityView(
        panels=panels,
        total=total,
        rows_used=int(x.size),
        x_range=bounds_x,
        y_range=bounds_y,
        bin_shape=(nx, ny),
        dropped_rows=dropped,
    )


def extreme_points(
    x_values: np.ndarray,
    y_values: np.ndarray,
    *,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    top: int = 120,
) -> tuple[np.ndarray, np.ndarray]:
    """挑出最"外围"的若干原始记录（稳健 z 距离最大的点）。

    密度视图按定义丢掉了单点身份（datashader 的 inspection reductions
    正是为此存在）。把最外围的少数真实记录以原始点叠加回来，既保住
    "个别极端记录长什么样"，又不会重新把画面糊掉（Power BI 的高密度
    抽样同样以实现离群点可见为目标）。
    """
    x = np.asarray(x_values, dtype=float)
    y = np.asarray(y_values, dtype=float)
    if x.shape != y.shape or x.size == 0:
        return np.array([]), np.array([])
    inside = (x >= x_range[0]) & (x <= x_range[1]) & (y >= y_range[0]) & (y <= y_range[1])
    x, y = x[inside], y[inside]
    if x.size == 0:
        return np.array([]), np.array([])

    def _robust_z(values: np.ndarray) -> np.ndarray:
        median = float(np.median(values))
        deviation = np.abs(values - median)
        scale = float(np.median(deviation)) * 1.4826  # MAD → 正态一致尺度
        if scale <= 0:
            scale = float(values.std()) or 1.0
        return np.abs(values - median) / scale

    distance = _robust_z(x) + _robust_z(y)
    keep = min(top, distance.size)
    index = np.argsort(distance)[::-1][:keep]
    return x[index], y[index]


def format_range(low: float, high: float) -> str:
    """区间文案："1,200 ~ 1,400"（大数带千分位，小数量保留有效位）。"""
    return f"{format_number(low)} ~ {format_number(high)}"


def format_number(value: float) -> str:
    if not math.isfinite(value):
        return "—"
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 10:
        return f"{value:,.1f}".rstrip("0").rstrip(".")
    if magnitude >= 0.01:
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return f"{value:.2e}"


def format_share(share: float) -> str:
    """占比文案：低于 0.1% 时保留两位小数（否则一律是刺眼的 0.0%）。"""
    percent = share * 100
    if percent >= 10:
        return f"{percent:.0f}%"
    if percent >= 0.1:
        return f"{percent:.1f}%"
    return f"{percent:.2f}%"


def clip_segment(
    segment: Sequence[float],
    *,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> list[float] | None:
    """把线段裁剪到可视范围内（Liang–Barsky），完全在范围外时返回 ``None``。

    趋势线按面板数据的 min/max 算端点，但坐标轴用的是共享范围：不裁剪的
    话线会从画面上方/右侧穿出去，看起来像"画错了"（实测截图可见）。
    """
    x0, y0, x1, y1 = (float(value) for value in segment)
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - x_range[0]), (dx, x_range[1] - x0),
                 (-dy, y0 - y_range[0]), (dy, y_range[1] - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        ratio = q / p
        if p < 0:
            if ratio > t1:
                return None
            t0 = max(t0, ratio)
        else:
            if ratio < t0:
                return None
            t1 = min(t1, ratio)
    return [x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy]


def density_note(view: DensityView, *, color_label: str | None = None) -> str:
    """密度视图说明（追加到图表解读里，让渲染方式的切换对用户可见）。"""
    if view.is_faceted:
        by = f"按{color_label}" if color_label else "按分组"
        head = (
            f"点云已{by}拆成 {len(view.panels)} 个密度面板"
            f"（{view.bin_label} 网格，颜色越深表示该区域记录越多）"
        )
    else:
        head = f"点云已聚合为 {view.bin_label} 网格的密度图（颜色越深表示该区域记录越多）"
    parts = [f"{head}，共 {view.rows_used:,} 条记录参与绘制"]
    if view.dropped_rows:
        parts.append(f"，{view.dropped_rows:,} 条落在坐标轴范围外未计入")
    return "".join(parts) + "。"


def hotspot_sentence(view: DensityView, x_label: str, y_label: str,
                     *, top: int = 2, panel: str | None = None) -> str:
    """最密集区域的句子（"哪里挤"的白话锚点）。

    没有真实聚集（峰均比过低）时不硬报热点，改说"分布比较均匀"——均匀
    随机点云的"最密集格"只是采样涨落，报出来是误导。
    """
    grid = view.overall if panel is None else next(
        (item.grid for item in view.panels if item.name == panel), view.overall
    )
    if grid is None:
        return ""
    if not grid.has_clusters:
        return (
            f"{x_label}与{y_label}的联合分布在范围内比较均匀，"
            "没有明显聚集区（最密格与平均水平接近，属于随机涨落）。"
        )
    # 判定有聚集后 hotspots 至少返回一格（top >= 1 由调用方保证）。
    spots = grid.hotspots(top=top)
    if not spots:  # pragma: no cover - 计数矩阵非空即必有正记录格
        return ""
    chunks = []
    for spot in spots:
        chunks.append(
            f"{x_label} {format_range(spot['x0'], spot['x1'])}、"
            f"{y_label} {format_range(spot['y0'], spot['y1'])}"
            f"（{spot['count']:,} 条，占 {format_share(spot['share'])}）"
        )
    return "最密集的区域在 " + "；".join(chunks) + "。"


def panels_sentence(view: DensityView, color_label: str) -> str:
    """分面差异的句子（"哪一组最抱团 / 最分散"，差异不明显时给结论）。"""
    if not view.is_faceted:
        return ""
    shares = sorted((panel.grid.peak_share for panel in view.panels), reverse=True)
    busiest = view.busiest_panel()
    spread = view.spread_panel()
    if busiest is None or spread is None:  # pragma: no cover - 分面至少两个面板
        return ""
    if shares[0] < 0.05:
        # 没有任何一组形成真正的热点：此时比较"谁最集中"是过度解读（差异
        # 全在采样涨落量级），直接给"都很分散"的结论。
        return (
            f"各{color_label}的分布都比较分散（最密单格占比都不超过 "
            f"{shares[0] * 100:.1f}%），没有哪一组明显更集中。"
        )
    if len(shares) >= 2 and shares[0] <= shares[1] * 1.3:
        return (
            f"各{color_label}的分布形态接近（最密单格占比都在 "
            f"{shares[-1] * 100:.1f}%~{shares[0] * 100:.1f}% 之间），没有哪一组明显更集中。"
        )
    parts = [
        f"{color_label}「{busiest.name}」最集中（最密的一格装了该组 "
        f"{busiest.grid.peak_share * 100:.0f}% 的记录）"
    ]
    if spread is not busiest:
        parts.append(
            f"「{spread.name}」最分散（一半记录要铺满 {spread.grid.half_mass_share * 100:.0f}% 的网格）"
        )
    return "；".join(parts) + "。"
