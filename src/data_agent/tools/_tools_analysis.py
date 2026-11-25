"""统计分析与登记指标工具：``statistical_analysis`` / ``compute_registered_metrics``。

从 ``builder.build_tools`` 按域拆出的工具定义；统计方法枚举与指标表达式
求值逻辑保持原实现不变。
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from data_agent.metrics import compute_metrics_on_frame, load_metrics_for_workspace
from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace

from ._stats import run_statistical_analysis


def build_analysis_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建统计分析类工具（statistical_analysis、compute_registered_metrics）。"""

    @tool
    def compute_registered_metrics(adopt: bool = False) -> str:
        """Compute metrics registered in metrics.json with their recorded expressions.

        Use this instead of inventing a formula in run_python_code whenever the session
        has metric definitions (name / aliases / expression). Expressions may only use
        column names and + - * / ** (). Returns sum / mean / non-null count per metric.
        adopt=true adds the computed columns to the active dataset.
        """
        definitions = load_metrics_for_workspace(workspace.root)
        if not definitions:
            return json_text(
                {
                    "status": "needs_input",
                    "message": (
                        "本会话没有 metrics.json，也没有 DATA_AGENT_METRICS_PATH。"
                        "登记口径后再调用，才能按统一公式计算，而不是现场发明。"
                    ),
                    "metrics": [],
                }
            )
        computed = compute_metrics_on_frame(workspace.dataframe, definitions)
        payload: dict[str, Any] = {"status": "ok", "metrics": computed}
        if adopt:
            from data_agent.metrics import evaluate_metric_expression

            frame = workspace.dataframe.copy()
            added: list[str] = []
            for item in computed:
                if item.get("status") != "ok":
                    continue
                frame[item["name"]] = evaluate_metric_expression(frame, item["expression"])
                added.append(item["name"])
            if added:
                workspace.adopt_dataset(frame, reset_source_baseline=False)
                payload["adopted_columns"] = added
                payload["output"] = workspace.save_dataframe(
                    "metrics_computed.csv", description="按登记口径计算的指标列"
                )
        return json_text(payload)

    @tool
    def statistical_analysis(
        method: Literal[
            "descriptive",
            "correlation",
            "groupby",
            "ttest_ind",
            "ttest_paired",
            "anova",
            "chi_square",
            "linear_regression",
            "rolling",
            "pct_change",
        ],
        columns: list[str] | None = None,
        group_by: str | None = None,
        target: str | None = None,
        aggregation: Literal["mean", "median", "sum", "count", "min", "max", "std"] = "mean",
        alpha: float = 0.05,
        groups: list[str] | None = None,
        window: int = 7,
        periods: int = 1,
    ) -> str:
        """Run rigorous statistical analysis on the active dataset.

        descriptive: optional columns. correlation: numeric columns, with Bonferroni-adjusted
        significance. groupby: group_by plus value columns. ttest_ind: prefer group_by + target
        to compare two groups (e.g. region vs sales); two numeric columns is a two-series test
        and is usually the wrong tool for business grouping. ttest_paired: two numeric columns.
        anova: numeric target grouped by group_by. chi_square: two categorical columns (warns
        when min expected frequency < 5). linear_regression: target plus numeric features.
        rolling / pct_change: ordered numeric series (period-over-period and rolling windows).
        """
        result = run_statistical_analysis(
            workspace.dataframe,
            method=method,
            columns=columns,
            group_by=group_by,
            target=target,
            aggregation=aggregation,
            alpha=alpha,
            groups=groups,
            window=window,
            periods=periods,
        )
        return json_text(result)

    return [compute_registered_metrics, statistical_analysis]
