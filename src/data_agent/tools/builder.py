"""数据分析工具集：为 ReAct 执行器提供会话绑定的数据操作能力。

本模块通过 ``build_tools(workspace)`` 工厂函数生成一组 LangChain BaseTool，
包括：
- inspect_data: 数据集概况检查（形状、类型、缺失、重复、样例）
- repair_data_format: 保守的格式修复（不触碰业务数据）
- clean_data: 带安全护栏的数据清洗
- transform_data: 非破坏性筛选/排序视图
- statistical_analysis: 描述统计、相关、分组、假设检验、回归
- create_visualization: Plotly 交互式图表生成
- export_data: 数据导出

设计原则：
- 所有工具通过闭包绑定同一个 DataWorkspace 实例，确保状态一致性。
- 清洗和变换操作带有严格的安全护栏（最小行数、最大删除比例）。
- 可视化自动检测极端值并提供主体尺度/全量视图切换，不修改原始数据。
- 工具返回 JSON 字符串，由 json_text() 统一序列化，确保 NaN/Infinity
  等非法 JSON 值被转为 null。
"""

from __future__ import annotations

import logging
import operator
import re
from html import escape
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import plotly.express as px
from langchain_core.tools import BaseTool, tool
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace, _atomic_write_text

from ._cleaning import (
    _apply_missing_strategy,
    _handle_outliers,
    _normalize_column_names,
    _parse_numeric_columns,
    _trim_string_columns,
)
from ._helpers import _human_column_label, _nice_ticks, _plotly_axis_tickformat
from .charts import (
    _BOOLEAN_VALUE_LABELS,
    _HAS_RECORDS_COLUMN,
    _HOVER_TEXT_COLUMN,
    _SAMPLE_COUNT_COLUMN,
    _add_missing_combination_markers,
    _aggregate_for_chart,
    _annotate_extreme_values,
    _apply_outlier_scale_controls,
    _chart_filename_stem,
    _checked_columns,
    _humanize_chart_title,
    _infer_chart_type,
    _localize_boolean_categories,
    _numeric_columns,
    _plotly_auto_interpret,
    _validate_chart_semantics,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 命名常量
# ---------------------------------------------------------------------------

#: 散点矩阵支持的最大维度数，超过后图表不可读且渲染性能急剧下降。
_SCATTER_MATRIX_MAX_DIMENSIONS = 8

#: 卡方检验拒绝的最大基数，超过此值应建议用户合并类别。
_CHI_SQUARE_MAX_CARDINALITY = 100

#: groupby 结果返回的最大行数，防止高基数分组擑爆 context window。
_GROUPBY_MAX_ROWS = 500

#: transform_data 的 limit 参数上限。
_TRANSFORM_LIMIT_MAX = 1_000_000

_CHART_COLORS = [
    "#245C55",
    "#D97745",
    "#4F6BED",
    "#C75D63",
    "#7A6FB0",
    "#D2A63C",
    "#4B8FA8",
    "#8A9A5B",
]

#: Plotly 暗色模式自适应脚本：注入图表 HTML，监听主题变化动态切换背景和文字颜色。
#: 浅色回退值与图表生成时的原始色板一致（#fbfaf5/#102a2a/#E5ECE9/#C9D5D1），
#: 避免 applyTheme 首次执行时改变浅色图表的视觉。
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
  function applyTheme() {
    var isDark = document.documentElement.dataset.theme === 'dark' ||
                 window.matchMedia('(prefers-color-scheme: dark)').matches;
    var plotEl = document.querySelector('.plotly-graph-div');
    if (!plotEl) return;
    var update = {
      'layout.paper_bgcolor': isDark ? '#1c2433' : '#fbfaf5',
      'layout.plot_bgcolor': isDark ? '#1c2433' : '#fbfaf5',
      'layout.font.color': isDark ? '#e6eaf0' : '#102a2a',
      'layout.xaxis.gridcolor': isDark ? '#2a3445' : '#E5ECE9',
      'layout.yaxis.gridcolor': isDark ? '#2a3445' : '#E5ECE9',
      'layout.xaxis.zerolinecolor': isDark ? '#3a4458' : '#C9D5D1',
      'layout.yaxis.zerolinecolor': isDark ? '#3a4458' : '#C9D5D1',
    };
    Plotly.relayout(plotEl, update);
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
</script>"""

_AGGREGATION_LABELS = {
    "sum": "合计",
    "mean": "平均值",
    "median": "中位数",
    "count": "计数",
    "min": "最小值",
    "max": "最大值",
}


def _apply_plotly_nice_ticks(
    fig: px.Figure,
    chart_type: str,
    x: str | None,
    y: str | None,
    scale_details: dict[str, Any],
) -> None:
    """对 Plotly 图表的数值轴应用 nice ticks（圆数刻度）。

    遍历所有 trace 收集 x/y 数值范围，计算 nice step 后设置
    dtick/tick0/tickmode。若 _apply_outlier_scale_controls 已设置范围
    （robust 模式），优先用该范围计算 nice ticks。category 轴跳过。
    """
    try:
        axis_ranges = scale_details.get("axis_ranges", {}) or {}
        # Y 轴（数值型图表都有）
        if y and chart_type in {"bar", "line", "area", "scatter", "histogram", "box", "violin"}:
            y_range = axis_ranges.get("y")
            if y_range:
                vmin, vmax = float(y_range[0]), float(y_range[1])
            else:
                values = _collect_trace_values(fig, "y")
                if not values:
                    return
                vmin, vmax = float(min(values)), float(max(values))
            nice_min, nice_max, step = _nice_ticks(vmin, vmax, n=5)
            if step > 0:
                fig.update_yaxes(
                    tickmode="linear",
                    dtick=step,
                    tick0=nice_min,
                    tickformat=_plotly_axis_tickformat((nice_min, nice_max)),
                )
        # X 轴（仅散点图等数值 X 轴）
        if x and chart_type in {"scatter", "scatter_3d"}:
            x_range = axis_ranges.get("x")
            if x_range:
                vmin, vmax = float(x_range[0]), float(x_range[1])
            else:
                values = _collect_trace_values(fig, "x")
                if not values:
                    return
                vmin, vmax = float(min(values)), float(max(values))
            nice_min, nice_max, step = _nice_ticks(vmin, vmax, n=5)
            if step > 0:
                fig.update_xaxes(
                    tickmode="linear",
                    dtick=step,
                    tick0=nice_min,
                    tickformat=_plotly_axis_tickformat((nice_min, nice_max)),
                )
    except Exception:
        # nice ticks 是 best-effort，不能影响图表生成
        pass


def _collect_trace_values(fig: px.Figure, axis: str) -> list[float]:
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


def build_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """创建绑定到指定工作区的工具集，供 ReAct Agent 使用。

    所有工具通过闭包捕获同一个 workspace 实例，确保数据状态一致性。
    工具列表的顺序即为 Agent 看到的工具顺序。

    Args:
        workspace: 当前分析会话的数据工作区。

    Returns:
        7 个 BaseTool 实例的列表。
    """

    @tool
    def inspect_data(sample_rows: int = 5) -> str:
        """Inspect the active dataset: shape, types, missing values, duplicates and sample rows.

        Always call this before cleaning, statistics or visualization. sample_rows must be 1-20.
        """
        if not 1 <= sample_rows <= 20:
            raise ValueError("sample_rows 必须在 1 到 20 之间。")
        return json_text(workspace.profile(sample_rows=sample_rows))

    @tool
    def repair_data_format(
        normalize_missing: bool = True,
        trim_strings: bool = True,
        parse_numeric: bool = True,
        parse_dates: bool = True,
        normalize_column_names: bool = False,
    ) -> str:
        """Repair only unambiguous formatting issues and return an audit record.

        Safe repairs include trimming text, standardizing explicit missing markers, converting
        fully numeric text columns, and converting fully parseable date/time columns. It never
        removes duplicates, fills missing business values, clips outliers, or changes negatives.
        Call this after a type/format error before retrying the failed analysis tool.
        """
        return json_text(
            workspace.repair_format(
                normalize_missing=normalize_missing,
                trim_strings=trim_strings,
                parse_numeric=parse_numeric,
                parse_dates=parse_dates,
                normalize_column_names=normalize_column_names,
            )
        )

    @tool
    def clean_data(
        columns: list[str] | None = None,
        drop_duplicates: bool = True,
        trim_strings: bool = True,
        normalize_column_names: bool = False,
        missing_strategy: Literal["none", "drop", "mean", "median", "mode", "forward_fill", "backward_fill"] = "none",
        parse_numeric: bool = False,
        datetime_columns: list[str] | None = None,
        outlier_method: Literal["none", "iqr", "zscore"] = "none",
        outlier_action: Literal["remove", "cap"] = "cap",
    ) -> str:
        """Clean the active dataset and save cleaned_data.csv.

        columns limits missing/outlier handling; other operations still apply globally.
        parse_numeric converts an object/string column when at least 80% of its non-empty
        values are numeric. This is intentionally more permissive than repair_data_format,
        which only converts columns where 100% of values are unambiguously numeric after
        trimming currency symbols and thousands separators. Use repair_data_format for safe
        formatting fixes and clean_data when the analysis needs broader numeric coercion.
        datetime_columns explicitly selects columns to parse as dates.
        IQR/z-score outlier handling only applies to numeric selected columns.
        Safety guards (always enforced regardless of parameters):
          - Missing-value deletion over 50% of the current rows is always refused;
            narrowing ``columns`` does not bypass this guard.
          - The cumulative row count of the main dataset must stay at or above 20%
            of the original source row count (computed as max(1, ceil(source/5))).
            Anything that would drop below that floor is refused so the active
            dataset never collapses to a meaningless sample.
        """
        df = workspace.dataframe.copy()
        before = {"rows": len(df), "columns": len(df.columns), "missing": int(df.isna().sum().sum())}
        changes: list[str] = []

        if normalize_column_names:
            df, columns, datetime_columns, changed = _normalize_column_names(df, columns, datetime_columns)
            if changed:
                changes.append("normalized column names")

        selected = _checked_columns(df, columns)
        if trim_strings:
            trimmed = _trim_string_columns(df)
            if trimmed:
                changes.append(f"trimmed {trimmed} text columns")

        if parse_numeric:
            converted = _parse_numeric_columns(df)
            changes.append(f"parsed numeric columns: {converted}")

        if datetime_columns:
            _checked_columns(df, datetime_columns)
            for column in datetime_columns:
                df[column] = pd.to_datetime(df[column], errors="coerce")
            changes.append(f"parsed datetime columns: {datetime_columns}")

        if drop_duplicates:
            count = int(df.duplicated().sum())
            df = df.drop_duplicates().copy()
            changes.append(f"removed {count} duplicate rows")

        if missing_strategy != "none":
            _apply_missing_strategy(df, selected, missing_strategy)
            changes.append(f"applied missing strategy: {missing_strategy}")

        if outlier_method != "none":
            count, _ = _handle_outliers(df, selected, outlier_method, outlier_action)
            changes.append(f"{outlier_action}ped {count} {outlier_method} outlier rows")

        minimum_rows = max(1, (workspace.source_row_count + 4) // 5)
        if len(df) < minimum_rows:
            raise ValueError(
                f"拒绝累计删除过多记录：当前操作会使主数据从原始 {workspace.source_row_count} 行"
                f"缩减为 {len(df)} 行，安全下限为 {minimum_rows} 行。"
                "请保留主数据，并使用非破坏性的筛选视图检查子集。"
            )

        workspace.dataframe = df.reset_index(drop=True)
        output = workspace.save_dataframe("cleaned_data.csv")
        after = {"rows": len(df), "columns": len(df.columns), "missing": int(df.isna().sum().sum())}
        return json_text({"status": "ok", "before": before, "after": after, "changes": changes, "output": output})

    @tool
    def transform_data(
        select_columns: list[str] | None = None,
        filter_column: str | None = None,
        filter_operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "in"] = "eq",
        filter_value: str | float | int | list[str] | list[float] | None = None,
        sort_by: list[str] | None = None,
        ascending: bool = True,
        limit: int | None = None,
    ) -> str:
        """Create a non-destructive filtered or sorted view and export it.

        Use 'in' with a list and 'contains' for text. The result is exported as
        transformed_data.csv but never replaces the active dataset. Use clean_data for deliberate,
        guarded changes to the main data.
        """
        df = workspace.dataframe.copy()
        if filter_column:
            _checked_columns(df, [filter_column])
            if filter_value is None:
                raise ValueError("使用筛选时 filter_value 不能为空。")
            series = df[filter_column]
            if filter_operator == "contains":
                mask = series.astype("string").str.contains(str(filter_value), case=False, na=False, regex=False)
            elif filter_operator == "in":
                if not isinstance(filter_value, list):
                    raise ValueError("in 操作需要列表类型 filter_value。")
                # Adapt list element types so that numeric columns accept numeric
                # lists and string columns accept string lists; mixed types
                # would silently produce an empty result via pandas isin.
                values: list[Any] = list(filter_value)
                if pd.api.types.is_numeric_dtype(series):
                    coerced: list[Any] = []
                    for value in values:
                        try:
                            coerced.append(float(value))
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                f"列 {filter_column} 是数值列，filter_value 中的 {value!r} 无法转为数值。"
                            ) from exc
                    values = coerced
                else:
                    values = [str(value) for value in values]
                mask = series.isin(values)
            else:
                operations = {
                    "eq": operator.eq,
                    "ne": operator.ne,
                    "gt": operator.gt,
                    "ge": operator.ge,
                    "lt": operator.lt,
                    "le": operator.le,
                }
                comparison: Any = filter_value
                if pd.api.types.is_numeric_dtype(series):
                    try:
                        comparison = float(filter_value)  # type: ignore[arg-type]
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"列 {filter_column} 是数值列，filter_value {filter_value!r} 无法转为数值。"
                        ) from exc
                mask = operations[filter_operator](series, comparison)
            df = df.loc[mask].copy()
        if sort_by:
            _checked_columns(df, sort_by)
            df = df.sort_values(sort_by, ascending=ascending)
        if select_columns:
            _checked_columns(df, select_columns)
            df = df[select_columns].copy()
        if limit is not None:
            if not 1 <= limit <= _TRANSFORM_LIMIT_MAX:
                raise ValueError(f"limit 必须在 1 到 {_TRANSFORM_LIMIT_MAX:,} 之间。")
            df = df.head(limit).copy()
        df = df.reset_index(drop=True)
        output = workspace.save_dataframe(
            "transformed_data.csv",
            dataframe=df,
            description="筛选或排序后的数据视图（未改变主数据）",
        )
        return json_text(
            {
                "status": "ok",
                "rows": len(df),
                "columns": list(df.columns),
                "output": output,
                "view_only": True,
                "active_rows": len(workspace.dataframe),
            }
        )

    @tool
    def statistical_analysis(
        method: Literal["descriptive", "correlation", "groupby", "ttest_ind", "ttest_paired", "anova", "chi_square", "linear_regression"],
        columns: list[str] | None = None,
        group_by: str | None = None,
        target: str | None = None,
        aggregation: Literal["mean", "median", "sum", "count", "min", "max", "std"] = "mean",
        alpha: float = 0.05,
    ) -> str:
        """Run rigorous statistical analysis on the active dataset.

        descriptive: optional columns. correlation: numeric columns. groupby: group_by plus value
        columns. t-tests: exactly two numeric columns. anova: numeric target grouped by group_by.
        chi_square: exactly two categorical columns. linear_regression: target plus numeric feature
        columns. Returns statistics, p-values, effect/model metrics and plain decision fields.

        只读契约：此工具不 copy DataFrame，所有下游操作（describe / corr /
        groupby / dropna / crosstab）都返回新对象，不修改 workspace.dataframe。
        若未来需要在此工具中修改数据，必须先 ``df = workspace.dataframe.copy()``
        以免污染主数据。
        """
        # 只读引用：所有下游操作都返回新 DataFrame，不修改 workspace.dataframe。
        df = workspace.dataframe
        if not 0 < alpha < 1:
            raise ValueError("alpha 必须在 0 到 1 之间。")
        result: dict[str, Any] = {"method": method, "alpha": alpha}

        if method == "descriptive":
            selected = _checked_columns(df, columns)
            desc = df[selected].describe(include="all").transpose().reset_index(names="column")
            # 补充分布形态指标（偏度/峰度），帮助判断数据是否正态、是否有重尾。
            numeric_selected = [c for c in selected if pd.api.types.is_numeric_dtype(df[c])]
            if numeric_selected:
                skew = df[numeric_selected].skew().rename("skewness")
                kurt = df[numeric_selected].kurtosis().rename("kurtosis")
                shape_df = pd.DataFrame({"column": numeric_selected, "skewness": skew.values, "kurtosis": kurt.values})
                desc = desc.merge(shape_df, on="column", how="left")
            result["result"] = desc
        elif method == "correlation":
            numeric = _numeric_columns(df, columns)
            result["columns"] = numeric
            result["result"] = df[numeric].corr().round(6).to_dict()
            # 性能优化：预先提取数值子集，避免每对列重复索引和 dropna。
            # 对 20+ 数值列，原实现每对都 df[[left,right]].dropna()，
            # 产生大量中间 DataFrame 对象；改为一次性提取子集后按列对操作。
            numeric_df = df[numeric]
            p_values: dict[str, dict[str, float | None]] = {column: {} for column in numeric}
            sample_sizes: dict[str, dict[str, int]] = {column: {} for column in numeric}
            for i, left in enumerate(numeric):
                p_values[left][left] = 0.0
                sample_sizes[left][left] = int(numeric_df[left].notna().sum())
                left_series = numeric_df[left]
                for right in numeric[i + 1:]:
                    right_series = numeric_df[right]
                    # 只对两列同时非空的行计算，避免全表 dropna。
                    pair_mask = left_series.notna() & right_series.notna()
                    n_valid = int(pair_mask.sum())
                    sample_sizes[left][right] = n_valid
                    sample_sizes[right][left] = n_valid
                    if n_valid < 3:
                        p_values[left][right] = None
                        p_values[right][left] = None
                    else:
                        lv = left_series[pair_mask]
                        rv = right_series[pair_mask]
                        if lv.nunique() < 2 or rv.nunique() < 2:
                            p_values[left][right] = None
                            p_values[right][left] = None
                        else:
                            p_val = float(stats.pearsonr(lv, rv).pvalue)
                            p_values[left][right] = p_val
                            p_values[right][left] = p_val
            result["p_values"] = p_values
            result["sample_sizes"] = sample_sizes
        elif method == "groupby":
            if not group_by:
                raise ValueError("groupby 方法需要 group_by。")
            _checked_columns(df, [group_by])
            values = _numeric_columns(df, columns) if aggregation != "count" else _checked_columns(df, columns)
            grouped = df.groupby(group_by, dropna=False)[values].agg(aggregation).reset_index()
            # 补充每组样本量，帮助用户判断分组结果是否可靠。
            group_sizes = df.groupby(group_by, dropna=False).size().rename("_group_n_").reset_index()
            grouped = grouped.merge(group_sizes, on=group_by, how="left")
            result["result"] = grouped.head(_GROUPBY_MAX_ROWS)
            result["truncated"] = len(grouped) > _GROUPBY_MAX_ROWS
            result["group_count"] = int(df[group_by].nunique(dropna=False))
        elif method in {"ttest_ind", "ttest_paired"}:
            numeric = _numeric_columns(df, columns)
            if len(numeric) != 2:
                raise ValueError("t 检验需要恰好两个数值列。")
            pair = df[numeric].dropna()
            first, second = pair[numeric[0]], pair[numeric[1]]
            test = stats.ttest_ind(first, second, equal_var=False) if method == "ttest_ind" else stats.ttest_rel(first, second)
            difference = first - second
            if method == "ttest_ind":
                pooled = np.sqrt(((first.var(ddof=1) * (len(first) - 1)) + (second.var(ddof=1) * (len(second) - 1))) / max(len(pair) - 2, 1))
                effect_size = float((first.mean() - second.mean()) / pooled) if pooled else 0.0
            else:
                effect_size = float(difference.mean() / difference.std(ddof=1)) if difference.std(ddof=1) else 0.0
            confidence = test.confidence_interval(confidence_level=1 - alpha)
            result.update(
                statistic=float(test.statistic),
                p_value=float(test.pvalue),
                significant=bool(test.pvalue < alpha),
                sample_size=len(pair),
                means={column: float(pair[column].mean()) for column in numeric},
                mean_difference=float(difference.mean()),
                confidence_interval={"low": float(confidence.low), "high": float(confidence.high)},
                effect_size=float(effect_size),
                effect_size_name="cohens_d",
            )
        elif method == "anova":
            if not group_by or not target:
                raise ValueError("anova 需要 group_by 和 target。")
            _checked_columns(df, [group_by, target])
            if not pd.api.types.is_numeric_dtype(df[target]):
                raise ValueError("anova target 必须是数值列。")
            groups = [part[target].dropna().to_numpy() for _, part in df.groupby(group_by) if part[target].notna().sum() >= 2]
            if len(groups) < 2:
                raise ValueError("anova 至少需要两个各含 2 个有效观测值的组。")
            test = stats.f_oneway(*groups)
            valid = np.concatenate(groups)
            grand_mean = float(valid.mean())
            between = sum(len(group) * (float(group.mean()) - grand_mean) ** 2 for group in groups)
            total = float(((valid - grand_mean) ** 2).sum())
            result.update(
                statistic=float(test.statistic),
                p_value=float(test.pvalue),
                significant=bool(test.pvalue < alpha),
                groups=len(groups),
                total_n=int(len(valid)),
                eta_squared=float(between / total) if total else 0.0,
                group_means={str(name): float(part[target].mean()) for name, part in df.groupby(group_by) if part[target].notna().sum() >= 2},
            )
        elif method == "chi_square":
            selected = _checked_columns(df, columns)
            if len(selected) != 2:
                raise ValueError("卡方检验需要恰好两个分类列。")
            cardinality = {column: int(df[column].nunique(dropna=True)) for column in selected}
            max_cardinality = max(cardinality.values())
            if max_cardinality > _CHI_SQUARE_MAX_CARDINALITY:
                raise ValueError(
                    f"卡方检验拒绝高基数列：{cardinality}。"
                    "请对类别做合并、分组或改用其他统计方法。"
                )
            table = pd.crosstab(df[selected[0]], df[selected[1]])
            chi2, p_value, dof, expected = stats.chi2_contingency(table)
            result.update(statistic=float(chi2), p_value=float(p_value), significant=bool(p_value < alpha), degrees_of_freedom=int(dof), contingency_table=table.to_dict(), min_expected=float(np.min(expected)))
        else:
            if not target:
                raise ValueError("linear_regression 需要 target。")
            features = _numeric_columns(df, columns)
            _checked_columns(df, [target])
            if target in features:
                features.remove(target)
            if not features or not pd.api.types.is_numeric_dtype(df[target]):
                raise ValueError("线性回归需要数值 target 和至少一个数值特征列。")
            model_data = df[[*features, target]].dropna()
            if len(model_data) <= len(features) + 1:
                raise ValueError("有效样本量不足以拟合线性回归。")
            model = LinearRegression().fit(model_data[features], model_data[target])
            prediction = model.predict(model_data[features])
            result.update(
                target=target,
                features=features,
                sample_size=len(model_data),
                intercept=float(model.intercept_),
                coefficients=dict(zip(features, map(float, model.coef_), strict=True)),
                r2=float(r2_score(model_data[target], prediction)),
                rmse=float(mean_squared_error(model_data[target], prediction) ** 0.5),
                mae=float(mean_absolute_error(model_data[target], prediction)),
                adjusted_r2=float(1 - (1 - r2_score(model_data[target], prediction)) * (len(model_data) - 1) / max(len(model_data) - len(features) - 1, 1)),
            )

        return json_text(result)

    @tool
    def create_visualization(
        chart_type: Literal["auto", "bar", "line", "area", "scatter", "scatter_3d", "histogram", "box", "violin", "pie", "heatmap", "correlation_heatmap", "scatter_matrix", "sunburst", "treemap"] = "auto",
        x: str | None = None,
        y: str | None = None,
        color: str | None = None,
        z: str | None = None,
        size: str | None = None,
        values: str | None = None,
        path_columns: list[str] | None = None,
        dimensions: list[str] | None = None,
        aggregation: Literal["none", "mean", "median", "sum", "count", "min", "max"] = "none",
        title: str | None = None,
        chart_engine: Literal["plotly", "echarts"] = "plotly",
        bins: int = 30,
        top_n: int | None = None,
        scale_mode: Literal["auto", "full"] = "auto",
        export_png: bool = False,
    ) -> str:
        """Create an interactive chart and save a standalone HTML artifact.

        双引擎：``chart_engine="plotly"``（默认）走原 Plotly 渲染分支；
        ``chart_engine="echarts"`` 走 ECharts 渲染分支，适合正式报告，
        自带学术级交互（区间缩放、图例多选、平滑动画、高清导出）和
        数据驱动的白话解读。两个引擎复用同一套数据准备逻辑（聚合、
        布尔值本地化、top_n、_checked_columns），上层无感知切换。

        ``chart_type="auto"``（默认）会按数据列的类型与格式自动选择最合适
        的图型：时间序列（datetime x）→ 折线图；两数值列 → 散点图；
        分类列 + 数值列 → 柱状图（类别少且无 color 时退化成饼图做构成占比）；
        单数值列分布 → 直方图；层级 path_columns → 旭日图；多数值列
        （无 x/y）→ 相关性热力图；dimensions ≥3 → 散点矩阵。调用方仍可用
        显式 chart_type 覆盖自动选择。

        Supports standard and complex plots including 3D scatter, correlation heatmap, scatter
        matrix, sunburst and treemap. For bar/line/area, aggregation groups by x and optional
        color. heatmap expects x, y and numeric values. scatter_matrix uses dimensions.
        sunburst/treemap use path_columns and values. scale_mode="auto" detects scale-destroying
        extreme values and adds readable main/full viewport controls without changing the data;
        use scale_mode="full" only when the user explicitly wants the uncompressed raw scale.
        export_png is best-effort and needs Chrome.
        """
        # 性能优化：只在确实需要修改数据时才 copy（聚合、布尔值本地化、
        # top_n 筛选）。对大 DataFrame（100K+ 行）避免无谓的深拷贝。
        # correlation_heatmap / scatter_matrix 等只读图表不需要 copy。
        _raw_df = workspace.dataframe
        requested = [value for value in [x, y, color, z, size, values] if value]
        requested.extend(path_columns or [])
        requested.extend(dimensions or [])
        # 先校验列存在性，确保后续自动选图能安全读取 df[x]/df[y]。
        _checked_columns(_raw_df, requested)

        # 自动选图：必须在“是否需要 copy”的判断之前解析出具体 chart_type，
        # 否则 needs_mutation 会因 chart_type=='auto' 误判为无需改动（bar/line/
        # area/pie/sunburst/treemap 需要 copy 做聚合/布尔本地化）。
        was_auto = chart_type == "auto"
        if was_auto:
            chart_type = _infer_chart_type(
                _raw_df, x=x, y=y, color=color, z=z, size=size,
                values=values, path_columns=path_columns, dimensions=dimensions,
                aggregation=aggregation, top_n=top_n,
            )
            # 纯分类计数场景（仅给 x、无 y）：自动用 count 聚合，避免空 series。
            if chart_type == "bar" and y is None and aggregation == "none":
                aggregation = "count"
            # 没有 color 分组、且 x 含重复类别（如 product×channel 但没传 color）
            # 时，柱图会出现重复类别导致错乱，自动按 x 求和聚合对齐到每类一行。
            # 注：aggregation 仅对 bar/line/area 生效，饼图的重复聚合在下方单独处理。
            if (
                chart_type == "bar"
                and y is not None
                and aggregation == "none"
                and not color
            ):
                non_null = _raw_df[x].dropna() if x else pd.Series(dtype=object)
                if len(non_null) > non_null.nunique():
                    aggregation = "sum"

        _has_bool = any(
            col and col in _raw_df.columns and pd.api.types.is_bool_dtype(_raw_df[col])
            for col in (x, color)
        )
        needs_mutation = (
            aggregation != "none"
            or chart_type in {"bar", "line", "area", "pie", "sunburst", "treemap"}
            or top_n is not None
            or _has_bool
        )
        df = _raw_df.copy() if needs_mutation else _raw_df
        # 自动选图推断为饼图、且 x 含重复类别（如 product×channel 但没传 color）
        # 时，饼图生成器按原始行会产生同名扇区错乱；这里预先按 x 聚合求和，
        # 两引擎饼图均按单行绘制（ECharts 生成器还会再 groupby，幂等）。
        if was_auto and chart_type == "pie" and y is not None and not color:
            non_null = df[x].dropna()
            if len(non_null) > non_null.nunique():
                df = df.groupby(x, dropna=False)[y].sum().reset_index()
        # 意义性防护：拦截 ID 列分布图、常量列图、高基数不可读图表，
        # 报错文案带修正建议（换列/传 top_n/换图型）供模型下一轮自行纠正。
        _validate_chart_semantics(
            df,
            chart_type=chart_type,
            x=x,
            y=y,
            color=color,
            values=values,
            path_columns=path_columns,
            dimensions=dimensions,
            top_n=top_n,
        )
        if top_n is not None:
            if not x or not 1 <= top_n <= 500:
                raise ValueError("top_n 需要 x，且必须在 1 到 500 之间。")
            keep = df[x].value_counts(dropna=False).head(top_n).index
            df = df[df[x].isin(keep)].copy()

        groupable = chart_type in {"bar", "line", "area"}
        coverage: dict[str, Any] = {
            "complete": True,
            "observed_combinations": 0,
            "total_combinations": 0,
            "missing_combinations": [],
        }
        if aggregation != "none":
            if not groupable or not x:
                raise ValueError("aggregation 仅用于带 x 的 bar/line/area。")
            df, y, coverage = _aggregate_for_chart(
                df,
                x=x,
                y=y,
                color=color,
                aggregation=aggregation,
            )

        _localize_boolean_categories(df, [x, color])
        for item in coverage.get("missing_combinations", []):
            for column in (x, color):
                if column and item.get(column) in _BOOLEAN_VALUE_LABELS:
                    item[column] = _BOOLEAN_VALUE_LABELS[item[column]]
        if coverage.get("color_levels"):
            coverage["color_levels"] = [
                _BOOLEAN_VALUE_LABELS.get(value, value) for value in coverage["color_levels"]
            ]

        labels = {column: _human_column_label(str(column)) for column in df.columns}
        if y and aggregation in _AGGREGATION_LABELS:
            labels[y] = f"{_human_column_label(y)}（{_AGGREGATION_LABELS[aggregation]}）"
        common = {
            "data_frame": df,
            "title": title,
            "template": "plotly_white",
            "labels": labels,
            "color_discrete_sequence": _CHART_COLORS,
        }
        if chart_type == "bar":
            # 预计算 hover 文本：对无记录 bar（reindex 产生的空白组合）显示
            # "无样本/无记录"，而非让 Plotly 把 y=NaN 格式化成 "nan"。
            # 聚合后行数通常很少（x_levels × color_levels），iterrows 开销可忽略。
            if aggregation != "none" and _SAMPLE_COUNT_COLUMN in df.columns:
                x_label_hover = _human_column_label(x)
                y_label_hover = _human_column_label(y) if y else ""
                hover_lines: list[str] = []
                for _, row in df.iterrows():
                    if row[_HAS_RECORDS_COLUMN]:
                        lines = [f"{x_label_hover}：{row[x]}"]
                        if y:
                            y_val = row[y]
                            y_text = f"{float(y_val):,.2f}" if pd.notna(y_val) else "—"
                            lines.append(f"{y_label_hover}：{y_text}")
                        lines.append(f"样本数：{int(row[_SAMPLE_COUNT_COLUMN])}")
                        hover_lines.append("<br>".join(lines))
                    else:
                        missing_label = "无样本" if aggregation in {"mean", "median", "min", "max"} else "无记录"
                        hover_lines.append(f"{x_label_hover}：{row[x]}<br>{missing_label}")
                df[_HOVER_TEXT_COLUMN] = hover_lines
                custom_data = [_SAMPLE_COUNT_COLUMN, _HAS_RECORDS_COLUMN, _HOVER_TEXT_COLUMN]
            else:
                custom_data = None
            fig = px.bar(**common, x=x, y=y, color=color, barmode="group", custom_data=custom_data)
        elif chart_type == "line":
            fig = px.line(**common, x=x, y=y, color=color, markers=True)
        elif chart_type == "area":
            fig = px.area(**common, x=x, y=y, color=color)
        elif chart_type == "scatter":
            fig = px.scatter(**common, x=x, y=y, color=color, size=size, trendline=None)
        elif chart_type == "scatter_3d":
            if not x or not y or not z:
                raise ValueError("scatter_3d 需要 x、y、z。")
            fig = px.scatter_3d(**common, x=x, y=y, z=z, color=color, size=size)
        elif chart_type == "histogram":
            fig = px.histogram(**common, x=x, color=color, nbins=max(2, min(bins, 200)), marginal="box")
        elif chart_type == "box":
            fig = px.box(**common, x=x, y=y, color=color, points="outliers")
        elif chart_type == "violin":
            fig = px.violin(**common, x=x, y=y, color=color, box=True, points="outliers")
        elif chart_type == "pie":
            if not x:
                raise ValueError("pie 需要 x 作为名称列。")
            fig = px.pie(**common, names=x, values=values or y, hole=0.42)
        elif chart_type == "heatmap":
            if not x or not y or not values:
                raise ValueError("heatmap 需要 x、y、values。")
            pivot = df.pivot_table(index=y, columns=x, values=values, aggfunc="mean")
            fig = px.imshow(pivot, text_auto=True, aspect="auto", title=title, color_continuous_scale="RdBu_r")
        elif chart_type == "correlation_heatmap":
            numeric = _numeric_columns(df, dimensions)
            corr = df[numeric].corr()
            fig = px.imshow(corr, text_auto=".2f", aspect="auto", title=title or "Correlation heatmap", color_continuous_scale="RdBu_r", zmin=-1, zmax=1)
        elif chart_type == "scatter_matrix":
            numeric = _numeric_columns(df, dimensions)
            if len(numeric) > _SCATTER_MATRIX_MAX_DIMENSIONS:
                raise ValueError(f"scatter_matrix 最多支持 {_SCATTER_MATRIX_MAX_DIMENSIONS} 个维度，请缩小 dimensions。")
            fig = px.scatter_matrix(**common, dimensions=numeric, color=color)
            fig.update_traces(diagonal_visible=False)
        elif chart_type == "sunburst":
            if not path_columns:
                raise ValueError("sunburst 需要 path_columns。")
            fig = px.sunburst(**common, path=path_columns, values=values)
        else:
            if not path_columns:
                raise ValueError("treemap 需要 path_columns。")
            fig = px.treemap(**common, path=path_columns, values=values, color=color)

        scale_details = _apply_outlier_scale_controls(fig, chart_type, scale_mode)
        if chart_type == "bar" and aggregation != "none":
            _add_missing_combination_markers(
                fig,
                coverage,
                x=x,
                color=color,
                aggregation=aggregation,
            )
        # 自动标注关键统计量（最大值/最小值），仅用于柱状图和折线图
        if chart_type in {"bar", "line"}:
            _annotate_extreme_values(fig)
        fig.update_layout(
            font={"family": "IBM Plex Sans, Noto Sans SC, sans-serif", "color": "#102a2a"},
            paper_bgcolor="#fbfaf5",
            plot_bgcolor="#fbfaf5",
            colorway=_CHART_COLORS,
            margin={
                "l": 64,
                "r": 36,
                "t": 108
                if scale_details["scale_mode"] == "robust" or coverage.get("missing_combinations")
                else 76,
                "b": 64,
            },
            hoverlabel={"bgcolor": "#102a2a", "font_color": "white"},
            title={"x": 0.01, "xanchor": "left", "font": {"size": 22, "color": "#102a2a"}},
            legend={
                "bgcolor": "rgba(255,255,255,0.82)",
                "bordercolor": "#D9E1DE",
                "borderwidth": 1,
                "font": {"size": 12},
            },
            autosize=True,
        )
        fig.update_xaxes(
            showgrid=True,
            gridcolor="#E5ECE9",
            zerolinecolor="#C9D5D1",
            linecolor="#BFCBC7",
            tickfont={"size": 12},
            title_font={"size": 14},
            automargin=True,
        )
        fig.update_yaxes(
            showgrid=True,
            gridcolor="#E5ECE9",
            zerolinecolor="#C9D5D1",
            linecolor="#BFCBC7",
            tickfont={"size": 12},
            title_font={"size": 14},
            automargin=True,
            separatethousands=True,
        )
        # === nice ticks：对数值轴应用圆数刻度（1/2/5/10 倍数）===
        # 仅对 value 类型的轴生效，category/time 轴跳过。
        # 若 _apply_outlier_scale_controls 已设置范围，优先用其范围计算 nice ticks。
        _apply_plotly_nice_ticks(fig, chart_type, x, y, scale_details)
        if chart_type == "bar":
            fig.update_traces(marker_line_color="rgba(16,42,42,0.16)", marker_line_width=0.8, selector={"type": "bar"})
            if aggregation != "none" and _HOVER_TEXT_COLUMN in df.columns:
                # 用预计算的 hover 文本（customdata[2]），避免无记录 bar 显示 nan。
                # customdata 结构：[sample_count, has_records, hover_text]
                fig.update_traces(
                    hovertemplate="%{customdata[2]}<extra>%{fullData.name}</extra>",
                    selector={"type": "bar"},
                )
            if x and not pd.api.types.is_numeric_dtype(df[x]):
                fig.update_xaxes(categoryorder="total descending")
            fig.update_layout(bargap=0.26, bargroupgap=0.08)
        elif chart_type == "scatter":
            fig.update_traces(marker={"size": 9, "opacity": 0.82, "line": {"width": 0.7, "color": "white"}}, selector={"type": "scatter"})
        if color:
            fig.update_layout(legend_title_text=_human_column_label(color))
        # 文件名用"类型_序号"格式（如 柱状图_01），不再把 LLM 给的整段标题
        # 塞进文件名——以前会出现"客户评分按产品分布_ANOVA_p_0_0012_η²_..."
        # 这种看不懂的乱码文件名。display_title 才是 UI 上展示的人话标题。
        existing_chart_count = workspace.count_artifacts("visualization")
        stem = _chart_filename_stem(chart_type, existing_chart_count)
        display_title = _humanize_chart_title(title, chart_type)
        # === 双引擎分派：echarts 走独立渲染分支，plotly 走原有逻辑 ===
        if chart_engine == "echarts":
            from data_agent.echarts_engine import _render_echarts
            return json_text(_render_echarts(
                workspace, df,
                chart_type=chart_type, x=x, y=y, color=color, z=z, size=size,
                values=values, path_columns=path_columns, dimensions=dimensions,
                aggregation=aggregation, title=title, bins=bins,
                display_title=display_title, stem=stem,
                chart_type_source="auto" if was_auto else "explicit",
            ))
        # === Plotly 原有渲染逻辑（默认分支，保持不变）===
        html_path = workspace.artifacts_dir / f"{stem}.html"
        shared_plotly = workspace.ensure_plotly_bundle()
        relative_script = shared_plotly.relative_to(workspace.artifacts_dir).as_posix() if shared_plotly else None
        # Plotly 白话解读：与 ECharts _auto_interpret 语义对齐
        interpretation = _plotly_auto_interpret(
            df, chart_type=chart_type, x=x, y=y, color=color,
            aggregation=aggregation, title=display_title,
        )
        interpretation_block = ""
        if interpretation:
            interpretation_block = (
                '<div class="plotly-interpretation">'
                '<div class="plotly-interpretation-title">数据解读</div>'
                f'{escape(interpretation)}'
                '</div>'
            )
        html_template = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>{title}</title><script src='{script}'></script>"
            "<style>html,body{{width:100%;height:100%;margin:0;background:#fbfaf5;overflow:hidden;font-family:'IBM Plex Sans','Noto Sans SC',sans-serif}}"
            ".plotly-graph-div{{width:100% !important;height:100% !important;min-height:440px}}"
            ".layout{{display:flex;flex-direction:column;height:100%}}"
            ".chart-wrap{{flex:1;min-height:0}}"
            ".plotly-interpretation{{border-top:1px solid #e5e7eb;padding:14px 24px;background:#f9fafb;font-size:13px;line-height:1.75;color:#374151;max-height:160px;overflow-y:auto}}"
            ".plotly-interpretation-title{{font-size:12px;color:#6b7280;font-weight:600;margin-bottom:6px;letter-spacing:0.5px}}"
            "</style>"
            "</head><body><div class='layout'><div class='chart-wrap'>{div}</div>{interpretation}</div>{dark_script}</body></html>"
            if relative_script
            else None
        )
        if html_template is not None:
            div = fig.to_html(
                full_html=False,
                include_plotlyjs=False,
                default_width="100%",
                default_height="100%",
                config={
                    "responsive": True,
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                },
            )
            # XSS 防护：Plotly to_html 把 figure 数据序列化进 <script> 标签，
            # json.dumps 默认不转义 </script>。若用户 CSV 某列含
            # "</script><script>alert(1)</script>"，LLM 用该列作 x/color，
            # 浏览器解析时第一个 </script> 会提前关闭 Plotly 的 script 块，
            # 剩余 JS 在 iframe 上下文执行。统一转义 <\/script> 避免注入。
            div = div.replace("</script>", "<\\/script>")
            # 原子写：进程被 kill / 磁盘满时不会留下半截 HTML 让预览 iframe
            # 加载到损坏页面。tmp + os.replace 对同目录文件是原子的。
            _atomic_write_text(
                html_path,
                html_template.format(
                    title=escape(display_title),
                    script=relative_script,
                    div=div,
                    interpretation=interpretation_block,
                    dark_script=_PLOTLY_DARK_MODE_SCRIPT,
                ),
            )
        else:
            fig.write_html(html_path, include_plotlyjs=True, full_html=True)
        workspace.register_artifact(html_path, "visualization", display_title)
        json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
        fig.write_json(json_path)
        workspace.register_artifact(json_path, "chart_data", "Plotly figure JSON")

        response: dict[str, Any] = {
            "status": "ok",
            "chart_type": chart_type,
            "chart_type_source": "auto" if was_auto else "explicit",
            "rows_plotted": len(df),
            **scale_details,
            "category_coverage": {
                "complete": coverage["complete"],
                "observed_combinations": coverage["observed_combinations"],
                "total_combinations": coverage["total_combinations"],
                "missing_count": len(coverage.get("missing_combinations", [])),
            },
            "html": html_path,
            "plotly_json": json_path,
        }
        if export_png:
            png_path = workspace.artifacts_dir / f"{stem}.png"
            try:
                fig.write_image(png_path, width=1400, height=850, scale=1.5)
                workspace.register_artifact(png_path, "image", title or chart_type)
                response["png"] = png_path
            except Exception as exc:  # Kaleido may need a local Chrome installation.
                response["png_warning"] = f"PNG 导出失败，但 HTML 图表已生成：{exc}"
        return json_text(response)

    @tool
    def export_data(format: Literal["csv", "xlsx", "parquet"] = "csv", filename: str = "analysis_result") -> str:
        """Export the current active dataset as CSV, Excel or Parquet and register the artifact."""
        safe_stem = re.sub(r"[^\w\-\u4e00-\u9fff]", "_", Path(filename).stem)[:60] or "analysis_result"
        path = workspace.save_dataframe(f"{safe_stem}.{format}")
        return json_text({"status": "ok", "rows": len(workspace.dataframe), "output": path})

    return [
        inspect_data,
        repair_data_format,
        clean_data,
        transform_data,
        statistical_analysis,
        create_visualization,
        export_data,
    ]
