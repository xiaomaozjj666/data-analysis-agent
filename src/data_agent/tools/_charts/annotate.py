"""图表标注辅助：标题附注与极值标注。

从 ``charts.py`` 拆分而来，均为对 ``plotly.graph_objects.Figure`` 的 best-effort
装饰，不修改数据本身。
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go


def _append_title_note(fig: go.Figure, note: str) -> None:
    """Append a compact factual note without nesting Plotly title markup."""
    current = fig.layout.title.text or "数据图表"
    if current.endswith("</sup>") and "<br><sup>" in current:
        fig.update_layout(title_text=f"{current[:-6]}；{note}</sup>")
    else:
        fig.update_layout(title_text=f"{current}<br><sup>{note}</sup>")


def _annotate_extreme_values(fig: go.Figure) -> None:
    """在柱状图和折线图上自动标注最大值和最小值（best-effort）。

    仅标注第一个匹配的 trace，避免多 trace 分组图表标注过多影响可读性。
    通过 trace 类型（``bar`` 或 ``scatter`` 且 mode 含 ``lines``）判定是否
    适用，跳过饼图、热力图、散点矩阵等不适用场景。

    标注是 best-effort：任何异常都被吞掉，不能影响图表正常生成。
    """
    try:
        for trace in fig.data:
            trace_type = getattr(trace, "type", "bar")
            trace_mode = getattr(trace, "mode", "") or ""
            # 仅对柱状图和折线图标注，跳过饼图、热力图、散点矩阵等
            is_bar = trace_type == "bar"
            is_line = trace_type == "scatter" and trace_mode in {"lines", "lines+markers"}
            if not (is_bar or is_line):
                continue
            y_values = list(trace.y) if getattr(trace, "y", None) is not None else []
            x_values = list(trace.x) if getattr(trace, "x", None) is not None else []
            if not y_values or not x_values or len(y_values) != len(x_values):
                continue
            # 找到最大值和最小值的索引
            max_idx = max(range(len(y_values)), key=lambda i: y_values[i])
            min_idx = min(range(len(y_values)), key=lambda i: y_values[i])
            # 最大值标注
            y_max = y_values[max_idx]
            max_text = (
                f"最大: {y_max:.1f}"
                if isinstance(y_max, (int, float, np.number))
                else f"最大: {y_max}"
            )
            fig.add_annotation(
                x=x_values[max_idx],
                y=y_max,
                text=max_text,
                showarrow=True,
                arrowhead=2,
                arrowsize=0.8,
                arrowwidth=1,
                arrowcolor="#D97745",
                font={"size": 10, "color": "#D97745"},
                ax=0,
                ay=-30,
            )
            # 最小值标注
            y_min = y_values[min_idx]
            min_text = (
                f"最小: {y_min:.1f}"
                if isinstance(y_min, (int, float, np.number))
                else f"最小: {y_min}"
            )
            fig.add_annotation(
                x=x_values[min_idx],
                y=y_min,
                text=min_text,
                showarrow=True,
                arrowhead=2,
                arrowsize=0.8,
                arrowwidth=1,
                arrowcolor="#7A6FB0",
                font={"size": 10, "color": "#7A6FB0"},
                ax=0,
                ay=30,
            )
            break  # 只标注第一个 trace，避免多 trace 时标注过多
    except Exception:
        pass  # 标注是 best-effort，不能影响图表生成
