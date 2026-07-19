from __future__ import annotations

import operator
import re
from html import escape
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import numpy as np
import pandas as pd
import plotly.express as px
from langchain_core.tools import BaseTool, tool
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace


def _checked_columns(df: pd.DataFrame, columns: list[str] | None) -> list[str]:
    result = list(df.columns) if not columns else columns
    missing = [column for column in result if column not in df.columns]
    if missing:
        raise ValueError(f"列不存在：{missing}。可用列：{list(df.columns)}")
    return result


def _numeric_columns(df: pd.DataFrame, columns: list[str] | None = None) -> list[str]:
    candidates = _checked_columns(df, columns) if columns else list(df.select_dtypes(include=np.number))
    result = [column for column in candidates if pd.api.types.is_numeric_dtype(df[column])]
    if not result:
        raise ValueError("没有可用于该操作的数值列。")
    return result


def _safe_stem(title: str | None, fallback: str) -> str:
    raw = title or fallback
    stem = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", raw).strip("_")[:50]
    return f"{stem or fallback}_{uuid4().hex[:8]}"


def _normalize_column_names(
    df: pd.DataFrame,
    columns: list[str] | None,
    datetime_columns: list[str] | None,
) -> tuple[pd.DataFrame, list[str] | None, list[str] | None, bool]:
    """Lowercase and sanitize column names; remap caller-provided selections."""
    original = list(df.columns)
    normalized: list[str] = []
    seen: dict[str, int] = {}
    for value in original:
        base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(value).strip().lower()).strip("_") or "column"
        seen[base] = seen.get(base, 0) + 1
        normalized.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    if normalized == original:
        return df, columns, datetime_columns, False
    df = df.copy()
    df.columns = normalized
    mapping = dict(zip(original, normalized, strict=True))
    if columns:
        columns = [mapping.get(value, value) for value in columns]
    if datetime_columns:
        datetime_columns = [mapping.get(value, value) for value in datetime_columns]
    return df, columns, datetime_columns, True


def _trim_string_columns(df: pd.DataFrame) -> int:
    """Trim leading/trailing whitespace on object/string columns in place."""
    string_columns = list(df.select_dtypes(include=["object", "string"]).columns)
    for column in string_columns:
        df[column] = df[column].map(lambda value: value.strip() if isinstance(value, str) else value)
    return len(string_columns)


def _parse_numeric_columns(df: pd.DataFrame, threshold: float = 0.8) -> list[str]:
    """Coerce object/string columns whose numeric ratio meets ``threshold``."""
    converted: list[str] = []
    for column in df.select_dtypes(include=["object", "string"]).columns:
        non_empty = df[column].dropna()
        if non_empty.empty:
            continue
        candidate = pd.to_numeric(df[column], errors="coerce")
        if candidate.notna().sum() / len(non_empty) >= threshold:
            df[column] = candidate
            converted.append(str(column))
    return converted


def _apply_missing_strategy(
    df: pd.DataFrame,
    selected: list[str],
    strategy: str,
) -> None:
    """Apply the requested missing-value strategy in place."""
    if strategy == "drop":
        old_len = len(df)
        df.dropna(subset=selected, inplace=True)
        df.reset_index(drop=True, inplace=True)
        dropped = old_len - len(df)
        if dropped > 0 and dropped / max(old_len, 1) > 0.5:
            raise ValueError(
                f"拒绝高比例删行：将删除 {dropped}/{old_len} 行。"
                "请改用填充策略，或先让用户确认后分批处理。"
            )
        return
    if strategy in {"forward_fill", "backward_fill"}:
        method = "ffill" if strategy == "forward_fill" else "bfill"
        df[selected] = getattr(df[selected], method)()
        return
    for column in selected:
        series = df[column]
        if not series.isna().any():
            continue
        if strategy in {"mean", "median"}:
            if not pd.api.types.is_numeric_dtype(series):
                raise ValueError(f"列 {column} 不是数值列，不能使用 {strategy} 填充。")
            fill_value = getattr(series, strategy)()
        else:
            modes = series.mode(dropna=True)
            if modes.empty:
                continue
            fill_value = modes.iloc[0]
        df[column] = series.fillna(fill_value)


def _handle_outliers(
    df: pd.DataFrame,
    selected: list[str],
    method: str,
    action: str,
) -> tuple[int, dict[str, tuple[float, float]]]:
    """Detect and optionally cap/remove outliers on numeric selected columns."""
    numeric = _numeric_columns(df, selected)
    masks: dict[str, pd.Series] = {}
    bounds: dict[str, tuple[float, float]] = {}
    for column in numeric:
        series = df[column]
        if method == "iqr":
            q1, q3 = series.quantile([0.25, 0.75])
            spread = q3 - q1
            lower, upper = float(q1 - 1.5 * spread), float(q3 + 1.5 * spread)
        else:
            mean, std = float(series.mean()), float(series.std(ddof=0))
            if std == 0 or not np.isfinite(std):
                continue
            lower, upper = mean - 3 * std, mean + 3 * std
        bounds[column] = (lower, upper)
        masks[column] = series.lt(lower) | series.gt(upper)
    count = int(pd.DataFrame(masks).any(axis=1).sum()) if masks else 0
    if action == "remove" and masks:
        if count / max(len(df), 1) > 0.3:
            raise ValueError(
                f"拒绝一次删除过多离群记录：将删除 {count}/{len(df)} 行。"
                "请改用 cap，或缩小需要检查的列。"
            )
        df.drop(pd.DataFrame(masks).any(axis=1).loc[lambda s: s].index, inplace=True)
        df.reset_index(drop=True, inplace=True)
    elif action == "cap":
        for column, (lower, upper) in bounds.items():
            df[column] = df[column].clip(lower, upper)
    return count, bounds


def build_tools(workspace: DataWorkspace) -> list[BaseTool]:
    """Create session-bound tools used by the ReAct agent."""

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
        Missing-value deletion over 50% and outlier deletion over 30% are always refused.
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
        """Apply a safe selection, single filter, sort and row limit to the active dataset.

        Use 'in' with a list. Use 'contains' for text. The transformed data replaces the active
        data and is exported as transformed_data.csv.
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
                mask = series.isin(filter_value)
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
                    comparison = float(filter_value)  # type: ignore[arg-type]
                mask = operations[filter_operator](series, comparison)
            df = df.loc[mask].copy()
        if sort_by:
            _checked_columns(df, sort_by)
            df = df.sort_values(sort_by, ascending=ascending)
        if select_columns:
            _checked_columns(df, select_columns)
            df = df[select_columns].copy()
        if limit is not None:
            if not 1 <= limit <= 1_000_000:
                raise ValueError("limit 必须在 1 到 1,000,000 之间。")
            df = df.head(limit).copy()
        workspace.dataframe = df.reset_index(drop=True)
        output = workspace.save_dataframe("transformed_data.csv")
        return json_text({"status": "ok", "rows": len(df), "columns": list(df.columns), "output": output})

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
        """
        df = workspace.dataframe
        if not 0 < alpha < 1:
            raise ValueError("alpha 必须在 0 到 1 之间。")
        result: dict[str, Any] = {"method": method, "alpha": alpha}

        if method == "descriptive":
            selected = _checked_columns(df, columns)
            result["result"] = df[selected].describe(include="all").transpose().reset_index(names="column")
        elif method == "correlation":
            numeric = _numeric_columns(df, columns)
            result["columns"] = numeric
            result["result"] = df[numeric].corr().round(6).to_dict()
            p_values: dict[str, dict[str, float | None]] = {column: {} for column in numeric}
            sample_sizes: dict[str, dict[str, int]] = {column: {} for column in numeric}
            for left in numeric:
                for right in numeric:
                    pair = df[[left, right]].dropna()
                    sample_sizes[left][right] = len(pair)
                    if left == right:
                        p_values[left][right] = 0.0
                    elif len(pair) < 3 or pair[left].nunique() < 2 or pair[right].nunique() < 2:
                        p_values[left][right] = None
                    else:
                        p_values[left][right] = float(stats.pearsonr(pair[left], pair[right]).pvalue)
            result["p_values"] = p_values
            result["sample_sizes"] = sample_sizes
        elif method == "groupby":
            if not group_by:
                raise ValueError("groupby 方法需要 group_by。")
            _checked_columns(df, [group_by])
            values = _numeric_columns(df, columns) if aggregation != "count" else _checked_columns(df, columns)
            grouped = df.groupby(group_by, dropna=False)[values].agg(aggregation).reset_index()
            result["result"] = grouped.head(500)
            result["truncated"] = len(grouped) > 500
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
                eta_squared=float(between / total) if total else 0.0,
            )
        elif method == "chi_square":
            selected = _checked_columns(df, columns)
            if len(selected) != 2:
                raise ValueError("卡方检验需要恰好两个分类列。")
            cardinality = {column: int(df[column].nunique(dropna=True)) for column in selected}
            max_cardinality = max(cardinality.values())
            if max_cardinality > 100:
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
        chart_type: Literal["bar", "line", "area", "scatter", "scatter_3d", "histogram", "box", "violin", "pie", "heatmap", "correlation_heatmap", "scatter_matrix", "sunburst", "treemap"],
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
        bins: int = 30,
        top_n: int | None = None,
        export_png: bool = False,
    ) -> str:
        """Create an interactive Plotly chart and save a standalone HTML artifact.

        Supports standard and complex plots including 3D scatter, correlation heatmap, scatter
        matrix, sunburst and treemap. For bar/line/area, aggregation groups by x and optional
        color. heatmap expects x, y and numeric values. scatter_matrix uses dimensions.
        sunburst/treemap use path_columns and values. export_png is best-effort and needs Chrome.
        """
        df = workspace.dataframe.copy()
        requested = [value for value in [x, y, color, z, size, values] if value]
        requested.extend(path_columns or [])
        requested.extend(dimensions or [])
        _checked_columns(df, requested)
        if top_n is not None:
            if not x or not 1 <= top_n <= 500:
                raise ValueError("top_n 需要 x，且必须在 1 到 500 之间。")
            keep = df[x].value_counts(dropna=False).head(top_n).index
            df = df[df[x].isin(keep)].copy()

        groupable = chart_type in {"bar", "line", "area"}
        if aggregation != "none":
            if not groupable or not x:
                raise ValueError("aggregation 仅用于带 x 的 bar/line/area。")
            group_columns = [x] + ([color] if color else [])
            if aggregation == "count":
                df = df.groupby(group_columns, dropna=False).size().reset_index(name="count")
                y = "count"
            else:
                if not y:
                    raise ValueError(f"{aggregation} 聚合需要 y。")
                df = df.groupby(group_columns, dropna=False)[y].agg(aggregation).reset_index()

        common = {"data_frame": df, "title": title, "template": "plotly_white"}
        if chart_type == "bar":
            fig = px.bar(**common, x=x, y=y, color=color, barmode="group")
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
            if len(numeric) > 8:
                raise ValueError("scatter_matrix 最多支持 8 个维度，请缩小 dimensions。")
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

        fig.update_layout(
            font={"family": "IBM Plex Sans, Noto Sans SC, sans-serif", "color": "#102a2a"},
            paper_bgcolor="#fbfaf5",
            plot_bgcolor="#fbfaf5",
            margin={"l": 40, "r": 30, "t": 70, "b": 40},
            hoverlabel={"bgcolor": "#102a2a", "font_color": "white"},
        )
        stem = _safe_stem(title, chart_type)
        html_path = workspace.artifacts_dir / f"{stem}.html"
        shared_plotly = workspace.ensure_plotly_bundle()
        relative_script = shared_plotly.relative_to(workspace.artifacts_dir).as_posix() if shared_plotly else None
        html_template = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>{title}</title><script src='{script}'></script>"
            "<style>html,body{{margin:0;background:#fbfaf5}}body{{min-height:100vh}}</style>"
            "</head><body>{div}</body></html>"
            if relative_script
            else None
        )
        if html_template is not None:
            div = fig.to_html(full_html=False, include_plotlyjs=False)
            html_path.write_text(
                html_template.format(
                    title=escape(title or chart_type),
                    script=relative_script,
                    div=div,
                ),
                encoding="utf-8",
            )
        else:
            fig.write_html(html_path, include_plotlyjs=True, full_html=True)
        workspace.register_artifact(html_path, "visualization", title or chart_type)
        json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
        fig.write_json(json_path)
        workspace.register_artifact(json_path, "chart_data", "Plotly figure JSON")

        response: dict[str, Any] = {
            "status": "ok",
            "chart_type": chart_type,
            "rows_plotted": len(df),
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
