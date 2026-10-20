"""统计分析实现：与 builder.statistical_analysis 的参数一一对应。"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from data_agent.tools.charts import _checked_columns, _numeric_columns

from ._limits import _CHI_SQUARE_MAX_CARDINALITY, _GROUPBY_MAX_ROWS


def _two_group_series(
    df: pd.DataFrame,
    target: str,
    group_by: str,
    groups: list[str] | None,
) -> tuple[pd.Series, pd.Series, str, str]:
    _checked_columns(df, [group_by, target])
    if not pd.api.types.is_numeric_dtype(df[target]):
        raise ValueError("分组 t 检验的 target 必须是数值列。")
    levels = [str(item) for item in df[group_by].dropna().unique().tolist()]
    if groups:
        wanted = [str(item) for item in groups]
        if len(wanted) != 2:
            raise ValueError("groups 必须恰好给出两个分组取值。")
        missing = [item for item in wanted if item not in levels]
        if missing:
            raise ValueError(f"分组取值 {missing} 不存在。可用取值：{levels[:20]}。")
        left_name, right_name = wanted
    else:
        if len(levels) != 2:
            raise ValueError(
                f"分组列 {group_by} 有 {len(levels)} 个取值，独立 t 检验需要恰好两组。"
                "请用 anova，或传入 groups=['组A','组B'] 指定要对比的两个取值。"
            )
        left_name, right_name = levels[0], levels[1]
    left = df.loc[df[group_by].astype(str) == left_name, target].dropna()
    right = df.loc[df[group_by].astype(str) == right_name, target].dropna()
    if len(left) < 2 or len(right) < 2:
        raise ValueError("每个分组至少需要 2 个有效观测值。")
    return left, right, left_name, right_name


def _welch_from_series(first: pd.Series, second: pd.Series, alpha: float) -> dict[str, Any]:
    test = stats.ttest_ind(first, second, equal_var=False)
    pooled = np.sqrt(
        ((first.var(ddof=1) * (len(first) - 1)) + (second.var(ddof=1) * (len(second) - 1)))
        / max(len(first) + len(second) - 2, 1)
    )
    effect_size = float((first.mean() - second.mean()) / pooled) if pooled else 0.0
    confidence = test.confidence_interval(confidence_level=1 - alpha)
    return {
        "statistic": float(test.statistic),
        "p_value": float(test.pvalue),
        "significant": bool(test.pvalue < alpha),
        "sample_size": int(len(first) + len(second)),
        "group_sizes": {"first": int(len(first)), "second": int(len(second))},
        "means": {"first": float(first.mean()), "second": float(second.mean())},
        "mean_difference": float(first.mean() - second.mean()),
        "confidence_interval": {"low": float(confidence.low), "high": float(confidence.high)},
        "effect_size": float(effect_size),
        "effect_size_name": "cohens_d",
    }


def run_statistical_analysis(
    df: pd.DataFrame,
    method: str,
    columns: list[str] | None = None,
    group_by: str | None = None,
    target: str | None = None,
    aggregation: Literal["mean", "median", "sum", "count", "min", "max", "std"] = "mean",
    alpha: float = 0.05,
    groups: list[str] | None = None,
    window: int = 7,
    periods: int = 1,
) -> dict[str, Any]:
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须在 0 到 1 之间。")
    result: dict[str, Any] = {"method": method, "alpha": alpha}

    if method == "descriptive":
        selected = _checked_columns(df, columns)
        desc = df[selected].describe(include="all").transpose().reset_index(names="column")
        numeric_selected = [c for c in selected if pd.api.types.is_numeric_dtype(df[c])]
        if numeric_selected:
            skew = df[numeric_selected].skew().rename("skewness")
            kurt = df[numeric_selected].kurtosis().rename("kurtosis")
            shape_df = pd.DataFrame(
                {"column": numeric_selected, "skewness": skew.values, "kurtosis": kurt.values}
            )
            desc = desc.merge(shape_df, on="column", how="left")
        result["result"] = desc
        return result

    if method == "correlation":
        numeric = _numeric_columns(df, columns)
        result["columns"] = numeric
        result["result"] = df[numeric].corr().round(6).to_dict()
        numeric_df = df[numeric]
        p_values: dict[str, dict[str, float | None]] = {column: {} for column in numeric}
        sample_sizes: dict[str, dict[str, int]] = {column: {} for column in numeric}
        pair_count = 0
        for i, left in enumerate(numeric):
            p_values[left][left] = 0.0
            sample_sizes[left][left] = int(numeric_df[left].notna().sum())
            left_series = numeric_df[left]
            for right in numeric[i + 1 :]:
                pair_count += 1
                right_series = numeric_df[right]
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
        adjusted = alpha / max(pair_count, 1)
        significant_after: dict[str, dict[str, bool | None]] = {column: {} for column in numeric}
        for left in numeric:
            for right in numeric:
                p_val = p_values[left].get(right)
                if left == right:
                    significant_after[left][right] = False
                elif p_val is None:
                    significant_after[left][right] = None
                else:
                    significant_after[left][right] = bool(p_val < adjusted)
        result["p_values"] = p_values
        result["sample_sizes"] = sample_sizes
        result["pair_count"] = pair_count
        result["bonferroni_alpha"] = adjusted
        result["significant_after_bonferroni"] = significant_after
        result["multiple_comparison"] = (
            f"共检验 {pair_count} 对相关；未校正显著不等于同时成立。"
            f"Bonferroni 校正阈值 α={adjusted:.6g}。"
        )
        return result

    if method == "groupby":
        if not group_by:
            raise ValueError("groupby 方法需要 group_by。")
        _checked_columns(df, [group_by])
        values = _numeric_columns(df, columns) if aggregation != "count" else _checked_columns(df, columns)
        grouped = df.groupby(group_by, dropna=False)[values].agg(aggregation).reset_index()
        group_sizes = df.groupby(group_by, dropna=False).size().rename("_group_n_").reset_index()
        grouped = grouped.merge(group_sizes, on=group_by, how="left")
        result["result"] = grouped.head(_GROUPBY_MAX_ROWS)
        result["truncated"] = len(grouped) > _GROUPBY_MAX_ROWS
        result["group_count"] = int(df[group_by].nunique(dropna=False))
        return result

    if method == "ttest_ind":
        if group_by and (target or (columns and len(columns) == 1)):
            value_col = target or columns[0]
            first, second, left_name, right_name = _two_group_series(df, value_col, group_by, groups)
            result.update(_welch_from_series(first, second, alpha))
            result["comparison"] = "two_groups"
            result["group_by"] = group_by
            result["target"] = value_col
            result["groups"] = [left_name, right_name]
            result["means"] = {left_name: result["means"]["first"], right_name: result["means"]["second"]}
            return result
        numeric = _numeric_columns(df, columns)
        if len(numeric) != 2:
            raise ValueError(
                "独立 t 检验请任选其一：① group_by + target（比较两组，如区域×销售额）；"
                "② 恰好两个数值列（把两列当两个样本，很少是业务问题想要的）。"
            )
        pair = df[numeric].dropna()
        first, second = pair[numeric[0]], pair[numeric[1]]
        result.update(_welch_from_series(first, second, alpha))
        result["comparison"] = "two_series"
        result["means"] = {column: float(pair[column].mean()) for column in numeric}
        result["note"] = (
            "这是两列对齐后的独立样本检验，不是「按某列分组对比」。"
            "比较华东 vs 华北请传 group_by 与 target。"
        )
        result["sample_size"] = len(pair)
        return result

    if method == "ttest_paired":
        numeric = _numeric_columns(df, columns)
        if len(numeric) != 2:
            raise ValueError("配对 t 检验需要恰好两个数值列（如处理前/处理后）。")
        pair = df[numeric].dropna()
        first, second = pair[numeric[0]], pair[numeric[1]]
        test = stats.ttest_rel(first, second)
        difference = first - second
        effect_size = (
            float(difference.mean() / difference.std(ddof=1)) if difference.std(ddof=1) else 0.0
        )
        confidence = test.confidence_interval(confidence_level=1 - alpha)
        result.update(
            comparison="paired_columns",
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
        return result

    if method == "anova":
        if not group_by or not target:
            raise ValueError("anova 需要 group_by 和 target。")
        _checked_columns(df, [group_by, target])
        if not pd.api.types.is_numeric_dtype(df[target]):
            raise ValueError("anova target 必须是数值列。")
        groups_data = [
            part[target].dropna().to_numpy()
            for _, part in df.groupby(group_by)
            if part[target].notna().sum() >= 2
        ]
        if len(groups_data) < 2:
            raise ValueError("anova 至少需要两个各含 2 个有效观测值的组。")
        test = stats.f_oneway(*groups_data)
        valid = np.concatenate(groups_data)
        grand_mean = float(valid.mean())
        between = sum(len(group) * (float(group.mean()) - grand_mean) ** 2 for group in groups_data)
        total = float(((valid - grand_mean) ** 2).sum())
        result.update(
            statistic=float(test.statistic),
            p_value=float(test.pvalue),
            significant=bool(test.pvalue < alpha),
            groups=len(groups_data),
            total_n=int(len(valid)),
            eta_squared=float(between / total) if total else 0.0,
            group_means={
                str(name): float(part[target].mean())
                for name, part in df.groupby(group_by)
                if part[target].notna().sum() >= 2
            },
        )
        return result

    if method == "chi_square":
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
        min_expected = float(np.min(expected))
        result.update(
            statistic=float(chi2),
            p_value=float(p_value),
            significant=bool(p_value < alpha),
            degrees_of_freedom=int(dof),
            contingency_table=table.to_dict(),
            min_expected=min_expected,
            expected_frequency_ok=min_expected >= 5,
        )
        if min_expected < 5:
            result["warning"] = (
                f"期望频数最小值 {min_expected:.3g} < 5，渐近卡方 p 值可能不准。"
                "请合并稀疏类别，或改用精确检验思路并在报告中写明局限。"
            )
        return result

    if method == "rolling":
        numeric = _numeric_columns(df, columns)
        if window < 2:
            raise ValueError("rolling 的 window 至少为 2。")
        rolled = df[numeric].rolling(window=window, min_periods=1).agg(aggregation)
        preview = rolled.tail(_GROUPBY_MAX_ROWS)
        result.update(
            window=window,
            aggregation=aggregation,
            columns=numeric,
            result=preview.reset_index(drop=True),
            truncated=len(rolled) > _GROUPBY_MAX_ROWS,
            note="默认按当前行序滚动；时间序列请先按日期列排序。",
        )
        return result

    if method == "pct_change":
        numeric = _numeric_columns(df, columns)
        if periods < 1:
            raise ValueError("pct_change 的 periods 必须 ≥ 1。")
        if group_by:
            _checked_columns(df, [group_by])
            changed = df.groupby(group_by, dropna=False)[numeric].pct_change(periods=periods)
        else:
            changed = df[numeric].pct_change(periods=periods)
        preview = changed.tail(_GROUPBY_MAX_ROWS)
        result.update(
            periods=periods,
            columns=numeric,
            group_by=group_by,
            result=preview.reset_index(drop=True),
            truncated=len(changed) > _GROUPBY_MAX_ROWS,
            note="值为小数涨跌幅（0.1 表示 +10%）；首行/每组首行因没有上期而为空。",
        )
        return result

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
    r2 = float(r2_score(model_data[target], prediction))
    result.update(
        target=target,
        features=features,
        sample_size=len(model_data),
        intercept=float(model.intercept_),
        coefficients=dict(zip(features, map(float, model.coef_), strict=True)),
        r2=r2,
        rmse=float(mean_squared_error(model_data[target], prediction) ** 0.5),
        mae=float(mean_absolute_error(model_data[target], prediction)),
        adjusted_r2=float(
            1 - (1 - r2) * (len(model_data) - 1) / max(len(model_data) - len(features) - 1, 1)
        ),
    )
    return result
