"""Plotly 渲染辅助：散点样式、3D 调整、WebGL 切换、密度视图、HTML 模板。

从 builder.py 提取的纯函数和大字符串常量，build_tools 闭包通过 re-export 引用。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from data_agent.density import build_density_view, should_use_density

from .charts import VIOLIN_SAMPLE_ROWS, _severe_axis_compression

#: Plotly 大数据 WebGL 阈值：超过该行数的散点/折线切换为 scattergl
#: （WebGL 渲染）。30 万行散点在 SVG 下渲染以分钟计（浏览器无响应），
#: scattergl 走 GPU 管线，几十万点仍可流畅悬停/缩放/导出。
_PLOTLY_WEBGL_THRESHOLD = 10_000


#: 散点"点径 / 透明度 / 描边"分档表：``(点数上限, 点径, 透明度, 描边宽度)``。
#: 两个引擎共用同一张表（ECharts 分支把点径换算成 symbolSize），保证同一份数据
#: 在两引擎下观感一致。设计依据：固定 9px/0.82 在几千点以上会糊成实心色块，
#: 分组色互相覆盖、看起来"只有一个颜色"；随点数缩小并降低透明度后，密集区
#: 自然混色、稀疏区与放大后仍能分辨单点（Tableau/Power BI 的默认做法）。
SCATTER_POINT_STYLE: tuple[tuple[int, float, float, float], ...] = (
    (500, 9.0, 0.85, 0.7),
    (2_000, 7.0, 0.72, 0.6),
    (5_000, 5.5, 0.60, 0.4),
    (_PLOTLY_WEBGL_THRESHOLD, 4.0, 0.50, 0.2),
    (float("inf"), 3.2, 0.45, 0.0),
)


def scatter_point_style(rows: int) -> dict[str, Any]:
    """按点数返回 Plotly 的 marker 配置（见 ``SCATTER_POINT_STYLE``）。"""
    for limit, size, opacity, border in SCATTER_POINT_STYLE:
        if rows <= limit:
            return {
                "size": size,
                "opacity": opacity,
                "line": {"width": border, "color": "white"},
            }
    return {"size": 3.2, "opacity": 0.45, "line": {"width": 0, "color": "white"}}  # pragma: no cover


def scatter_symbol_style(rows: int) -> tuple[float, float, float]:
    """按点数返回 ECharts 的 ``(symbolSize, opacity, borderWidth)``。

    与 Plotly 共用同一张表、同一数值：ECharts 的 ``symbolSize`` 与 Plotly 的
    ``marker.size`` 都是像素直径量级，取同值才能保证"同一份数据在两引擎下
    观感一致"（此前 ECharts 在大点云下反而更大，属历史偏差）。
    """
    for limit, size, opacity, border in SCATTER_POINT_STYLE:
        if rows <= limit:
            return size, opacity, border
    return 3.2, 0.45, 0.0  # pragma: no cover


def _refine_scatter_3d(fig: Any, *, rows: int) -> None:
    """把 3D 散点从"默认毛球"调成可读、可判断空间结构的样子（就地改 fig）。

    默认 ``px.scatter_3d`` 的问题（实测截图逐条确认）：

    1. 点接近不透明且没有描边/光照层次 → 前后点糊成一团，**看不出深度**；
    2. 立方体三轴等长（``aspectmode="cube"``）而三列量纲常相差几个数量级，
       真实形状被拉伸成一条斜带；
    3. 默认相机角度让密集区正好朝向观察者，遮挡最严重；
    4. 无 marker 描边时同色点相互粘连，边界不可辨。

    调整：点径与透明度按点数分档（与 2D 共用一张表），加白色细描边，
    立方体用**压缩比例**（见 ``_scatter_3d_aspect``），相机抬高俯视角，
    网格线弱化并去掉背景板。
    """
    size, opacity, border = scatter_symbol_style(rows)[:3]
    # 3D 里点需要比 2D 略小才不至于互相粘连（透视投影下近点会被放大）
    point_size = max(2.4, size - 1.4)
    aspect = _scatter_3d_aspect(fig)
    fig.update_traces(
        marker={
            "size": point_size,
            "opacity": min(0.92, opacity + 0.12),
            # 3D 描边只能靠 marker.line（scatter3d 支持），白描边给点之间留缝
            "line": {"width": 0.6 if border else 0.4, "color": "rgba(255,255,255,0.85)"},
            # 不要碰 showscale/colorbar：分类着色（color=地区）时强行显示色标
            # 会画出一条 1~9 的"类别编号色条"，看起来像多了一个连续维度
            # （实测踩到）。数值着色时 px 自己会带上 colorbar，保持原样即可。
        },
        selector={"type": "scatter3d"},
    )
    scene: dict[str, Any] = {
        # manual + 对数压缩比例：比 data 可读、比 cube 诚实（见 _scatter_3d_aspect）
        "aspectmode": "manual",
        "aspectratio": aspect,
        # 相机：抬高俯视角（z 更大）。默认视角下竖轴几乎与视线平行，
        # 整团点看起来是"一张平板"，量纲差异被压没（实测截图对比后定的值）。
        "camera": {"eye": {"x": 1.5, "y": 1.5, "z": 1.35}},
        "xaxis": {"showbackground": False, "gridcolor": "rgba(148,163,184,0.28)",
                  "zerolinecolor": "rgba(148,163,184,0.45)", "showspikes": False},
        "yaxis": {"showbackground": False, "gridcolor": "rgba(148,163,184,0.28)",
                  "zerolinecolor": "rgba(148,163,184,0.45)", "showspikes": False},
        "zaxis": {"showbackground": False, "gridcolor": "rgba(148,163,184,0.28)",
                  "zerolinecolor": "rgba(148,163,184,0.45)", "showspikes": False},
    }
    fig.update_layout(
        scene=scene,
        # 3D 图例横排在底部会挤压立方体高度，改为贴右侧竖排
        legend={"orientation": "v", "x": 1.0, "xanchor": "right", "y": 0.5},
        margin={"l": 0, "r": 0, "t": 56, "b": 0},
        hoverlabel={"namelength": -1},
    )


def _scatter_3d_aspect(fig: Any) -> dict[str, float]:
    """按三轴量程的对数压缩出立方体长宽比，兼顾"真实比例"与"可读性"。

    Plotly 的两个极端都不好用：``aspectmode="data"`` 忠实反映量程，但三列量纲
    相差百倍时小量程轴会被压成一条缝（刻度挤在一起、轴名也看不见）；
    ``aspectmode="cube"`` 三轴等长，形状信息全丢。这里取中间：长宽比与量程的
    **对数**成正比，并压缩到 [0.75, 1.25]，量程大的轴仍然更长（方向可读），
    但小量程轴不会被压没。
    """
    import numpy as np

    spans: list[float] = []
    for axis in ("x", "y", "z"):
        values: list[float] = []
        for trace in fig.data:
            raw = getattr(trace, axis, None)
            if raw is None:
                continue
            try:
                array = np.asarray(raw, dtype=float)
            except (TypeError, ValueError):
                continue
            finite = array[np.isfinite(array)]
            if finite.size:
                values.append(float(finite.max() - finite.min()))
        spans.append(max(values) if values else 1.0)
    logs = np.log10([max(span, 1e-9) for span in spans])
    spread = float(logs.max() - logs.min())
    if spread <= 0.05:
        # 三轴量程同量级：等比即可（此时 cube 不会失真）
        return {"x": 1.0, "y": 1.0, "z": 0.9}
    scaled = 0.75 + 0.5 * (logs - logs.min()) / spread
    return {"x": round(float(scaled[0]), 3), "y": round(float(scaled[1]), 3),
            "z": round(float(scaled[2]), 3)}


def _as_scattergl(fig: Any) -> Any:
    """把 Plotly 图例的 Scatter 轨迹整体换成 Scattergl（WebGL 渲染）。

    plotly.py 的 trace type 是只读的（update_traces(type=...) 抛
    "property 'type' is read-only"），且 fig.data 赋值要求轨迹来自原
    figure 本身，只能重建整个 figure。Scatter→Scattergl 属性完全兼容
    （marker/mode/hovertemplate/customdata 等全部透传），布局不变。
    """
    import plotly.graph_objects as go

    traces: list[go.Scattergl] = []
    for trace in fig.data:
        payload = trace.to_plotly_json()
        payload.pop("type", None)
        traces.append(go.Scattergl(**payload))
    return go.Figure(data=traces, layout=fig.layout)


def _plotly_webgl_if_large(fig: Any, df: pd.DataFrame, chart_type: str) -> Any:
    """大数据时把点状/线状图型切换为 WebGL 轨迹（仅 Plotly 分支）。"""
    if chart_type in {"scatter", "line"} and len(df) > _PLOTLY_WEBGL_THRESHOLD:
        return _as_scattergl(fig)
    return fig


def _stat_agg_note(stat_agg: dict[str, Any]) -> str:
    """统计图服务端聚合的说明文案（让"渲染方式变了"对用户可见）。"""
    total = int(stat_agg.get("total", 0))
    if stat_agg.get("kind") == "box":
        outliers = int(stat_agg.get("outliers", 0))
        share = outliers / total * 100 if total else 0.0
        return (
            f"\n\n注：数据量较大（{total:,} 条），箱体与须线按全量数据的五数概括"
            f"（下四分位/中位数/上四分位）与 1.5 倍四分位距直接计算绘制，"
            f"共检出 {outliers:,} 条须线外记录（{share:.1f}%）未逐点绘制。"
            "原始数据未做任何裁剪。"
        )
    return (
        f"\n\n注：数据量较大（{total:,} 条），直方图由服务端按全量数据分箱"
        f"（{int(stat_agg.get('bins', 0))} 个区间）绘制，纵轴是真实记录数"
        "而非抽样估计。"
    )


def _stratified_sample(df: pd.DataFrame, color: str | None, rows: int = VIOLIN_SAMPLE_ROWS) -> pd.DataFrame:
    """按分组列分层抽样（每组配额与其占比成正比）。

    小提琴图的形状来自核密度，两万个样本与三十万样本画出的小提琴几乎重合；
    但直接 `df.sample` 在极不平衡的分组下会把小组合并成一根细线，因此按组
    分配配额（每组至少 200 行，保证形状可辨）。
    """
    if len(df) <= rows:
        return df
    if not color or color not in df.columns:
        return df.sample(n=rows, random_state=42)
    groups = df.groupby(color, observed=True, dropna=True)
    quota = {level: max(200, int(round(rows * len(chunk) / len(df))))
             for level, chunk in groups}
    pieces = [chunk.sample(n=min(quota[level], len(chunk)), random_state=42)
              for level, chunk in groups]
    if not pieces:
        return df.sample(n=rows, random_state=42)
    return pd.concat(pieces)


def _density_view_for(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    color: str | None,
    scale_mode: str,
) -> tuple[Any, dict[str, Any] | None]:
    """超大数据散点 → 密度视图；返回 ``(view, scale_details)``，不适用时 ``(None, None)``。

    为什么不再逐点渲染：30 万行散点按"每点一个标记"画出来是同一像素上叠
    几十个半透明点，alpha 饱和成一团灰噪声——品类色互相覆盖、密度差异被
    抹平（用户实测反馈"所有的点和数据都堆在一起，根本看不出来什么"）。
    改为服务端聚合成网格 + 分档颜色表达记录数（datashader / 2D 直方图 /
    seaborn jointplot 的通行做法），并在有颜色分组时拆成分面密度图。

    ``scale_details`` 的字段与 ``_apply_outlier_scale_controls`` 完全一致，
    保证响应结构不因渲染方式切换而变化：极端值仍走"主体尺度"，超出范围
    的记录不计入网格（条数写进解读文案，不静默丢数据）。
    """
    if not (x and y and x in df.columns and y in df.columns):
        return None, None
    if not should_use_density(len(df)):
        return None, None
    x_values = pd.to_numeric(df[x], errors="coerce")
    y_values = pd.to_numeric(df[y], errors="coerce")
    pair = pd.DataFrame({"x": x_values.to_numpy(), "y": y_values.to_numpy()}).dropna()
    if len(pair) < 2:
        return None, None

    x_guard = y_guard = None
    if scale_mode != "full":
        x_guard = _severe_axis_compression(pair["x"].tolist())
        y_guard = _severe_axis_compression(pair["y"].tolist())
    groups = None
    faceted = False
    if color and color in df.columns:
        groups = df[color].tolist()
        faceted = df[color].nunique(dropna=True) > 1
    # 分面后面板变窄，网格同步变少，保证每格在屏幕上仍接近正方形。
    panel_size = (556.0, 452.0) if faceted else (1080.0, 560.0)
    view = build_density_view(
        x_values.to_numpy(dtype=float),
        y_values.to_numpy(dtype=float),
        groups=groups,
        panel_size=panel_size,
        x_range=(x_guard["lower"], x_guard["upper"]) if x_guard else None,
        y_range=(y_guard["lower"], y_guard["upper"]) if y_guard else None,
    )
    if view is None:
        return None, None

    axis_ranges: dict[str, list[float]] = {}
    if x_guard:
        axis_ranges["x"] = [x_guard["lower"], x_guard["upper"]]
    if y_guard:
        axis_ranges["y"] = [y_guard["lower"], y_guard["upper"]]
    extreme_points = max(
        x_guard["extreme_count"] if x_guard else 0,
        y_guard["extreme_count"] if y_guard else 0,
    )
    scale_details = {
        "scale_mode": "robust" if axis_ranges else "full",
        "extreme_points": extreme_points,
        "axis_ranges": axis_ranges,
    }
    return view, scale_details


#: 图表分类色板：Tableau 10 官方默认色板（数据可视化业界标准，明度
#: 层级统一、色相分布均匀，白底/暗底均协调）。双引擎（Plotly /
#: ECharts）共享，保证视觉一致。
_CHART_COLORS = [
    "#4E79A7",  # 蓝
    "#F28E2B",  # 橙
    "#E15759",  # 红
    "#76B7B2",  # 青绿
    "#59A14F",  # 绿
    "#EDC948",  # 黄
    "#B07AA1",  # 紫
    "#FF9DA7",  # 粉
    "#9C755F",  # 棕
    "#BAB0AC",  # 灰
]


#: Plotly 暗色模式自适应脚本：注入图表 HTML，监听主题变化动态切换背景和文字颜色。
#: 浅色回退值与图表生成时的原始色板一致（#fbfaf5/#102a2a/#E5ECE9/#C9D5D1），
#: 避免 applyTheme 首次执行时改变浅色图表的视觉。
#: scattergl WebGL 图表在运行时主题 relayout（暗色脚本）后的缩放修复 +
#: 点径自适应。plotly.js 的 gl 画布缓存在初始化时的背景色，relayout 更新
#: layout 背景色后不会重绘 gl 画布——深度缩放（modebar/框选）后绘图区会
#: 退回初始浅色（暗色模式下出现"白带"）。此处监听 plotly_relayout 事件：
#: 含 WebGL 轨迹的图表在暗色主题下 react 重渲染修复白带（保留当前缩放
#: 范围）；同时按缩放深度自适应点径/不透明度——全量 3.5px/50% 的彩色
#: 点阵在深度放大后太淡，放大越深点越大越实，回到全量恢复点阵（浅色
#: 主题同样需要点径自适应，白带修复才限定暗色）。自包含 IIFE，可被
#: 预览/下载修复链注入历史产物（旧模板不含本脚本）。
_PLOTLY_GL_REFRESH_SCRIPT = """<script>/*gl-refresh-fix*/
(function() {
  var bound = false, reacting = false, timer = null, polled = 0;
  var fullRanges = null;
  // 白带修复只需每个主题状态做一次：react 会重建 gl 画布（暗色底色被
  // 固化），后续缩放的画布重绘不再回退浅色。每次缩放都 react 意味着
  // 每次缩放后都要全量重绘几秒——深层放大/缩小时持续抖动卡顿的来源。
  var repaired = false;
  var lastSize = 0, lastOpacity = 0;
  function hasGl() {
    var gd = document.querySelector('.plotly-graph-div');
    var traces = gd ? gd.data : [];
    for (var i = 0; i < traces.length; i++) {
      if (/gl$|3d$/i.test(String(traces[i].type || ''))) return true;
    }
    return false;
  }
  function recordRanges() {
    var gd = document.querySelector('.plotly-graph-div');
    if (!gd || !gd._fullLayout) return;
    fullRanges = { x: gd._fullLayout.xaxis.range, y: gd._fullLayout.yaxis.range };
  }
  // 缩放倍数：当前轴范围相对初始全量范围的宽度比（x/y 取较大者）
  function zoomLevel() {
    var gd = document.querySelector('.plotly-graph-div');
    if (!gd || !gd._fullLayout) return 1;
    var factor = 1;
    if (fullRanges && fullRanges.x && gd._fullLayout.xaxis.range) {
      var fx = Math.abs(fullRanges.x[1] - fullRanges.x[0]) || 1;
      var wx = Math.abs(gd._fullLayout.xaxis.range[1] - gd._fullLayout.xaxis.range[0]) || 1;
      factor = fx / wx;
    }
    if (fullRanges && fullRanges.y && gd._fullLayout.yaxis.range) {
      var fy = Math.abs(fullRanges.y[1] - fullRanges.y[0]) || 1;
      var wy = Math.abs(gd._fullLayout.yaxis.range[1] - gd._fullLayout.yaxis.range[0]) || 1;
      factor = Math.max(factor, fy / wy);
    }
    return factor;
  }
  // 大数据点阵缩放自适应：全量小点半透明（分组色混合），放大后点径
  // 与不透明度随缩放倍数提升，深度缩放下依然清晰可辨。数值未变时
  // 跳过 restyle（无变化的 restyle 也会触发一次全量重绘——深层放大
  // 时点径封顶后每次缩放都 restyle 会造成一颤一颤）。
  function adaptPointSize() {
    var gd = document.querySelector('.plotly-graph-div');
    if (!gd || !window.Plotly) return;
    var z = zoomLevel();
    var size = Math.min(9, Math.max(3.5, 3.5 * Math.sqrt(z)));
    var opacity = Math.min(0.9, Math.max(0.5, 0.5 * Math.pow(z, 0.2)));
    if (Math.abs(size - lastSize) < 0.25 && Math.abs(opacity - lastOpacity) < 0.02) return;
    lastSize = size;
    lastOpacity = opacity;
    try { Plotly.restyle(gd, { 'marker.size': size, 'marker.opacity': opacity }); } catch (e) {}
  }
  function refresh() {
    var gd = document.querySelector('.plotly-graph-div');
    if (!gd || !window.Plotly || reacting) return;
    if (document.documentElement.dataset.theme !== 'dark') return;
    reacting = true;
    try { Plotly.react(gd, gd.data, gd.layout); } catch (e) {}
    setTimeout(function () { reacting = false; }, 250);
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  }
  // 滚轮缩放会连续触发大量 plotly_relayout（每次滚动一格一次）。
  // 策略：
  // 1) 点径随缩放连续微调（每次 relayout 立即 adaptPointSize——
  //    实测单次 gl restyle 仅 ~7ms，delta 跳过保证封顶/回底后零操作），
  //    避免"缩放停止后一次性跳变"造成的点径突跳感；
  // 2) 白带修复的 react（30 万点全量重绘约 2.5s）只在缩放完全停止
  //    900ms 后执行且每主题状态仅一次——不与连续缩放抢占主线程。
  var settleTimer = null;
  function settledApply() {
    adaptPointSize();
    clearTimeout(settleTimer);
    settleTimer = setTimeout(function () {
      // 白带修复：每个主题状态只需一次（react 后画布底色固化），
      if (document.documentElement.dataset.theme === 'dark' && !repaired) {
        repaired = true;
        schedule();
      }
    }, 900);
  }
  function bind() {
    var gd = document.querySelector('.plotly-graph-div');
    if (bound || !gd || !gd.on || !window.Plotly) return false;
    bound = true;
    if (hasGl()) {
      recordRanges();
      adaptPointSize();
      gd.on('plotly_relayout', settledApply);
      // 主题切换后暗色 gl 画布重新变为未修复状态：下一次缩放时再修复
      var themeWatcher = new MutationObserver(function () {
        repaired = false;
        lastSize = 0;
        lastOpacity = 0;
      });
      themeWatcher.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
    return true;
  }
  var keep = setInterval(function () {
    if (bound) { clearInterval(keep); return; }
    if (bind()) { clearInterval(keep); return; }
    if (++polled > 25) { clearInterval(keep); }
  }, 200);
})();
</script>"""


_PLOTLY_DARK_MODE_SCRIPT = """<script>
(function() {
  // 运行时错误上报：图表脚本执行失败且画布未渲染时，把错误消息回传
  // 父页面（{type:'chart-error'}），让预览面板显示具体错误而不是永远空白。
  // 延迟检查 .main-svg 避免把非致命错误误报成渲染失败。
  window.addEventListener('error', function(e) {
    setTimeout(function() {
      if (!document.querySelector('.plotly-graph-div .main-svg')) {
        try { parent.postMessage({type: 'chart-error', message: String((e && e.message) || '图表脚本执行失败')}, '*'); } catch (_) {}
      }
    }, 300);
  });
  function getIsDark() {
    var t = document.documentElement.dataset.theme;
    if (t === 'dark') return true;
    if (t === 'light') return false;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function applyTheme() {
    var isDark = getIsDark();
    var plotEl = document.querySelector('.plotly-graph-div');
    if (!plotEl) return;
    // 注意：relayout 的键必须是相对 figure 根节点的属性路径（如
    // 'paper_bgcolor' / 'font.color'）。带 'layout.' 前缀的写法（旧版
    // bug）在 Plotly v3 会被静默忽略，导致暗色只改了页面背景、
    // 图表画布/文字/网格仍是浅色。
    var update = {
      'paper_bgcolor': isDark ? '#1c2433' : '#fbfaf5',
      'plot_bgcolor': isDark ? '#1c2433' : '#fbfaf5',
      'font.color': isDark ? '#e6eaf0' : '#102a2a',
      // 标题字体色是显式写死的深色（builder 统一样式），不改它的话暗色下
      // 标题就是"深色字 + 深色底"，实测在密度图/多子图上直接看不见。
      'title.font.color': isDark ? '#e6eaf0' : '#102a2a',
      'legend.bgcolor': isDark ? 'rgba(28, 36, 51, 0.85)' : 'rgba(255, 255, 255, 0.82)',
      'legend.bordercolor': isDark ? '#2a3445' : '#D9E1DE',
    };
    // 多子图（散点矩阵、密度分面、边缘直方图）的坐标轴是 xaxis/xaxis2/xaxis3…
    // 只改 xaxis/yaxis 会让其余子图保留浅色网格线，在深色底上出现刺眼白线。
    Object.keys(plotEl.layout).forEach(function(key) {
      if (!/^[xy]axis[0-9]*$/.test(key)) return;
      update[key + '.gridcolor'] = isDark ? '#2a3445' : '#E5ECE9';
      update[key + '.zerolinecolor'] = isDark ? '#3a4458' : '#C9D5D1';
    });
    // 图内按钮（主体尺度/全量视图、显示抽样原始点）是显式写死的浅色底 + 深色字，
    // 暗色主题下会变成一块白斑；逐组跟着主题换底色/字色/描边。
    (plotEl.layout.updatemenus || []).forEach(function(_, index) {
      update['updatemenus[' + index + '].bgcolor'] = isDark ? '#1f2733' : '#FFFFFF';
      update['updatemenus[' + index + '].bordercolor'] = isDark ? '#2a3445' : '#CBD5D1';
      update['updatemenus[' + index + '].font.color'] = isDark ? '#c9cfd9' : '#245C55';
    });
    Plotly.relayout(plotEl, update);
    // 密度视图（大数据散点聚合）的档位色板随主题切换：浅色主题下低档是
    // 近白浅蓝、暗色主题下低档是深蓝——同一颜色在两种主题下必须代表同一
    // 档记录数，所以两套色标由图对象带过来，这里只做替换不做重新分档。
    // restyle 的 colorscale 必须包一层数组（值数组按 trace 展开），否则
    // plotly.js 会把 [[pos, color], ...] 误读成"每个 trace 一个色标"。
    var densityMeta = plotEl.layout && plotEl.layout.meta && plotEl.layout.meta.density_view;
    if (densityMeta && densityMeta.colorscales) {
      var bandScale = isDark ? densityMeta.colorscales.dark : densityMeta.colorscales.light;
      var heatmapIndexes = [];
      for (var i = 0; i < plotEl.data.length; i++) {
        if (plotEl.data[i].type === 'heatmap') heatmapIndexes.push(i);
      }
      if (heatmapIndexes.length && bandScale) {
        try { Plotly.restyle(plotEl, {'colorscale': [bandScale]}, heatmapIndexes); } catch (_) {}
      }
    }
    document.documentElement.style.background = isDark ? '#1c2433' : '#fbfaf5';
    document.body.style.background = isDark ? '#1c2433' : '#fbfaf5';
  }
  setTimeout(applyTheme, 100);
  var observer = new MutationObserver(function() { setTimeout(applyTheme, 50); });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
  // 响应式重绘：监听窗口尺寸变化，防抖 150ms 后调用 Plotly.Plots.resize，
  // 避免拖拽调整预览模态或全屏切换时图表被裁剪/留白。
  var _resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function() {
      var gd = document.querySelector('.plotly-graph-div');
      if (gd && window.Plotly) Plotly.Plots.resize(gd);
    }, 150);
  });
  // PNG 导出（postMessage 通道）：父页面发送 {type:"download-png"} 触发导出，
  // 这里调用 Plotly.toImage 生成 dataURL 后回传 {type:"png-data", data}。
  // 使用 postMessage 而非直接下载，可避免 iframe 需要 allow-same-origin 权限。
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'download-png') {
      var gd = document.querySelector('.plotly-graph-div');
      if (gd && window.Plotly) {
        Plotly.toImage(gd, {format: 'png', width: 1200, height: 700, scale: 2}).then(function(url) {
          parent.postMessage({type: 'png-data', data: url}, '*');
        });
      }
    }
  });
})();
</script>
""" + _PLOTLY_GL_REFRESH_SCRIPT


_AGGREGATION_LABELS = {
    "sum": "合计",
    "mean": "平均值",
    "median": "中位数",
    "count": "计数",
    "min": "最小值",
    "max": "最大值",
}


def _collect_trace_values(fig: Any, axis: str) -> list[float]:
    """从 Plotly figure 的所有 trace 收集指定轴的数值数据。"""
    values: list[float] = []
    for trace in fig.data:
        if getattr(trace, "name", None) == "极端值提示":
            continue
        raw = getattr(trace, axis, None)
        if raw is None:
            continue
        for v in raw:
            try:
                num = float(v)
                if pd.notna(num) and np.isfinite(num):
                    values.append(num)
            except (TypeError, ValueError):
                continue
    return values


def _render_plotly_html(
    *,
    title: str,
    script_src: str,
    div: str,
    interpretation_block: str = "",
    dark_script: str = "",
    full_page: bool = True,
) -> str:
    """Shared Plotly HTML template used by chart creation and editing paths.

    Both ``create_visualization`` (builder.py) and ``edit_chart`` (artifacts.py)
    use this helper to produce consistent HTML documents, avoiding duplicated
    template strings and CSS blocks.
    """
    style = (
        "html,body{width:100%;height:100%;margin:0;background:#fbfaf5;overflow:hidden;"
        "font-family:'IBM Plex Sans','Noto Sans SC',sans-serif}"
        ".plotly-graph-div{width:100% !important;height:100% !important;min-height:440px}"
        ".layout{display:flex;flex-direction:column;height:100%}"
        ".chart-wrap{flex:1;min-height:0}"
        ".plotly-interpretation{border-top:1px solid #e5e7eb;padding:14px 24px;background:#f9fafb;"
        "font-size:13px;line-height:1.75;color:#374151;max-height:160px;overflow-y:auto}"
        ".plotly-interpretation-title{font-size:12px;color:#6b7280;font-weight:600;"
        "margin-bottom:6px;letter-spacing:0.5px}"
        # 暗色模式：数据解读卡片必须随主题换肤，否则暗背景下一块白色
        # 卡片非常突兀（iframe 文档由 chart-theme-bridge 设置 data-theme）。
        "html[data-theme='dark'] .plotly-interpretation{border-top-color:#2a3445;"
        "background:#1c2433;color:#c9cfd9}"
        "html[data-theme='dark'] .plotly-interpretation-title{color:#9aa4b5}"
    )
    if full_page:
        body = (
            f"<div class='layout'><div class='chart-wrap'>{div}</div>"
            f"{interpretation_block}</div>{dark_script}"
        )
    else:
        body = f"{div}{dark_script}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><script src='{script_src}'></script>"
        f"<style>{style}</style>"
        f"</head><body>{body}</body></html>"
    )
