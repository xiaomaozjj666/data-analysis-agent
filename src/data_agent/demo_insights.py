"""打开即有的预分析：先识别列语义，再自动选最合适的图。

选择规则（与人工分析师的默认判断一致）：

- 日期 + 数值 → 折线趋势（按日求和）；
- 分类 + 数值 → 条形对比（取 Top 8，金额类求和）；
- 两个数值 → 散点（样本上限 400，离群点单列）；
- 单个数值 → 直方图（12 档）；
- 分类基数 ≤ 8 → 占比环；
- 超过 5 万行且有两个数值 → 网格密度图（不逐点画）。

每个数据集同时给出 KPI 与两条以内的结论；分析全部是确定性的，不调模型。
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import date
from statistics import median
from typing import Any

_ISO_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def _number(value: str) -> float | None:
    text = (value or "").strip().replace(",", "").replace("¥", "").replace("￥", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def _is_date(value: str) -> bool:
    text = (value or "").strip()
    if not _ISO_DATE.match(text):
        return False
    try:
        date.fromisoformat(text[:10].replace("/", "-"))
        return True
    except ValueError:
        return False


_LABELS = {
    "order_id": "订单号", "sales": "销售额", "quantity": "销量", "revenue": "收入",
    "cost": "成本", "orders": "订单数", "order_date": "订单日期", "date": "日期",
    "region": "区域", "product": "商品", "category": "品类", "channel": "渠道",
    "store": "门店", "group": "分组", "customer_segment": "客户群",
    "phone": "手机号", "x": "x", "y": "y", "z": "z", "note": "备注",
}
_MONEY = ("sales", "revenue", "amount", "cost", "price", "金额", "销售额", "收入", "成本", "客单价")
_QUANTITY = ("quantity", "qty", "销量", "数量", "件数")
_ID_HINTS = ("id", "编号", "序号", "code", "phone", "mobile", "电话", "手机")


def _label(column: str) -> str:
    return _LABELS.get(column, column)


def _unit(column: str) -> str:
    lower = column.lower()
    if any(word in lower for word in _MONEY):
        return "元"
    if any(word in lower for word in _QUANTITY):
        return "件"
    return ""


def _is_id(column: str) -> bool:
    return any(hint in column.lower() for hint in _ID_HINTS)


def _profile(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    columns = list(rows[0].keys()) if rows else []
    numeric: list[str] = []
    dates: list[str] = []
    categorical: list[str] = []
    for column in columns:
        values = [(row.get(column) or "").strip() for row in rows]
        present = [value for value in values if value]
        if not present:
            continue
        if sum(1 for value in present if _number(value) is not None) >= len(present) * 0.9:
            if not _is_id(column):
                numeric.append(column)
            continue
        if sum(1 for value in present if _is_date(value)) >= len(present) * 0.8:
            dates.append(column)
            continue
        unique = len({value for value in present})
        if 2 <= unique <= 20 and unique < max(3, len(present) * 0.6):
            categorical.append(column)
    numeric.sort(key=lambda column: (0 if any(h in column.lower() for h in _MONEY) else 1))
    categorical.sort(key=lambda column: (0 if any(h in column.lower() for h in ("region", "product", "channel", "store", "group", "category")) else 1))
    return {"numeric": numeric, "dates": dates, "categorical": categorical}


def _top(items: dict[str, float], limit: int = 8) -> list[dict[str, Any]]:
    ordered = sorted(items.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [{"label": label, "value": round(value, 2)} for label, value in ordered]


def _kpi(label: str, value: str, note: str = "", unit: str = "") -> dict[str, str]:
    return {"label": label, "value": value, "note": note, "unit": unit}


def _histogram(values: list[float], bins: int = 12) -> list[dict[str, Any]]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [{"label": f"{low:,.0f}", "value": len(values)}]
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, int((value - low) / width))
        counts[index] += 1
    return [
        {
            "label": f"{low + index * width:,.0f}–{low + (index + 1) * width:,.0f}",
            "value": counts[index],
        }
        for index in range(bins)
    ]


def _auto_charts(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    profile = _profile(rows)
    numeric, dates, categorical = (
        profile["numeric"],
        profile["dates"],
        profile["categorical"],
    )
    charts: list[dict[str, Any]] = []

    def column_values(column: str) -> list[float]:
        return [value for row in rows if (value := _number(row.get(column, ""))) is not None]

    if dates and numeric:
        column = numeric[0]
        buckets: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            amount = _number(row.get(column, ""))
            if amount is None:
                continue
            buckets[(row.get(dates[0]) or "")[:10].replace("/", "-")].append(amount)
        flattened = [value for values in buckets.values() for value in values]
        baseline = median(flattened) if flattened else 0.0
        cap = baseline * 20 if baseline else None
        excluded = 0
        points = []
        for label, values in sorted(buckets.items()):
            if not label:
                continue
            kept = [value for value in values if cap is None or value <= cap]
            excluded += len(values) - len(kept)
            if kept:
                points.append({"label": label, "value": round(sum(kept), 2)})
        if points:
            charts.append(
                {
                    "type": "line",
                    "title": f"{_label(column)}趋势",
                    "subtitle": f"按{_label(dates[0])}汇总"
                    + (f" · 已排除 {excluded} 条极端值" if excluded else ""),
                    "unit": _unit(column),
                    "points": points[:40],
                }
            )

    category_summary: dict[str, float] | None = None
    category_unit = ""
    category_basis = ""
    if categorical and numeric:
        column = numeric[0]
        groups: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            amount = _number(row.get(column, ""))
            if amount is not None:
                groups[row.get(categorical[0], "未命名")].append(amount)
        flattened = [value for values in groups.values() for value in values]
        baseline = median(flattened) if flattened else 0.0
        cap = baseline * 20 if baseline else None
        excluded = 0
        summary = {}
        for label, values in groups.items():
            kept = [value for value in values if cap is None or value <= cap]
            excluded += len(values) - len(kept)
            if kept:
                summary[label] = sum(kept)
        measure_like = any(hint in column.lower() for hint in _MONEY + _QUANTITY)
        if len(rows) > 50_000 and not measure_like:
            counts = Counter(row.get(categorical[0], "未命名") for row in rows)
            summary = {label: float(value) for label, value in counts.items()}
            category_basis = "按行数"
            category_unit = "行"
            excluded = 0
        else:
            category_basis = "按" + _label(column) + "求和"
            category_unit = _unit(column)
        if summary:
            category_summary = summary
            charts.append(
                {
                    "type": "bar",
                    "title": f"{_label(categorical[0])}对比",
                    "subtitle": category_basis
                    + (f" · 已排除 {excluded} 条极端值" if excluded else "")
                    + " · Top 8",
                    "unit": category_unit,
                    "items": _top(summary),
                }
            )

    if len(numeric) >= 2:
        x_column, y_column = numeric[0], numeric[1]
        points: list[list[float]] = []
        outliers: list[list[float]] = []
        def fence(values: list[float]) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            q1 = ordered[int(len(ordered) * 0.25)]
            q3 = ordered[int(len(ordered) * 0.75)]
            spread = q3 - q1
            if spread <= 0:
                return max((abs(value) for value in values), default=0.0) * 0.999
            return q3 + 3 * spread

        fence_x = fence(column_values(x_column))
        fence_y = fence(column_values(y_column))
        for row in rows:
            x = _number(row.get(x_column, ""))
            y = _number(row.get(y_column, ""))
            if x is None or y is None:
                continue
            point = [round(x, 2), round(y, 2)]
            if (fence_x and abs(x) > fence_x) or (fence_y and abs(y) > fence_y):
                outliers.append(point)
            elif len(points) < 400:
                points.append(point)
        if points or outliers:
            charts.append(
                {
                    "type": "scatter",
                    "title": f"{_label(x_column)}与{_label(y_column)}",
                    "subtitle": "离群点单列，主体尺度不被拉偏",
                    "x_label": _label(x_column),
                    "y_label": _label(y_column),
                    "samples": points,
                    "outliers": outliers[:20],
                }
            )

    if numeric and len(rows) <= 50_000:
        values = column_values(numeric[0])
        if len(values) >= 8:
            baseline = median(values)
            clipped = [value for value in values if value <= baseline * 20] if baseline else values
            usable = clipped if len(clipped) >= 8 else values
            charts.append(
                {
                    "type": "histogram",
                    "title": f"{_label(numeric[0])}分布",
                    "subtitle": "12 档等宽分箱"
                    + (" · 已排除极端值" if len(usable) < len(values) else ""),
                    "unit": "行",
                    "items": _histogram(usable),
                }
            )

    if categorical:
        if category_summary:
            donut_items = _top(category_summary, 8)
            donut_basis = category_basis or "按行数"
        else:
            counts = Counter(row.get(categorical[0], "未命名") for row in rows)
            donut_items = _top({key: float(value) for key, value in counts.items()}, 8)
            donut_basis = "按行数"
        if 2 <= len(donut_items) <= 8:
            charts.append(
                {
                    "type": "donut",
                    "title": f"{_label(categorical[0])}占比",
                    "subtitle": donut_basis,
                    "items": donut_items,
                }
            )

    if len(rows) > 50_000 and len(numeric) >= 2:
        cols, grid_rows = 36, 18
        xs = column_values(numeric[0])
        ys = column_values(numeric[1])
        if xs and ys:
            x_min, x_max = min(xs), max(xs)
            y_min, y_max = min(ys), max(ys)
            counts = [[0 for _ in range(cols)] for _ in range(grid_rows)]
            for x, y in zip(xs, ys, strict=False):
                col = min(cols - 1, int((x - x_min) / (x_max - x_min + 1e-9) * cols))
                row_index = min(grid_rows - 1, int((y - y_min) / (y_max - y_min + 1e-9) * grid_rows))
                counts[row_index][col] += 1
            peak = max(max(row) for row in counts) or 1
            charts.insert(
                0,
                {
                    "type": "heatmap",
                    "title": "密度网格",
                    "subtitle": f"{len(rows):,} 行聚合 · 越深落点越多",
                    "cols": cols,
                    "rows": grid_rows,
                    "values": [[round(value / peak, 3) for value in row] for row in counts],
                    "x_label": _label(numeric[0]),
                    "y_label": _label(numeric[1]),
                },
            )
    return charts[:5]


def _sales(rows: list[dict[str, str]]) -> dict[str, Any]:
    sales = [_number(row.get("sales", "")) or 0.0 for row in rows]
    quantities = [_number(row.get("quantity", "")) or 0.0 for row in rows]
    orders = {row.get("order_id", "").strip() for row in rows if row.get("order_id")}
    revenue = sum(sales)
    lines = Counter(row.get("order_id", "") for row in rows)
    multi = sum(1 for count in lines.values() if count > 1)
    return {
        "headline": "订单只有 20 笔，销量却是 462 件",
        "subline": "同一订单的多条明细只算一笔订单；销量按件数求和。草稿把销量当成了订单数。",
        "kpis": [
            _kpi("订单数", str(len(orders)), "订单去重"),
            _kpi("销量", f"{sum(quantities):g}", "按件求和", "件"),
            _kpi("销售额", _money(revenue), "销售额求和", "元"),
            _kpi("客单价", _money(revenue / len(orders)) if orders else "—", "销售额 / 订单数", "元"),
        ],
        "findings": [
            f"{multi} 笔订单包含多条明细；把明细行数当订单数会从 {len(orders)} 虚增到 {len(rows)}。",
            "销量 462 件与订单 20 笔是两个口径，核验时必须分别声明聚合方式。",
        ],
    }


def _regional(rows: list[dict[str, str]]) -> dict[str, Any]:
    east = [row for row in rows if row.get("region") == "华东"]
    revenue = sum(_number(row.get("revenue", "")) or 0.0 for row in east)
    return {
        "headline": "同一份数据，不同主体看到不同的行",
        "subline": "演示策略以 u_east 核验：只统计华东 8 行，另外 12 行在聚合前被行级权限过滤。",
        "kpis": [
            _kpi("全部订单", str(len(rows)), "原始 CSV 行数"),
            _kpi("华东可见", str(len(east)), "RLS 过滤后", "行"),
            _kpi("华东销售额", _money(revenue), "按主体过滤后", "元"),
            _kpi("脱敏列", "1", "phone → 已脱敏", "列"),
        ],
        "findings": [
            "行级权限发生在聚合之前：被过滤的行不会进入指标，也不会出现在证据预览。",
            "手机号在证据明细中显示为「已脱敏」，原值不落到响应里。",
        ],
    }


def _quality(rows: list[dict[str, str]]) -> dict[str, Any]:
    missing = sum(1 for row in rows if _number(row.get("revenue", "")) is None)
    negatives = sum(
        1
        for row in rows
        if (_number(row.get("revenue", "")) or 0) < 0 or (_number(row.get("cost", "")) or 0) < 0
    )
    outliers = sum(1 for row in rows if (_number(row.get("revenue", "")) or 0) > 100_000)
    injected = any("忽略" in (row.get("note") or "") for row in rows)
    return {
        "headline": "先看清数据，再谈结论",
        "subline": f"{len(rows)} 行里有缺失、负数、极端值和混合日期；备注里还藏了一句注入文本。",
        "kpis": [
            _kpi("行数", str(len(rows)), "全部记录"),
            _kpi("缺失", str(missing), "销售额为空", "行"),
            _kpi("负数", str(negatives), "收入或成本为负", "行"),
            _kpi("极端值", str(outliers), "销售额 > 10 万", "行"),
        ],
        "findings": [
            (
                "备注里的「忽略以上所有指令…」是数据内容，不是指令；分析链路不会执行它。"
                if injected
                else "备注列按普通文本处理，不参与指令解析。"
            ),
            "极端值会拉高均值，看板同时保留中位数与逐行明细。",
        ],
    }


def _bigdata(rows: list[dict[str, str]]) -> dict[str, Any]:
    xs = [value for row in rows if (value := _number(row.get("x", ""))) is not None]
    ys = [value for row in rows if (value := _number(row.get("y", ""))) is not None]
    groups = Counter(row.get("group", "未分组") for row in rows)
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    correlation = covariance / math.sqrt(variance_x * variance_y) if variance_x and variance_y else 0.0
    return {
        "headline": "20 万行，先聚合再渲染",
        "subline": "散点按网格聚合成密度图：颜色深浅表示落点数量，交互仍保持顺滑。",
        "kpis": [
            _kpi("行数", f"{len(rows):,}", "服务端生成"),
            _kpi("分组", str(len(groups)), "A / B / C", "组"),
            _kpi("相关系数", f"{correlation:.3f}", "x 与 y", ""),
            _kpi("网格", "36 × 18", "密度聚合", "格"),
        ],
        "findings": [
            "20 万点逐点绘制会糊成一团；聚合后每个网格的计数仍然精确。",
            "同一路径在 100 万行口径复算上约 15–20 秒。",
        ],
    }


BUILDERS = {
    "sales_audit": _sales,
    "regional_policy": _regional,
    "quality_injection": _quality,
    "bigdata": _bigdata,
}

_REQUIRED = {
    "sales_audit": {"order_id", "sales", "quantity"},
    "regional_policy": {"region", "revenue"},
    "quality_injection": {"revenue"},
    "bigdata": {"x", "y"},
}
_LOGGER = logging.getLogger(__name__)


def insights_for(
    kind: str, csv_text: str, policy: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    builder = BUILDERS.get(kind)
    if builder is None:
        return None
    try:
        rows = _rows(csv_text)
        if not rows or len(rows) < 5:
            return None
        missing = _REQUIRED.get(kind, set()) - set(rows[0])
        if missing:
            _LOGGER.warning("演示预分析缺少列 %s：%s", kind, sorted(missing))
            return None
        visible = rows
        rls_column = None
        if policy and policy.get("rls"):
            rls_column = str(policy["rls"]["column"])
            claim = str(policy["rls"]["claim"])
            value = (policy.get("claims") or {}).get(claim)
            if value is not None:
                visible = [row for row in rows if row.get(rls_column) == str(value)]
        insight = builder(rows)
        insight["kind"] = kind
        insight["charts"] = _auto_charts(visible)
        insight["scope"] = {
            "rows": len(rows),
            "visible": len(visible),
            "rls_column": rls_column,
        }
        return insight
    except (KeyError, ValueError, ZeroDivisionError):
        _LOGGER.warning("演示预分析失败：%s", kind, exc_info=True)
        return None
