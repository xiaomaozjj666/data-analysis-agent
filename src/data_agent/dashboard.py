"""数据画像仪表盘：KPI 指标卡 + 数据质量告警 + 全部图表的自包含 HTML 导出。

设计参照"单文件仪表盘"最佳实践并对齐本项目视觉体系：
- KPI 卡片：行/列规模、缺失率、重复行、质量问题数、图表产物数，一眼看到核心结论；
- 质量告警卡：缺失 / 重复行 / 主键冲突 / 极端离群 / 负值疑点 / 常量列，
  每张卡带定位目标与修复建议；
- 图表区：复用已生成图表的 ``.echarts.json`` / ``.plotly.json`` 数据文件原地重渲染，
  ECharts 图共享一份 bundle（而非逐图内联），Plotly 图按需附带；
- 亮暗双主题：页面骨架用 CSS 变量翻转，图表复用 ``_ECHARTS_DARK_MODE_SCRIPT``
  的多实例模式（``window.__echartsInstances``）与 Plotly relayout 适配脚本，
  与单图 HTML 的主题行为完全一致（含 localStorage 记忆）。

导出产物是单一 HTML：离线可开、可直接转发，无服务端依赖。
"""

from __future__ import annotations

import json
import re
import time
from html import escape
from typing import Any

import pandas as pd

from data_agent.echarts_engine import (
    _ECHARTS_DARK_MODE_SCRIPT,
    _ECHARTS_FONT_FAMILY,
    _JsFunction,
    _serialize_option,
)
from data_agent.workspace import (
    ECHARTS_CDN_URL,
    ECHARTS_GL_CDN_URL,
    DataWorkspace,
)

# 质量告警上限：超出后聚合成一张"其余问题"卡，避免告警区淹没图表区
_MAX_ISSUE_CARDS = 12

#: 疑似主键/编号列名（中英文），用于唯一性检查
_ID_LIKE_PATTERN = re.compile(r"(?i)(?:^|[_\s])(?:id|no|code)$|id$|编号$|单号$|序号$")


# === KPI 指标计算 ===

def compute_kpis(df: pd.DataFrame, *, chart_count: int, issue_count: int) -> list[dict[str, str]]:
    """基于当前工作区数据计算 KPI 卡片：label / val / sub / cls（语义色）。"""
    rows, cols = df.shape
    numeric_cols = df.select_dtypes(include="number").shape[1]
    datetime_cols = df.select_dtypes(include=["datetime", "datetimetz"]).shape[1]
    other_cols = cols - numeric_cols - datetime_cols
    total_cells = rows * cols
    missing = int(df.isna().sum().sum())
    missing_pct = (missing / total_cells * 100) if total_cells else 0.0
    dup_rows = int(df.duplicated().sum())

    return [
        {"label": "记录数", "val": f"{rows:,}", "sub": f"{cols} 个字段", "cls": "blue"},
        {"label": "字段构成", "val": str(cols),
         "sub": f"数值 {numeric_cols} · 时间 {datetime_cols} · 类别/文本 {other_cols}", "cls": ""},
        {"label": "缺失单元格", "val": f"{missing_pct:.1f}%",
         "sub": f"{missing:,} / {total_cells:,} 个", "cls": "amber" if missing else "green"},
        {"label": "重复行", "val": f"{dup_rows:,}",
         "sub": "完全重复的记录" if dup_rows else "未发现完全重复", "cls": "red" if dup_rows else "green"},
        {"label": "数据质量问题", "val": f"{issue_count} 项",
         "sub": "详见下方告警区" if issue_count else "未发现明显问题", "cls": "red" if issue_count else "green"},
        {"label": "图表产物", "val": str(chart_count), "sub": "交互式图表（亮暗双主题）", "cls": "blue"},
    ]


# === 数据质量剖析 ===

def profile_quality(df: pd.DataFrame) -> list[dict[str, str]]:
    """结构化数据质量剖析：每项 {tag, level(high/mid/low), target, desc, fix}。

    只做通用、可靠的检测（缺失/重复/唯一性/离群/负值疑点/常量列），
    不猜测业务语义；启发式判定均在描述里说明依据，保持诚实。
    """
    issues: list[dict[str, str]] = []
    rows = len(df)
    if rows == 0:
        return issues

    # 1) 缺失值：按缺失率降序，前 4 列单独成卡，其余聚合
    missing = df.isna().sum()
    missing = missing[missing > 0].sort_values(ascending=False)
    for col, cnt in list(missing.items())[:4]:
        pct = cnt / rows * 100
        issues.append({
            "tag": "缺失", "level": "high" if pct >= 20 else "mid", "target": str(col),
            "desc": f"「{col}」缺失 {int(cnt):,} 个（{pct:.1f}%），相关统计的有效样本降为 {rows - int(cnt):,} 条。",
            "fix": "补录数据，或在聚合口径中显式排除/归入「未知」分组，避免口径不一致。",
        })
    if len(missing) > 4:
        rest = ", ".join(str(c) for c in list(missing.index)[4:9])
        issues.append({
            "tag": "缺失", "level": "low", "target": f"另 {len(missing) - 4} 列",
            "desc": f"其余存在缺失的列：{rest}{'…' if len(missing) > 9 else ''}。",
            "fix": "按列重要性逐一确认缺失原因（未采集 / 不适用 / 录入遗漏）。",
        })

    # 2) 完全重复行
    dup_rows = int(df.duplicated().sum())
    if dup_rows:
        issues.append({
            "tag": "重复行", "level": "mid", "target": f"{dup_rows} 条",
            "desc": f"存在 {dup_rows:,} 条与其他记录完全相同的行，直接聚合会重复计数。",
            "fix": "确认是否为重复导入；若是则去重后再做统计。",
        })

    # 3) 疑似主键列唯一性：列名形如 id/编号/单号 且存在重复值
    for col in df.columns:
        if not _ID_LIKE_PATTERN.search(str(col)):
            continue
        non_null = df[col].dropna()
        dup_keys = int(len(non_null) - non_null.nunique())
        if dup_keys > 0:
            sample = non_null[non_null.duplicated()].astype(str).unique()[:3]
            issues.append({
                "tag": "主键冲突", "level": "high", "target": str(col),
                "desc": f"「{col}」按列名疑似唯一标识，但有 {dup_keys} 个重复值（如 {', '.join(sample)}），唯一性不成立。",
                "fix": "核实重复键是否为两条独立业务记录；统计唯一实体数时应先按该列去重。",
            })

    # 4) 极端离群：数值列超出 3 倍四分位距（比常规 1.5 倍更严格，只报真正极端的）
    numeric = df.select_dtypes(include="number")
    outlier_cards = 0
    for col in numeric.columns:
        vals = numeric[col].dropna()
        if len(vals) < 8 or outlier_cards >= 3:
            continue
        q1, q3 = vals.quantile(0.25), vals.quantile(0.75)
        iqr = float(q3 - q1)
        if iqr <= 0:
            continue
        extreme = vals[(vals < q1 - 3 * iqr) | (vals > q3 + 3 * iqr)]
        if len(extreme):
            worst = extreme.iloc[extreme.abs().argmax()]
            issues.append({
                "tag": "极端离群", "level": "high", "target": str(col),
                "desc": f"「{col}」有 {len(extreme)} 个极端离群值（超出 3 倍四分位距），最极端为 {worst:,.4g}，会显著拉偏均值与合计。",
                "fix": "核实是否为录入错误或测试数据；聚合分析建议剔除或截尾（可用数据清洗工具的 IQR 处理）。",
            })
            outlier_cards += 1

    # 5) 负值疑点：数值列 95% 以上为非负、仅少量负值时提示（不假定业务语义）
    for col in numeric.columns:
        vals = numeric[col].dropna()
        if len(vals) < 8:
            continue
        neg = int((vals < 0).sum())
        if 0 < neg <= max(1, int(len(vals) * 0.05)):
            issues.append({
                "tag": "负值疑点", "level": "mid", "target": str(col),
                "desc": f"「{col}」{len(vals) - neg:,} 个值非负，仅 {neg} 个为负，疑似录入错误或冲销/退款记录。",
                "fix": "确认负值业务含义；若为冲销请单独口径统计，若为错误请修正。",
            })

    # 6) 常量列：全列只有一个取值，对分析无信息量
    constant_cols = [str(c) for c in df.columns if df[c].dropna().nunique() == 1]
    if constant_cols:
        issues.append({
            "tag": "常量列", "level": "low", "target": f"{len(constant_cols)} 列",
            "desc": f"以下列所有记录取值相同，无区分度：{', '.join(constant_cols[:6])}{'…' if len(constant_cols) > 6 else ''}。",
            "fix": "确认是否为筛选残留或冗余字段，可从分析维度中移除。",
        })

    if len(issues) > _MAX_ISSUE_CARDS:
        overflow = len(issues) - (_MAX_ISSUE_CARDS - 1)
        issues = issues[:_MAX_ISSUE_CARDS - 1] + [{
            "tag": "更多", "level": "low", "target": f"另 {overflow} 项",
            "desc": "其余问题从略，建议在对话中让 Agent 逐项展开数据质量分析。",
            "fix": "使用数据清洗工具按列处理，或缩小数据范围后重新剖析。",
        }]
    return issues


# === 图表收集 ===

def _rehydrate_js(node: Any) -> Any:
    """把 ``.echarts.json`` 里以字符串形态存档的 JS 函数还原为 _JsFunction，
    使 ``_serialize_option`` 重新序列化时能去引号内联（与原图 HTML 行为一致）。"""
    if isinstance(node, str):
        s = node.strip()
        if s.startswith("function(") and s.endswith("}"):
            return _JsFunction(s)
        return node
    if isinstance(node, dict):
        return {k: _rehydrate_js(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_rehydrate_js(v) for v in node]
    return node


def _collect_charts(workspace: DataWorkspace) -> list[dict[str, Any]]:
    """按注册顺序收集图表：优先 ECharts 数据文件，回退 Plotly 数据文件。

    没有数据 sidecar 的图表（历史产物）无法原地重渲染，跳过并保持诚实
    （仪表盘元信息里只统计纳入的图表数）。
    """
    charts: list[dict[str, Any]] = []
    for item in workspace.artifacts:
        if item.get("kind") != "visualization" or not item["name"].endswith(".html"):
            continue
        stem = item["name"][: -len(".html")]
        title = item.get("description") or stem
        echarts_json = workspace.artifacts_dir / f"{stem}.echarts.json"
        plotly_json = workspace.artifacts_dir / f"{stem}.plotly.json"
        try:
            if echarts_json.is_file():
                option = json.loads(echarts_json.read_text(encoding="utf-8"))
                charts.append({"engine": "echarts", "title": title, "option": option})
            elif plotly_json.is_file():
                fig = json.loads(plotly_json.read_text(encoding="utf-8"))
                charts.append({"engine": "plotly", "title": title, "fig": fig})
        except (OSError, ValueError):
            continue  # 单张图数据损坏不拖垮整个仪表盘
    return charts


def _is_wide_chart(chart: dict[str, Any]) -> bool:
    """SPLOM（多 grid）与 3D 散点占满整行，其余图表半宽双列。"""
    if chart["engine"] != "echarts":
        return False
    option = chart["option"]
    grids = option.get("grid")
    if isinstance(grids, list) and len(grids) > 4:
        return True
    return any(s.get("type") == "scatter3D" for s in option.get("series", []))


# === HTML 组装 ===

# 页面骨架样式：亮色值与 ECharts 常量一致，暗色值与前端 tokens.css 一致；
# html[data-theme] 显式优先，未指定时跟随系统偏好（与图表脚本 getIsDark 同序）。
_DASH_CSS = """
  :root{
    --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d29; --ink2:#6b7280;
    --line:#e4e6ea; --accent:#2c5f8d; --warn:#c75d63; --good:#4f9d7c; --amber:#b8860b;
    --shadow:0 2px 12px rgba(26,29,41,.06);
  }
  html[data-theme='dark']{
    --bg:#131417; --card:#1a1b1e; --ink:#e8eaed; --ink2:#9aa0a6;
    --line:#2e2f33; --accent:#6fa8d6; --warn:#e08a8f; --good:#6fbf9c; --amber:#e0bc6a;
    --shadow:0 2px 12px rgba(0,0,0,.35);
  }
  @media (prefers-color-scheme: dark){
    html:not([data-theme='light']){
      --bg:#131417; --card:#1a1b1e; --ink:#e8eaed; --ink2:#9aa0a6;
      --line:#2e2f33; --accent:#6fa8d6; --warn:#e08a8f; --good:#6fbf9c; --amber:#e0bc6a;
      --shadow:0 2px 12px rgba(0,0,0,.35);
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);line-height:1.5;
    -webkit-font-smoothing:antialiased}
  .wrap{max-width:1280px;margin:0 auto;padding:24px 20px 48px}
  header.top h1{font-size:24px;margin:0 0 6px;font-weight:700;letter-spacing:.5px}
  header.top .meta{color:var(--ink2);font-size:13px}
  header.top .meta b{color:var(--ink)}
  .grid-kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:14px;margin:18px 0 22px}
  .kpi{background:var(--card);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow);
    border:1px solid var(--line)}
  .kpi .label{font-size:12.5px;color:var(--ink2);margin-bottom:8px}
  .kpi .val{font-size:26px;font-weight:700;letter-spacing:.3px}
  .kpi .sub{font-size:11.5px;color:var(--ink2);margin-top:4px}
  .kpi.red .val{color:var(--warn)} .kpi.blue .val{color:var(--accent)}
  .kpi.green .val{color:var(--good)} .kpi.amber .val{color:var(--amber)}
  .section-title{font-size:17px;font-weight:700;margin:30px 0 14px;
    padding-left:10px;border-left:4px solid var(--accent)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}
  .card{background:var(--card);border-radius:12px;padding:14px 16px 10px;
    box-shadow:var(--shadow);border:1px solid var(--line)}
  .card.full{grid-column:1/-1}
  .card h3{margin:2px 0 8px;font-size:15px;font-weight:600}
  .chart{width:100%;height:380px}
  .card.full .chart{height:520px}
  .qa-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
  .qa{background:var(--card);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow);
    border:1px solid var(--line);border-left:4px solid var(--warn)}
  .qa.mid{border-left-color:var(--amber)} .qa.low{border-left-color:var(--accent)}
  .qa .q-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .qa .tag{font-size:11px;font-weight:700;color:#fff;background:var(--warn);
    padding:2px 8px;border-radius:6px}
  .qa.mid .tag{background:var(--amber)} .qa.low .tag{background:var(--accent)}
  .qa .rid{font-size:13px;font-weight:700;color:var(--ink)}
  .qa .desc{font-size:13px;color:var(--ink2)}
  .qa .fix{font-size:12px;color:var(--good);margin-top:6px}
  .empty{color:var(--ink2);font-size:13px;padding:8px 2px}
  footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
    font-size:12px;color:var(--ink2);line-height:1.7}
  footer b{color:var(--ink)}
  .theme-toggle{
    position:fixed;top:16px;right:16px;z-index:30;
    width:36px;height:36px;border-radius:10px;
    border:1px solid var(--line);background:var(--card);color:var(--ink);
    cursor:pointer;display:flex;align-items:center;justify-content:center;
    box-shadow:var(--shadow);transition:transform .1s;padding:0;
    -webkit-appearance:none;appearance:none}
  .theme-toggle:hover{transform:translateY(-1px)}
  .theme-toggle svg{display:block}
"""

# Plotly 图表主题适配 + 响应式重绘（多实例）；配色与页面卡片同步：
# 图表纸面直接用卡片底色，网格线用分隔线令牌，避免卡片内出现"色块补丁"。
_DASH_PLOTLY_SCRIPT = """<script>
(function() {
  function isDark() {
    var t = document.documentElement.dataset.theme;
    if (t === 'dark') return true;
    if (t === 'light') return false;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function apply() {
    if (!window.Plotly || !window.__plotlyDivs || !window.__plotlyDivs.length) return;
    var d = isDark();
    var up = {
      'paper_bgcolor': d ? '#1a1b1e' : '#ffffff',
      'plot_bgcolor': d ? '#1a1b1e' : '#ffffff',
      'font.color': d ? '#e8eaed' : '#1a1d29',
      'xaxis.gridcolor': d ? '#2e2f33' : '#eef0f3',
      'yaxis.gridcolor': d ? '#2e2f33' : '#eef0f3',
      'xaxis.zerolinecolor': d ? '#3a3b40' : '#e4e6ea',
      'yaxis.zerolinecolor': d ? '#3a3b40' : '#e4e6ea'
    };
    for (var i = 0; i < window.__plotlyDivs.length; i++) {
      try { Plotly.relayout(window.__plotlyDivs[i], up); } catch (_) {}
    }
  }
  setTimeout(apply, 120);
  new MutationObserver(function() { setTimeout(apply, 60); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', apply);
  var t;
  window.addEventListener('resize', function() {
    clearTimeout(t);
    t = setTimeout(function() {
      for (var i = 0; i < (window.__plotlyDivs || []).length; i++) {
        try { Plotly.Plots.resize(window.__plotlyDivs[i]); } catch (_) {}
      }
    }, 150);
  });
})();
</script>"""


def _bundle_script_tag(workspace: DataWorkspace, kind: str) -> str:
    """内联对应引擎的 JS bundle；文件不可得时回退 CDN 直引（在线可用）。"""
    if kind == "echarts":
        path, cdn = workspace.ensure_echarts_bundle(), ECHARTS_CDN_URL
    elif kind == "echarts-gl":
        path, cdn = workspace.ensure_echarts_gl_bundle(), ECHARTS_GL_CDN_URL
    else:  # plotly：本地包必然可得，仅极端情况下退 CDN
        path, cdn = workspace.ensure_plotly_bundle(), "https://cdn.plot.ly/plotly-2.35.2.min.js"
    if path is not None:
        try:
            return f"<script>{path.read_text(encoding='utf-8')}</script>"
        except OSError:
            pass
    return f'<script src="{cdn}"></script>'


def build_dashboard_html(workspace: DataWorkspace) -> str:
    """组装数据画像仪表盘（自包含 HTML）。调用方需保证已加载数据集。"""
    df = workspace.dataframe  # 未加载时抛 RuntimeError，由路由层转 404
    charts = _collect_charts(workspace)
    issues = profile_quality(df)
    kpis = compute_kpis(df, chart_count=len(charts), issue_count=len(issues))

    # --- KPI 卡片 ---
    kpi_html = "".join(
        f'<div class="kpi {k["cls"]}"><div class="label">{escape(k["label"])}</div>'
        f'<div class="val">{escape(k["val"])}</div><div class="sub">{escape(k["sub"])}</div></div>'
        for k in kpis
    )

    # --- 质量告警卡片 ---
    if issues:
        qa_html = "".join(
            f'<div class="qa {i["level"]}"><div class="q-head"><span class="tag">{escape(i["tag"])}</span>'
            f'<span class="rid">{escape(i["target"])}</span></div>'
            f'<div class="desc">{escape(i["desc"])}</div>'
            f'<div class="fix">建议：{escape(i["fix"])}</div></div>'
            for i in issues
        )
    else:
        qa_html = '<div class="empty">未发现明显数据质量问题（缺失 / 重复 / 唯一性 / 离群 / 负值 / 常量列检查全部通过）。</div>'

    # --- 图表卡片 + 初始化脚本 ---
    chart_cards: list[str] = []
    init_scripts: list[str] = []
    has_echarts = has_gl = has_plotly = False
    for idx, chart in enumerate(charts):
        el_id = f"chart-{idx}"
        wide = ' full' if _is_wide_chart(chart) else ''
        chart_cards.append(
            f'<div class="card{wide}"><h3>{escape(chart["title"])}</h3>'
            f'<div id="{el_id}" class="chart"></div></div>'
        )
        if chart["engine"] == "echarts":
            has_echarts = True
            option = chart["option"]
            if any(s.get("type") == "scatter3D" for s in option.get("series", [])):
                has_gl = True
            option_js = _serialize_option(_rehydrate_js(option))
            init_scripts.append(
                "<script>(function(){"
                f"var el=document.getElementById('{el_id}');"
                "var c=echarts.init(el,null,{renderer:'canvas',"
                "devicePixelRatio:Math.min(window.devicePixelRatio||1,2)});"
                f"c.setOption({option_js},true);"
                "window.__echartsInstances.push(c);})();</script>"
            )
        else:
            has_plotly = True
            fig_js = json.dumps(chart["fig"], ensure_ascii=False).replace("</script>", "<\\/script>")
            # 去掉存档里的固定宽高，让图表自适应卡片尺寸
            init_scripts.append(
                "<script>(function(){"
                f"var fig={fig_js};var l=fig.layout||{{}};"
                "delete l.width;delete l.height;l.autosize=true;"
                f"Plotly.newPlot('{el_id}',fig.data||[],l,"
                "{responsive:true,displaylogo:false,modeBarButtonsToRemove:['lasso2d','select2d']});"
                f"window.__plotlyDivs.push(document.getElementById('{el_id}'));"
                "})();</script>"
            )

    charts_html = (
        f'<div class="grid">{"".join(chart_cards)}</div>' if chart_cards
        else '<div class="empty">当前会话尚无图表产物；在对话中让 Agent 生成图表后重新导出即可。</div>'
    )

    bundle_tags = ""
    if has_echarts:
        bundle_tags += _bundle_script_tag(workspace, "echarts")
    if has_gl:
        bundle_tags += _bundle_script_tag(workspace, "echarts-gl")
    if has_plotly:
        bundle_tags += _bundle_script_tag(workspace, "plotly")

    # --- 元信息与口径说明 ---
    rows, cols = df.shape
    input_names = sorted(p.name for p in workspace.input_dir.glob("*") if p.is_file())
    source = escape("、".join(input_names)) if input_names else "当前工作区数据"
    generated_at = time.strftime("%Y-%m-%d %H:%M")
    meta = (
        f'数据 <b>{rows:,}</b> 行 × <b>{cols}</b> 列 · 来源：<b>{source}</b>'
        f' · 生成于 <b>{generated_at}</b> · 图表 <b>{len(charts)}</b> 张'
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据画像仪表盘</title>
{bundle_tags}
<style>
  body{{font-family:{_ECHARTS_FONT_FAMILY};}}
{_DASH_CSS}
</style>
</head>
<body>
<button id="theme-toggle" class="theme-toggle" type="button" aria-label="切换主题" title="切换主题">
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
</button>
<div class="wrap">
  <header class="top">
    <h1>数据画像仪表盘</h1>
    <div class="meta">{meta}</div>
  </header>
  <div class="grid-kpi">{kpi_html}</div>
  <div class="section-title">图表总览（交互式 · 右上角切换亮暗主题）</div>
  {charts_html}
  <div class="section-title">数据质量告警（共 {len(issues)} 项）</div>
  <div class="qa-grid">{qa_html}</div>
  <footer>
    <b>口径说明：</b>KPI 与质量告警基于导出时刻的工作区数据实时计算（含已执行的清洗步骤）；
    图表复用生成时的数据快照，若导出前又做过清洗，个别图表与 KPI 可能存在口径差异，以图表副标题标注为准。
    <br>本仪表盘由数据智能分析 Agent 自动生成 · 单一自包含 HTML，离线可打开、可直接转发。
  </footer>
</div>
<script>window.__echartsInstances=[];window.__plotlyDivs=[];
window.__pageBgLight='#f6f7f9';window.__pageBgDark='#131417';</script>
{"".join(init_scripts)}
<script>if(window.__echartsInstances.length)window.__echartsInstance=window.__echartsInstances[0];</script>
{_ECHARTS_DARK_MODE_SCRIPT}
{_DASH_PLOTLY_SCRIPT if has_plotly else ""}
</body>
</html>
"""
