"""ECharts 渲染引擎常量：视觉 token、主题 dict、JS 格式化、密度阈值、模板字符串。

从 ``echarts_engine.py`` 拆分而来，纯配置/字面量，零行为逻辑。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from data_agent.tools.builder import _CHART_COLORS

_ECHARTS_PALETTE = _CHART_COLORS

# 文本/网格/背景色：与前端 tokens.css 亮色令牌一致（fg-default/border-default），
# 图表嵌在前端 iframe 里时不产生色差
_ECHARTS_TEXT_COLOR = "#1a1d29"
_ECHARTS_TEXT_SECONDARY = "#6b7280"
_ECHARTS_GRID_COLOR = "#e4e6ea"
_ECHARTS_BG_COLOR = "#ffffff"
_ECHARTS_BORDER_COLOR = "#eef0f3"

# 字体栈：与前端 tokens.css 一致（Inter 优先），图表与外层界面字形统一
_ECHARTS_FONT_FAMILY = (
    "'Inter', 'IBM Plex Sans', 'Noto Sans SC', -apple-system, "
    "'Segoe UI', 'PingFang SC', 'Microsoft YaHei UI', sans-serif"
)

# ECharts 主题常量：所有图表共享，保证视觉一致
_ECHARTS_BASE_GRID = {
    "left": 64,
    "right": 32,
    # grid 顶部跟随 legend 下移（legend top:80 + 图例高度 + 安全边距），
    # 保证折线/柱体主体不会被下移后的图例压住。
    "top": 112,
    "bottom": 72,
    "containLabel": True,
}

_ECHARTS_BASE_AXIS = {
    "axisLine": {"lineStyle": {"color": _ECHARTS_GRID_COLOR}},
    "axisTick": {"show": False},
    "axisLabel": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
    "splitLine": {"show": True, "lineStyle": {"color": _ECHARTS_BORDER_COLOR, "type": "dashed"}},
    "nameTextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12, "padding": [0, 0, 0, -16]},
}

#: 轴名位置：ECharts 默认把类目轴名放在轴末端（"区域"贴在绘图区右下角），
#: 实测看起来像残留文本、也容易被裁掉。改成居中放在轴下方（数值轴同理放在
#: 左侧中部），这是 ECharts 官方示例之外的通用做法，读数时视线不用横跨整屏。
_ECHARTS_X_AXIS_NAME: dict[str, Any] = {"nameLocation": "middle", "nameGap": 32}
#: 数值轴名放在轴顶端偏左（与 y 轴刻度同一视觉列），避免与顶部标题打架。
_ECHARTS_Y_AXIS_NAME: dict[str, Any] = {
    "nameLocation": "end", "nameGap": 14,
    "nameTextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12, "align": "left"},
}

_ECHARTS_BASE_TOOLTIP = {
    "backgroundColor": "rgba(255,255,255,0.98)",
    "borderColor": _ECHARTS_GRID_COLOR,
    "borderWidth": 1,
    "padding": [12, 16],
    "textStyle": {"color": _ECHARTS_TEXT_COLOR, "fontSize": 13},
    "extraCssText": "box-shadow: 0 8px 24px rgba(0,0,0,0.08); border-radius: 10px;",
}

_ECHARTS_BASE_LEGEND = {
    # 图例下移到 top:80，给右上角亮/暗按钮（top:12 高 34px）和 toolbox
    # 留出充分垂直间距；right 设为 72，让水平图例整体左移，避免图例项
    # 从右向左排列时文本向左延伸到按钮下方，与主题按钮发生水平重叠。
    # type:scroll 防止多系列（如 color 维度 10 级）图例换行溢出、压住绘图区。
    "top": 80,
    "right": 72,
    "type": "scroll",
    "itemGap": 20,
    "itemWidth": 14,
    "itemHeight": 8,
    "icon": "roundRect",
    "textStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
    "inactiveColor": "#d1d5db",
}

_ECHARTS_BASE_TITLE = {
    "left": 16,
    "top": 16,
    # 标题右侧留出控件区（亮/暗按钮约 34px、toolbox 约 80px、安全边距），
    # 避免主标题/副标题被右上角按钮和工具箱截断或重叠。
    "right": 120,
    "textStyle": {"color": _ECHARTS_TEXT_COLOR, "fontSize": 18, "fontWeight": 600},
    "subtextStyle": {"color": _ECHARTS_TEXT_SECONDARY, "fontSize": 12},
}

_ECHARTS_BASE_TOOLBOX = {
    # right:70 给右上角亮/暗切换按钮（right:12、宽34px，左缘在距右46px处）
    # 留出约 24px 水平间隙，避免两者视觉/投影重叠。
    "right": 70,
    "top": 24,
    "itemSize": 16,
    "itemGap": 12,
    "iconStyle": {"borderColor": _ECHARTS_TEXT_SECONDARY},
    "emphasis": {"iconStyle": {"borderColor": _ECHARTS_PALETTE[0]}},
    "feature": {
        # 数据缩放：框选 + 滚轮 + 拖拽平移 + 一键重置，对标金融终端
        "dataZoom": {"yAxisIndex": "none", "title": {"zoom": "框选缩放", "back": "还原"}},
        "restore": {"title": "重置"},
        # 高清导出：适配论文/正式报告，2x 分辨率
        "saveAsImage": {"title": "导出 PNG", "pixelRatio": 2, "backgroundColor": _ECHARTS_BG_COLOR},
    },
}



class _JsFunction:
    """标记一段字符串应被序列化为 JS 函数字面量（而非 JSON 字符串）。

    ECharts 的 formatter / callback 需要真实的 JS 函数对象；但 Python 的
    json.dumps 会把函数源码字符串序列化为带引号的 JSON 字符串，导致前端
    把源码当普通文本显示（例如 Y 轴标签出现 "function(value){...}"）。
    此类用 UUID 占位符隔离用户数据，序列化后再替换为无引号的函数源码。
    """

    def __init__(self, code: str) -> None:
        self.code = code
        self.token = f"__JS_FN_{uuid.uuid4().hex}__"


# ECharts axisLabel formatter JS 函数：大数值自适应万/亿单位。
# 注入到 option 的 axisLabel.formatter，前端 ECharts 会作为函数执行。
_ECHARTS_AXIS_LABEL_FORMATTER_JS = _JsFunction(
    "function(value){"
    "if(value===0||value===null||isNaN(value)){return '0';}"
    "var sign=value<0?'-':'';var abs=Math.abs(value);"
    "if(abs>=100000000){return sign+(abs/100000000).toFixed(2).replace(/0+$/,'').replace(/\\.$/,'')+'亿';}"
    "if(abs>=10000){return sign+(abs/10000).toFixed(2).replace(/0+$/,'').replace(/\\.$/,'')+'万';}"
    "if(abs>=1000){return value.toLocaleString();}"
    "if(abs>=10){return abs.toFixed(1).replace(/0+$/,'').replace(/\\.$/,'');}"
    "if(abs>=1){return abs.toFixed(2).replace(/0+$/,'').replace(/\\.$/,'');}"
    "if(abs>=0.01){return abs.toFixed(3).replace(/0+$/,'').replace(/\\.$/,'');}"
    "if(abs>=0.001){return abs.toFixed(4).replace(/0+$/,'').replace(/\\.$/,'');}"
    "if(abs>0){return abs.toExponential(2);}"
    "return '0';"
    "}"
)


#: 数值标签 formatter（柱状图顶部数值等）：大数用「万」缩写、其余
#: 千分位并去浮点噪声。用 {c} 模板会裸显 70012.68000000001 这类
#: 浮点尾巴，且 10 万级数字与相邻标签重叠成乱码。
_ECHARTS_VALUE_LABEL_JS = _JsFunction(
    "function(p){var v=p.value;if(v==null||isNaN(v))return '';"
    "return Math.abs(v)>=10000?(v/10000).toFixed(1)+'\u4e07'"
    ":Number(v.toFixed(2)).toLocaleString();}"
)


_TIME_LABEL_PATTERN = re.compile(
    r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?([ T]\d{2}:\d{2}(:\d{2})?)?$"
)

_DENSITY_PANEL_SIZE = (1080.0, 560.0)
_DENSITY_FACET_PANEL_SIZE = (560.0, 468.0)

#: 每个面板叠加的"最外围原始记录"数量上限（与 Plotly 分支一致）。
_DENSITY_EXTREME_POINTS = 120

#: "放大看具体记录"用的分层抽样点数（默认隐藏，点图例才叠加；与 Plotly 分支同值）。
_DENSITY_DETAIL_POINTS = 6_000

#: 趋势线与面板标题里相关系数的显示门槛：弱相关（|r| < 0.25）画线只添噪声。
_DENSITY_TREND_MIN_R = 0.25

#: 密度图的强调色（峰值框 / 趋势线 / 最外围记录，与 Plotly 分支同色）。
_DENSITY_ACCENT = "#E15759"

#: 单面板布局（jointplot：主密度面板 + 顶部 x 分布 + 右侧 y 分布），百分比定位。
#: 百分比跟着容器缩放，缩略图（209×131）与预览模态（1400×850）用同一套几何。
_DENSITY_SINGLE_LAYOUT = {
    "left": 7.0,        # 留给 y 轴刻度与轴名
    "right": 2.5,
    "top": 14.0,        # 主标题 + 副标题
    "bottom": 14.0,     # x 轴刻度 + 轴名 + 档位色标
    "hist": 9.0,        # 边缘直方图厚度
    "hist_gap": 1.5,    # 边缘直方图与主面板的间隙
    "hist_width": 8.5,  # 右侧 y 分布宽度
}

#: 分面布局（2 列或 3 列）：面板标题写在每块面板上方，行间距要同时容下
#: 上一行的 x 刻度与下一行的面板标题。
_DENSITY_FACET_LAYOUT = {
    "left": 7.0, "right": 2.5, "top": 13.5, "bottom": 13.0,
    "gap_x": 3.2, "gap_y": 9.0, "title": 2.8,
}

#: 悬浮提示里的数字格式化（JS）：与类目标签/数值轴 formatter 同一口径
#: （万/亿优先，其余按有效位取整），避免 tooltip 出现 1234.5678901 这种尾巴。
_DENSITY_NUMBER_JS = (
    "var _dn=function(v){if(v===null||v===undefined||isNaN(v))return '—';var a=Math.abs(v);"
    "if(a>=100000000)return Number((v/100000000).toFixed(2))+'亿';"
    "if(a>=10000)return Number((v/10000).toFixed(2))+'万';"
    "return Number(v.toFixed(4)).toLocaleString();};"
)

_ECHARTS_DARK_MODE_SCRIPT = """<script>
(function() {
  // 运行时错误上报：图表脚本执行失败且实例未创建时，把错误消息回传
  // 父页面（{type:'chart-error'}），让预览面板显示具体错误而不是永远空白。
  // 延迟检查 __echartsInstance 避免把非致命错误误报成渲染失败。
  window.addEventListener('error', function(e) {
    setTimeout(function() {
      if (!window.__echartsInstance) {
        try { parent.postMessage({type: 'chart-error', message: String((e && e.message) || '图表脚本执行失败')}, '*'); } catch (_) {}
      }
    }, 300);
  });

  // 切换按钮图标（随主题互换），使用 currentColor 继承按钮文字色，亮/暗皆清晰。
  var SUN_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  var MOON_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  // 当前是否应为暗色：data-theme 显式优先，否则回退到系统偏好。
  function getIsDark() {
    var t = document.documentElement.dataset.theme;
    if (t === 'dark') return true;
    if (t === 'light') return false;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function updateToggleUI(isDark) {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.innerHTML = isDark ? SUN_SVG : MOON_SVG;
    btn.title = isDark ? '切换到亮色' : '切换到暗色';
    btn.setAttribute('aria-label', isDark ? '切换到亮色' : '切换到暗色');
  }

  // 系列色板双向映射：亮色学术色板为白底设计，在 #1a1b1e 暗底上明度不足发闷；
  // 切暗色时逐色提亮（保持色相），映射为双射，切回亮色可精确还原。
  // LP/DP 顺序与 Python _ECHARTS_PALETTE 一致；LR/DR 是 _hex_to_rgba 输出的前缀形态。
  var LP = ['#2c5f8d', '#d97745', '#4f9d7c', '#c75d63', '#7a6fb0',
            '#d2a63c', '#4b8fa8', '#8a9a5b', '#b07b9e', '#5e7a8c'];
  var DP = ['#6fa8d6', '#e89a6e', '#6fbf9c', '#e08a8f', '#a79ed6',
            '#e0bc6a', '#74b4cc', '#adbf7e', '#cfa0c0', '#8ca6b8'];
  var LR = ['rgba(44,95,141,', 'rgba(217,119,69,', 'rgba(79,157,124,', 'rgba(199,93,99,', 'rgba(122,111,176,',
            'rgba(210,166,60,', 'rgba(75,143,168,', 'rgba(138,154,91,', 'rgba(176,123,158,', 'rgba(94,122,140,'];
  var DR = ['rgba(111,168,214,', 'rgba(232,154,110,', 'rgba(111,191,156,', 'rgba(224,138,143,', 'rgba(167,158,214,',
            'rgba(224,188,106,', 'rgba(116,180,204,', 'rgba(173,191,126,', 'rgba(207,160,192,', 'rgba(140,166,184,'];

  function colorTable(toDark) {
    var t = {}, i;
    for (i = 0; i < LP.length; i++) t[toDark ? LP[i] : DP[i]] = toDark ? DP[i] : LP[i];
    if (toDark) {
      // 符号描边白→融入暗底；系列级深色文字→亮字；柱顶标签等次级文字→暗色次级色
      t['#fff'] = '#1a1b1e'; t['#ffffff'] = '#1a1b1e'; t['#1a1d29'] = '#e8eaed';
      t['#6b7280'] = '#9aa0a6';
    } else {
      t['#1a1b1e'] = '#ffffff'; t['#e8eaed'] = '#1a1d29'; t['#9aa0a6'] = '#6b7280';
    }
    return t;
  }

  function swapColor(s, table, rf, rt) {
    var lower = s.toLowerCase();
    if (table[lower]) return table[lower];
    for (var i = 0; i < rf.length; i++) {
      if (lower.indexOf(rf[i]) === 0) return rt[i] + lower.slice(rf[i].length);
    }
    return s;
  }

  // 递归替换系列中的颜色引用（实色/rgba/渐变 colorStops）；
  // 跳过 data 键：数据项级颜色（如热力图深色格白字）由 Python 预置，不参与主题翻转；
  // 函数（formatter）原样保留引用。
  function mapNode(node, table, rf, rt) {
    if (typeof node === 'string') return swapColor(node, table, rf, rt);
    if (Array.isArray(node)) {
      var arr = [];
      for (var i = 0; i < node.length; i++) arr.push(mapNode(node[i], table, rf, rt));
      return arr;
    }
    if (node && typeof node === 'object') {
      var out = {};
      for (var k in node) {
        if (!Object.prototype.hasOwnProperty.call(node, k)) continue;
        out[k] = (k === 'data') ? node[k] : mapNode(node[k], table, rf, rt);
      }
      return out;
    }
    return node;
  }

  function applyTheme() {
    var isDark = getIsDark();
    // 支持多实例（仪表盘导出页注入 __echartsInstances 数组）；
    // 单图页回退 __echartsInstance，行为与重构前完全一致。
    var __charts = window.__echartsInstances || (window.__echartsInstance ? [window.__echartsInstance] : []);
    for (var __ci = 0; __ci < __charts.length; __ci++) { var chart = __charts[__ci];
      // 暗色值与前端 tokens.css 一致（border/fg/canvas 令牌），亮色值与 Python 常量一致
      var axisColor = isDark ? '#2e2f33' : '#e4e6ea';
      var splitColor = isDark ? '#2e2f33' : '#eef0f3';
      var labelColor = isDark ? '#9aa0a6' : '#6b7280';
      var textColor = isDark ? '#e8eaed' : '#1a1d29';
      var tooltipBg = isDark ? 'rgba(36,37,40,0.98)' : 'rgba(255,255,255,0.98)';
      var axisUpdate = function() {
        return { axisLabel: { color: labelColor }, axisLine: { lineStyle: { color: axisColor } },
                 splitLine: { lineStyle: { color: splitColor } }, nameTextStyle: { color: labelColor } };
      };
      var update = {
        backgroundColor: isDark ? '#1a1b1e' : '#ffffff',
        // 顶层调色板跟随主题：饼/旭日/矩形树等靠 palette 自动分色的图同步提亮
        color: isDark ? DP : LP,
        textStyle: { color: textColor },
        title: { textStyle: { color: textColor }, subtextStyle: { color: labelColor } },
        legend: { textStyle: { color: labelColor },
                  // 未激活图例：亮灰在暗底上反而比激活项更显眼，暗色换成暗灰
                  inactiveColor: isDark ? '#5f6368' : '#d1d5db' },
        toolbox: { iconStyle: { borderColor: labelColor },
                   emphasis: { iconStyle: { borderColor: isDark ? '#6fa8d6' : '#2C5F8D' } },
                   // 工具箱导出 PNG 背景跟随主题，避免暗色图表配白底不可读
                   feature: { saveAsImage: { backgroundColor: isDark ? '#1a1b1e' : '#ffffff' } } },
        tooltip: { backgroundColor: tooltipBg, borderColor: axisColor, textStyle: { color: textColor } }
      };
      try {
        // 换肤状态源：首次 applyTheme（尚未改过任何颜色）用 getOption 固化
        // 一份「原始亮色快照」，后续换肤从快照重算。getOption 会深拷贝整个
        // option（30 万点散点的 series 数据几十 MB），每次换肤都调一次是
        // 无谓的大额开销。快照是初始亮色态，双向换肤语义不变：亮→暗走
        // LP→DP 色表映射；暗→亮时色表（DP→LP）匹配不到快照里的亮色，
        // swapColor 原样返回，等效于直接回落亮色原值。
        if (!chart.__themeSnapshot) chart.__themeSnapshot = chart.getOption() || {};
        var cur = chart.__themeSnapshot;
        // title 可能是数组（分面密度图：首个是主标题，其余是各面板标题）：
        // 逐项带上原文重发（不依赖 setOption 对组件数组的按项合并语义），
        // 主标题跟主题字色、面板标题保持固定的青绿强调色在暗底上提亮。
        // 单标题仍走上面的对象写法，既有行为完全不变。
        if (cur.title && cur.title.length > 1) {
          update.title = cur.title.map(function(t, i) {
            var item = {};
            for (var tk in t) {
              if (Object.prototype.hasOwnProperty.call(t, tk)) item[tk] = t[tk];
            }
            item.textStyle = i === 0
              ? { color: textColor }
              : { color: isDark ? '#8fd3cc' : '#245C55' };
            if (i === 0) item.subtextStyle = { color: labelColor };
            return item;
          });
        }
        if (cur.xAxis && cur.xAxis.length) update.xAxis = cur.xAxis.map(function() { return axisUpdate(); });
        if (cur.yAxis && cur.yAxis.length) update.yAxis = cur.yAxis.map(function() { return axisUpdate(); });
        if (cur.parallelAxis && cur.parallelAxis.length) update.parallelAxis = cur.parallelAxis.map(function() { return axisUpdate(); });
        // echarts-gl 3D 坐标轴不在 xAxis/yAxis 数组里，单独跟随主题；
        // gl 2.x 的 axisLabel 兼容 color / textStyle.color 两种写法，两者都设
        if (cur.xAxis3D) {
          var axis3dUpdate = function() {
            return { nameTextStyle: { color: labelColor },
                     axisLine: { lineStyle: { color: axisColor } },
                     axisLabel: { color: labelColor, textStyle: { color: labelColor } },
                     splitLine: { lineStyle: { color: splitColor } } };
          };
          update.xAxis3D = axis3dUpdate();
          update.yAxis3D = axis3dUpdate();
          update.zAxis3D = axis3dUpdate();
        }
        // dataZoom 滑条：亮灰槽底/拖动把手在暗底上会形成亮条，跟随主题换成中性深灰；
        // 填充色/把手色同步切换主色亮暗版本
        if (cur.dataZoom && cur.dataZoom.length) {
          update.dataZoom = cur.dataZoom.map(function(d) {
            if (d.type !== 'slider') return {};
            return { backgroundColor: isDark ? '#242528' : '#eceef1',
                     moveHandleStyle: { color: isDark ? '#3a3b40' : '#D2DBEE' },
                     fillerColor: isDark ? 'rgba(111,168,214,0.15)' : 'rgba(44,95,141,0.12)',
                     handleStyle: { color: isDark ? '#6fa8d6' : '#2C5F8D' },
                     textStyle: { color: labelColor } };
          });
        }
        // visualMap：除文字色外，暗色下替换色板——浅色端（相关性中点 #F7F7F7、
        // 顺序色低端 #EDF3F9）在暗底上刺眼；首次运行时缓存浅色原值供切回。
        // 发散色板（>3 段）中点换暗底色，两端降饱和抬亮度。
        // 密度图的档位色板（piecewise）不走 inRange：颜色在 pieces[i].color 上，
        // 必须整组换成 densityBands 的另一套，才能保证同一颜色在两种主题下代表
        // 同一档记录数（Tableau 明/暗色板同理）。逐项带上原始 min/max/label，
        // 不依赖 setOption 对数组的按项合并语义。
        var densityBands = cur.densityBands || chart.__densityBands || null;
        if (cur.visualMap && cur.visualMap.length) {
          if (!chart.__vmLightRange) {
            chart.__vmLightRange = cur.visualMap.map(function(v) {
              return (v.inRange && v.inRange.color) || null;
            });
          }
          update.visualMap = cur.visualMap.map(function(v, i) {
            var light = chart.__vmLightRange[i];
            var upd = { textStyle: { color: labelColor } };
            if (light && light.length) {
              var diverging = light.length > 3;
              upd.inRange = { color: isDark
                ? (diverging
                    ? ['#6FA3DC', '#4C79A9', '#3c4654', '#2a2b2f', '#52383e', '#A0525E', '#E0787F']
                    : ['#262b33', '#3A6386', '#4E8FC7'])
                : light };
            }
            if (densityBands) {
              var want = isDark ? densityBands.dark : densityBands.light;
              if (want && want.length && v.pieces && v.pieces.length) {
                upd.pieces = v.pieces.map(function(piece, j) {
                  var copy = {};
                  for (var pk in piece) {
                    if (Object.prototype.hasOwnProperty.call(piece, pk)) copy[pk] = piece[pk];
                  }
                  if (want[j] && want[j].color) copy.color = want[j].color;
                  return copy;
                });
              }
            }
            return upd;
          });
        }
        // 系列级颜色跟随主题：递归映射把线色/柱色/渐变填充整体切到亮暗对应色板；
        // 热力图单元格描边融入背景；箱线图均值菱形改为背景色填充 + 文字色描边。
        if (cur.series && cur.series.length) {
          var tbl = colorTable(isDark);
          var rf = isDark ? LR : DR, rt = isDark ? DR : LR;
          update.series = cur.series.map(function(s) {
            var m = mapNode(s, tbl, rf, rt);
            // mapNode 跳过 data 键：m.data 是 getOption 深拷贝出的全量
            // 数据副本。主题翻转只改颜色不改数据，未做数据级映射的系列
            // 把 data 从 update 里剥掉，setOption 合并时保留现场数据——
            // 否则 30 万点散点每次换肤都要把整份数据回灌 setOption（重
            // 建整个系列层，秒级卡顿）。
            var dataMapped = false;
            if (s.type === 'treemap' || s.type === 'sunburst') {
              // 层级图节点色在 data 树里（mapNode 跳过 data），对 data 单独递归：
              // name/value 不在色表中不受影响，只翻节点 itemStyle 颜色
              m.data = s.data.map(function(d) {
                var node = {};
                for (var k in d) {
                  if (!Object.prototype.hasOwnProperty.call(d, k)) continue;
                  node[k] = (k === 'itemStyle' || k === 'children')
                    ? mapNode(d[k], tbl, rf, rt) : d[k];
                }
                return node;
              });
              dataMapped = true;
            }
            if (s.type === 'boxplot' && Array.isArray(s.data)) {
              // 箱体颜色在数据项级 itemStyle（mapNode 跳过 data），单独映射：
              // 保留五数概括 value，只翻转填充/描边色，暗底下箱体同步提亮
              m.data = s.data.map(function(d) {
                if (!d || !d.itemStyle) return d;
                return { value: d.value, itemStyle: mapNode(d.itemStyle, tbl, rf, rt) };
              });
              dataMapped = true;
            }
            if (s.type === 'heatmap') {
              m.itemStyle = m.itemStyle || {};
              m.itemStyle.borderColor = isDark ? '#1a1b1e' : '#ffffff';
              m.label = m.label || {};
              m.label.color = isDark ? '#e8eaed' : '#1a1d29';
              // 数据项级预置白字（亮色深格）：暗色色板极值格反转为亮色，
              // 同步翻成深字保证强相关/高值格可读，切回亮色还原白字。
              // 复制整项再改色：label 里可能还有 show/formatter/position
              // （密度图的"最密 N 条"峰值标签），只回写 {value,label} 会
              // 把这些字段一起丢掉，峰值标注切主题后就消失了。
              if (Array.isArray(s.data)) {
                m.data = s.data.map(function(d) {
                  if (d && d.label && d.label.color) {
                    var copy = {};
                    for (var dk in d) {
                      if (Object.prototype.hasOwnProperty.call(d, dk)) copy[dk] = d[dk];
                    }
                    var label = {};
                    for (var lk in d.label) {
                      if (Object.prototype.hasOwnProperty.call(d.label, lk)) label[lk] = d.label[lk];
                    }
                    // 只翻"亮色深格白字"这一种约定（相关性热力图的数据项级预置
                    // 白字）；密度图峰值标签自带白底深红字（两种主题都可读），
                    // 颜色必须原样保留，否则会被翻成白/深蓝压在白色标签底上。
                    if (String(d.label.color).toLowerCase() === '#ffffff') {
                      label.color = isDark ? '#16324a' : '#ffffff';
                    }
                    copy.label = label;
                    return copy;
                  }
                  return d;
                });
                dataMapped = true;
              }
            }
            if (s.type === 'scatter' && s.name === '\u5747\u503c') {
              m.itemStyle = { color: isDark ? '#1a1b1e' : '#ffffff',
                              borderColor: isDark ? '#e8eaed' : '#1a1d29', borderWidth: 1.5 };
            }
            if (s.type === 'scatter') {
              // 快照是初始态，而 datazoom 自适应点径会实时改 symbolSize /
              // itemStyle.opacity——从快照写回会覆盖现场（缩放到 9px 后切
              // 主题被打回初值）。从 update 里剥掉，setOption 合并保留现场。
              // symbolSize 为函数（size 维度）时同样剥掉：省略即不动现场。
              delete m.symbolSize;
              if (m.itemStyle) delete m.itemStyle.opacity;
            }
            if (!dataMapped) delete m.data;
            return m;
          });
        }
        chart.setOption(update);
      } catch (e) { /* noop */ }
    }
    document.documentElement.style.background = isDark ? (window.__pageBgDark || '#1a1b1e') : (window.__pageBgLight || '#ffffff');
    // tooltip 内联说明文字/分隔线：formatter 用 var(--tt-muted)/var(--tt-border) 引用，这里统一切换
    document.documentElement.style.setProperty('--tt-muted', isDark ? '#9aa0a6' : '#6b7280');
    document.documentElement.style.setProperty('--tt-border', isDark ? '#2e2f33' : '#e4e6ea');
    document.body.style.background = isDark ? (window.__pageBgDark || '#1a1b1e') : (window.__pageBgLight || '#ffffff');
    document.body.style.color = isDark ? '#e8eaed' : '#1a1d29';
    var interp = document.querySelector('.interpretation');
    if (interp) {
      interp.style.background = isDark ? '#202124' : '#f9fafb';
      interp.style.color = isDark ? '#bdc1c6' : '#374151';
      interp.style.borderTopColor = isDark ? '#2e2f33' : '#e4e6ea';
    }
    var interpTitle = document.querySelector('.interpretation-title');
    if (interpTitle) {
      interpTitle.style.color = isDark ? '#9aa0a6' : '#6b7280';
    }
    // 同步切换按钮图标与提示
    updateToggleUI(isDark);
  }

  // 独立打开时记忆用户选择（沙箱 iframe 下 localStorage 受限，失败静默降级）。
  try {
    var saved = localStorage.getItem('echarts-theme');
    if (saved === 'dark' || saved === 'light') document.documentElement.dataset.theme = saved;
  } catch (_) { /* noop */ }

  // 切换按钮：默认跟随外层/系统主题，点按后写入 data-theme 并优先于系统偏好。
  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function() {
      var next = getIsDark() ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('echarts-theme', next); } catch (_) { /* noop */ }
      applyTheme();
    });
  }

  setTimeout(applyTheme, 100);
  var observer = new MutationObserver(function() { setTimeout(applyTheme, 50); });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
  // 响应式重绘：监听窗口尺寸变化，防抖 150ms 后调用 chart.resize()，
  // 与模板内已有的 resize 监听并存；这里防抖避免高频拖拽时重复重绘卡顿。
  var _resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function() {
      var cs = window.__echartsInstances || (window.__echartsInstance ? [window.__echartsInstance] : []);
      for (var i = 0; i < cs.length; i++) cs[i].resize();
    }, 150);
  });
  // PNG 导出（postMessage 通道）：父页面发送 {type:"download-png"} 触发导出，
  // 这里调用 chart.getDataURL 生成 dataURL 后回传 {type:"png-data", data}。
  // 使用 postMessage 而非直接下载，可避免 iframe 需要 allow-same-origin 权限。
  // 导出背景跟随当前主题，避免暗色图表配白底导致文字不可读。
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'download-png') {
      var chart = window.__echartsInstance;
      if (chart) {
        var url = chart.getDataURL({type: 'png', pixelRatio: 2, backgroundColor: getIsDark() ? '#1a1b1e' : '#fff'});
        parent.postMessage({type: 'png-data', data: url}, '*');
      }
    }
  });
})();
</script>"""

_ECHARTS_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script src="{script}"></script>{extra_script}
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: 100%; height: 100%; background: {bg}; font-family: {font}; color: {text}; overflow: hidden; }}
  /* 图表区域自适应容器尺寸：去掉 min-height: 560px 强制撑高，
     改为 min-height: 320px 保证小屏可读，高度 100% 填满预览模态。
     这样预览模态（840px 高）里图表会完整显示，不再溢出看不见。 */
  #chart {{ width: 100%; height: 100%; min-height: 320px; }}
  .interpretation {{
    border-top: 1px solid #e4e6ea;
    padding: 16px 24px;
    background: #f9fafb;
    font-size: 13px;
    line-height: 1.75;
    color: #374151;
    max-height: 180px;
    overflow-y: auto;
  }}
  .interpretation-title {{
    font-size: 12px;
    color: #6b7280;
    font-weight: 600;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }}
  .layout {{ display: flex; flex-direction: column; height: 100%; }}
  .chart-wrap {{ flex: 1; min-height: 0; position: relative; }}
  /* 亮/暗主题切换按钮：悬浮于图表右上角（让出 toolbox 的 right:56 区域），
     自身配色随主题切换，点击后写入 <html data-theme> 触发图表重绘。 */
  .theme-toggle {{
    position: absolute; top: 12px; right: 12px; z-index: 30;
    width: 34px; height: 34px; border-radius: 9px;
    border: 1px solid #e4e6ea; background: rgba(255,255,255,0.92);
    color: #1a1d29; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    transition: background .15s, border-color .15s, transform .1s;
    -webkit-appearance: none; appearance: none; padding: 0;
  }}
  .theme-toggle:hover {{ transform: translateY(-1px); border-color: #c7ccd4; }}
  .theme-toggle:active {{ transform: translateY(0); }}
  .theme-toggle svg {{ display: block; }}
  html[data-theme='dark'] .theme-toggle {{
    background: rgba(26,27,30,0.92); border-color: #2e2f33; color: #e8eaed;
  }}
</style>
</head>
<body>
<div class="layout">
  <div class="chart-wrap">
    <button id="theme-toggle" class="theme-toggle" type="button" aria-label="切换主题" title="切换到暗色">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
    <div id="chart"></div>
  </div>
  {interpretation_block}
</div>
<script>
(function(){{
  var el = document.getElementById('chart');
  var chart = echarts.init(el, null, {{renderer: 'canvas', devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2)}});
  var option = {option};
  chart.setOption(option, true);
  // 大数据散点自适应点径：dataZoom 放大后全量的 4px/50% 透明度点阵
  // 太淡，点径与不透明度随缩放倍数提升；回到全量恢复彩色点阵混合
  // （与 Plotly 分支的缩放自适应语义一致）。
  var _scatterSeries = (option.series || []).filter(function(s) {{
    return s && s.type === 'scatter' && typeof s.symbolSize === 'number';
  }});
  // 双轴 dataZoom 的显式 id 归因表（dz-x/dz-y，Python 端写入）。
  // 同时充当门控：只有散点图带显式 id，箱线图离群点等同为 scatter
  // 数值 symbolSize 的系列不参与散点的点径自适应（缩放时保持
  // 设计点径 7/9，不被改写成 4-9px）。
  var _dzAxis = {{}};
  (option.dataZoom || []).forEach(function (d) {{
    if (!d || !d.id) return;
    _dzAxis[d.id] = (d.yAxisIndex != null && d.xAxisIndex == null) ? 'y' : 'x';
  }});
  if (_scatterSeries.length && Object.keys(_dzAxis).length) {{
    // 每次 datazoom 直接微调点径（setOption 增量更新开销小，delta
    // 跳过保证封顶/回底后零操作），避免"缩放停止后跳变"的突跳感。
    // 缩放倍数只从事件 payload 增量记账（inside 走 e.batch，滑条走顶层
    // start/end），不调 chart.getOption()——它深拷贝整个 option（30 万点
    // 散点的 series 数据可达几十 MB），滚轮缩放每个事件一次就是持续卡顿
    // 源。双轴 dataZoom 按显式 id（dz-x/dz-y）分别记账，取缩放更深的轴
    // （与 Plotly 分支 max(x,y) 因子语义一致；此前只读 dz[0]，缩放 Y 轴
    // 点径不更新）。无 id 的组件（其他图型的 dataZoom）归入 x 轴，
    // 与旧版单 dataZoom 行为等价。
    var _lastSize = 0;
    var _spans = {{ x: 100, y: 100 }};
    var _applyAdaptiveSize = function () {{
      var zoom = 100 / Math.min(_spans.x, _spans.y);
      var size = Math.min(9, Math.max(4, 4 * Math.sqrt(zoom)));
      var opacity = Math.min(0.9, Math.max(0.5, 0.5 * Math.pow(zoom, 0.2)));
      if (Math.abs(size - _lastSize) < 0.25) return;
      _lastSize = size;
      var updates = (option.series || []).map(function (s) {{
        if (s && s.type === 'scatter' && typeof s.symbolSize === 'number') {{
          return {{ symbolSize: size, itemStyle: {{ opacity: opacity }} }};
        }}
        return {{}};
      }});
      chart.setOption({{ series: updates }});
    }};
    chart.on('datazoom', function (e) {{
      var batch = e.batch || (e.start != null || e.end != null
        ? [{{ dataZoomId: e.dataZoomId, start: e.start, end: e.end }}] : []);
      for (var _bi = 0; _bi < batch.length; _bi++) {{
        var _it = batch[_bi];
        if (!_it || _it.start == null || _it.end == null) continue;
        _spans[_dzAxis[_it.dataZoomId] || 'x'] = Math.max(1e-6, _it.end - _it.start);
      }}
      _applyAdaptiveSize();
    }});
    // toolbox「重置」回到全量视图：恢复双轴跨度并回底点径（restore 后
    // datazoom 事件不一定触发，显式监听保证点径一定回底）。
    chart.on('restore', function () {{
      _spans.x = 100;
      _spans.y = 100;
      _applyAdaptiveSize();
    }});
  }}
  window.addEventListener('resize', function(){{ chart.resize(); }});
  // 主题切换：监听 prefers-color-scheme（暂只渲染浅色，预留深色扩展点）
  // 密度图的档位色板挂在顶层非标准键 densityBands 上：getOption() 不保证
  // 保留非标准键，这里顺手挂到实例上供换肤脚本取用（非密度图无此键，跳过）。
  if (option.densityBands) chart.__densityBands = option.densityBands;
  window.__echartsInstance = chart;
}})();
</script>
{dark_script}
</body>
</html>
"""

