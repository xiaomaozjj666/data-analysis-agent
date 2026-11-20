"""把演示预分析落成真实图表产物。

预分析此前只在页面里渲染，历史会话的「分析产物」是 0。这里用 Plotly 把每个
chart 规格生成一份自包含 HTML，写入 workspace/artifacts 并注册为
visualization，产物页、预览和会话统计都能看到。

图表类型覆盖：折线、多序列、面积、堆叠、条形、直方图、散点、气泡、占比环、
热力、树图、漏斗、雷达、仪表、箱线、瀑布。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go

from data_agent.workspace import DataWorkspace

_SERIES = ["#18181b", "#0d9488", "#71717a", "#a1a1aa"]
_DANGER = "#b91c1c"
_POSITIVE = "#0d9488"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-一-鿿]", "_", name)[:40]


def _figure(chart: dict[str, Any]) -> go.Figure:
    kind = chart.get("type")
    labels = chart.get("labels") or []
    items = chart.get("items") or []
    series = chart.get("series") or []
    fig = go.Figure()

    if kind in {"line", "area"}:
        points = chart.get("points") or []
        fig.add_trace(
            go.Scatter(
                x=[point["label"] for point in points],
                y=[point["value"] for point in points],
                mode="lines+markers",
                fill="tozeroy" if kind == "area" else None,
                line={"width": 2, "color": _SERIES[0]},
            )
        )
    elif kind == "multiline":
        for index, item in enumerate(series):
            fig.add_trace(
                go.Scatter(
                    x=labels,
                    y=item.get("points") or item.get("values") or [],
                    mode="lines+markers",
                    name=item.get("name"),
                    line={
                        "width": 2,
                        "color": _SERIES[index % len(_SERIES)],
                        "dash": "dash" if item.get("dash") else "solid",
                    },
                )
            )
    elif kind == "stacked":
        for index, item in enumerate(series):
            fig.add_trace(
                go.Bar(
                    x=labels,
                    y=item.get("values") or [],
                    name=item.get("name"),
                    marker_color=_SERIES[index % len(_SERIES)],
                )
            )
        fig.update_layout(barmode="stack")
    elif kind in {"bar", "histogram"}:
        fig.add_trace(
            go.Bar(
                x=[item["label"] for item in items],
                y=[item["value"] for item in items],
                marker_color=_SERIES[0],
            )
        )
    elif kind == "scatter":
        samples = chart.get("samples") or []
        outliers = chart.get("outliers") or []
        fig.add_trace(
            go.Scatter(
                x=[point[0] for point in samples],
                y=[point[1] for point in samples],
                mode="markers",
                marker={"size": 5, "color": _SERIES[1], "opacity": 0.55},
                name="主体",
            )
        )
        if outliers:
            fig.add_trace(
                go.Scatter(
                    x=[point[0] for point in outliers],
                    y=[point[1] for point in outliers],
                    mode="markers",
                    marker={"size": 8, "color": _DANGER},
                    name="离群点",
                )
            )
    elif kind == "bubble":
        xs = [float(item.get("x") or 0) for item in items]
        ys = [float(item.get("y") or 0) for item in items]
        sizes = [float(item.get("size") or 1) for item in items]
        peak = max(sizes) or 1
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text",
                text=[item["label"] for item in items],
                textposition="middle center",
                marker={
                    "size": [10 + (size / peak) * 40 for size in sizes],
                    "color": [_SERIES[index % len(_SERIES)] for index in range(len(items))],
                    "opacity": 0.75,
                },
            )
        )
    elif kind == "donut":
        fig.add_trace(
            go.Pie(
                labels=[item["label"] for item in items],
                values=[item["value"] for item in items],
                hole=0.55,
                marker={"colors": _SERIES},
            )
        )
    elif kind == "heatmap":
        fig.add_trace(
            go.Heatmap(
                z=chart.get("values") or [],
                colorscale=[[0, "#ffffff"], [1, "#18181b"]],
                showscale=False,
            )
        )
    elif kind == "treemap":
        fig.add_trace(
            go.Treemap(
                labels=[item["label"] for item in items],
                parents=[item.get("group") or "" for item in items],
                values=[item["value"] for item in items],
                marker={"colors": _SERIES},
            )
        )
    elif kind == "funnel":
        fig.add_trace(
            go.Funnel(
                y=[item["label"] for item in items],
                x=[item["value"] for item in items],
                marker={"color": _SERIES},
            )
        )
    elif kind == "radar":
        for index, item in enumerate(series):
            fig.add_trace(
                go.Scatterpolar(
                    r=item.get("values") or [],
                    theta=chart.get("indicators") or [],
                    fill="toself",
                    name=item.get("name"),
                    line={"color": _SERIES[index % len(_SERIES)]},
                )
            )
    elif kind == "gauge":
        value = float(chart.get("value") or 0)
        target = float(chart.get("target") or 0)
        fig.add_trace(
            go.Indicator(
                mode="gauge+number",
                value=value,
                number={"suffix": chart.get("unit") or ""},
                gauge={
                    "axis": {"range": [0, max(value, target) * 1.15 or 1]},
                    "bar": {"color": _SERIES[0]},
                    "threshold": {
                        "line": {"color": _DANGER, "width": 2},
                        "value": target,
                    },
                },
            )
        )
    elif kind == "box":
        groups = chart.get("groups") or []
        for group in groups:
            fig.add_trace(
                go.Box(
                    q1=[group["q1"]],
                    median=[group["median"]],
                    q3=[group["q3"]],
                    lowerfence=[group["min"]],
                    upperfence=[group["max"]],
                    name=group["label"],
                    marker_color=_SERIES[0],
                )
            )
    elif kind == "waterfall":
        fig.add_trace(
            go.Waterfall(
                x=[item["label"] for item in items],
                y=[item["value"] for item in items],
                measure=["relative" for _ in items],
                increasing={"marker": {"color": _POSITIVE}},
                decreasing={"marker": {"color": _DANGER}},
            )
        )
    else:
        points = chart.get("points") or []
        fig.add_trace(
            go.Scatter(
                x=[point["label"] for point in points],
                y=[point["value"] for point in points],
                mode="lines+markers",
            )
        )

    fig.update_layout(
        title={"text": chart.get("title") or "预分析", "font": {"size": 16}},
        template="plotly_white",
        height=420,
        margin={"l": 48, "r": 24, "t": 56, "b": 48},
        colorway=_SERIES,
        font={"family": "system-ui, -apple-system, 'Segoe UI', sans-serif"},
        showlegend=kind in {"multiline", "stacked", "area", "radar", "scatter", "box"},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
    )
    fig.update_xaxes(showgrid=False, linecolor="#e4e4e7")
    fig.update_yaxes(gridcolor="#f1f1f2", zeroline=False)
    return fig


def write_demo_chart_artifacts(
    workspace: DataWorkspace, insights: dict[str, Any] | None
) -> list[Path]:
    """把一份演示的图表写成 artifacts；返回写入的文件路径。"""
    if not insights:
        return []
    written: list[Path] = []
    bundle = workspace.artifacts_dir / "plotly.min.js"
    if not bundle.is_file():
        source = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        if source.is_file():
            shutil.copyfile(source, bundle)
    for index, chart in enumerate(insights.get("charts") or [], start=1):
        try:
            figure = _figure(chart)
            html = figure.to_html(
                full_html=True,
                include_plotlyjs=False,
                config={"displayModeBar": False, "responsive": True},
            )
            html = html.replace(
                "</head>",
                '<script src="plotly.min.js"></script></head>',
                1,
            )
        except Exception:  # noqa: BLE001 - 单个图失败不影响其它产物
            continue
        name = f"预分析-{index:02d}-{_safe(str(chart.get('title') or '图表'))}.html"
        path = workspace.artifacts_dir / name
        path.write_text(html, encoding="utf-8")
        # Plotly sidecar：产物中心据此识别引擎并生成缩略图。
        path.with_name(path.stem + ".plotly.json").write_text(
            figure.to_json(), encoding="utf-8"
        )
        workspace.register_artifact(
            path,
            "visualization",
            f"预分析 · {chart.get('title')}（自动生成，未调用模型）",
        )
        written.append(path)
    return written
