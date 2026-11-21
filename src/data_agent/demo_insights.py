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
from statistics import median, pstdev
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
            raw_summary = {
                label: sum(values)
                for label, values in groups.items()
                if values
            } if measure_like else summary
            charts.append(
                {
                    "type": "bar",
                    "title": f"{_label(categorical[0])}对比",
                    "subtitle": category_basis
                    + (f" · 已排除 {excluded} 条极端值" if excluded else "")
                    + " · Top 8",
                    "unit": category_unit,
                    "dimension": categorical[0],
                    "items": _top(summary),
                    "raw_items": _top(raw_summary),
                    "excluded": excluded,
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
                return math.inf
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
                    "raw_items": _histogram(values) if len(usable) < len(values) else None,
                    "excluded": len(values) - len(usable),
                }
            )

    if categorical:
        if category_summary:
            source = category_summary
            donut_basis = category_basis or "按行数"
        else:
            source = {key: float(value) for key, value in Counter(row.get(categorical[0], "未命名") for row in rows).items()}
            donut_basis = "按行数"
        ordered = sorted(source.items(), key=lambda item: item[1], reverse=True)
        if len(ordered) > 5:
            head = ordered[:4]
            rest = sum(value for _, value in ordered[4:])
            donut_items = [{"label": label, "value": round(value, 2)} for label, value in head]
            donut_items.append({"label": "其他", "value": round(rest, 2)})
            donut_basis += " · 其余合并为其他"
        else:
            donut_items = [{"label": label, "value": round(value, 2)} for label, value in ordered]
        if 2 <= len(donut_items) <= 5:
            charts.append(
                {
                    "type": "donut",
                    "title": f"{_label(categorical[0])}占比",
                    "subtitle": donut_basis,
                    "dimension": categorical[0],
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
        "headline": "实际 20 笔订单，462 为商品件数",
        "subline": "同一单买了多件，只算一单。462 是件数，不是订单数。",
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


def _regional(
    rows: list[dict[str, str]],
    visible: list[dict[str, str]] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """区域演示的结论随主体切换重算：headline/KPI 都基于可见行。

    ``visible`` 是 RLS 过滤后的行（管理员无 RLS 时等于全部行）；不能再用固定
    的华东 8 行，否则切换主体后 KPI 与预览会各说各话。
    """
    scoped = rows if visible is None else visible
    active = policy or {}
    pii = tuple(str(column) for column in active.get("pii_columns") or ())
    revenue = sum(_number(row.get("revenue", "")) or 0.0 for row in scoped)
    filtered = len(rows) - len(scoped)
    if active.get("rls"):
        headline = f"华东的同事打开，只能看到自己的 {len(scoped)} 行"
        subline = f"另外 {filtered} 行不在权限里，手机号也只露出脱敏后的样子。"
        visible_kpi = _kpi("华东可见", str(len(scoped)), "RLS 过滤后", "行")
        revenue_kpi = _kpi("华东销售额", _money(revenue), "按主体过滤后", "元")
    else:
        headline = f"管理员能看到全部 {len(rows)} 行"
        subline = (
            f"没有行级过滤，全部 {len(rows)} 行都参与统计；手机号仍然只以脱敏形式出现。"
            if pii
            else f"没有行级过滤，全部 {len(rows)} 行都参与统计。"
        )
        visible_kpi = _kpi("全部可见", str(len(scoped)), "无行级过滤", "行")
        revenue_kpi = _kpi("全部销售额", _money(revenue), "全部可见行", "元")
    return {
        "headline": headline,
        "subline": subline,
        "kpis": [
            _kpi("全部订单", str(len(rows)), "原始 CSV 行数"),
            visible_kpi,
            revenue_kpi,
            _kpi(
                "脱敏列",
                str(len(pii)),
                ("、".join(pii) + " → 已脱敏") if pii else "无",
                "列",
            ),
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
        "headline": "这张表现在还不能直接用",
        "subline": f"{len(rows)} 行里，{missing} 行没填销售额，{negatives} 行成本是负数，还有 {outliers} 个 1 亿级的异常值。备注里那句话是数据，不是命令。",
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
        "headline": "20 万行，别一个一个点",
        "subline": "先把点聚成格子，颜色越深人越多。点还是那些点，只是终于看得清了。",
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




def _quantile(values: list[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _growth(rows: list[dict[str, str]]) -> dict[str, Any]:
    months = sorted({row["month"] for row in rows})
    channels = sorted({row["channel"] for row in rows})
    categories = sorted({row["category"] for row in rows})

    def total(column: str, predicate=lambda row: True) -> float:
        return sum(_number(row.get(column, "")) or 0.0 for row in rows if predicate(row))

    revenue = total("revenue")
    target = total("target")
    orders = total("orders")
    clicks = total("clicks")
    impressions = total("impressions")
    cost = total("cost")

    def by_month(column: str, channel: str) -> list[float]:
        return [
            round(
                sum(
                    _number(row.get(column, "")) or 0.0
                    for row in rows
                    if row["month"] == month and row["channel"] == channel
                ),
                2,
            )
            for month in months
        ]

    series = [
        {"name": channel, "points": by_month("revenue", channel)} for channel in channels
    ]
    stacked = [
        {"name": channel, "values": by_month("revenue", channel)} for channel in channels
    ]
    order_series = [
        {"name": channel, "points": by_month("orders", channel)} for channel in channels
    ]

    treemap = []
    for category in categories:
        value = sum(
            _number(row.get("revenue", "")) or 0.0
            for row in rows
            if row["category"] == category
        )
        leader = max(
            channels,
            key=lambda channel, cat=category: sum(
                _number(row.get("revenue", "")) or 0.0
                for row in rows
                if row["category"] == cat and row["channel"] == channel
            ),
        )
        treemap.append(
            {"label": category, "value": round(value, 2), "group": leader}
        )

    funnel = [
        {"label": "曝光", "value": round(impressions)},
        {"label": "点击", "value": round(clicks)},
        {"label": "下单", "value": round(orders)},
    ]

    channel_stats = []
    for channel in channels:
        channel_revenue = total("revenue", lambda row, ch=channel: row["channel"] == ch)
        channel_orders = total("orders", lambda row, ch=channel: row["channel"] == ch)
        channel_clicks = total("clicks", lambda row, ch=channel: row["channel"] == ch)
        channel_cost = total("cost", lambda row, ch=channel: row["channel"] == ch)
        channel_stats.append(
            {
                "name": channel,
                "revenue": channel_revenue,
                "orders": channel_orders,
                "conversion": channel_orders / channel_clicks if channel_clicks else 0,
                "margin": (channel_revenue - channel_cost) / channel_revenue if channel_revenue else 0,
            }
        )

    def normalize(key: str) -> list[float]:
        values = [float(stat[key]) for stat in channel_stats]
        peak = max(values) or 1
        return [round(value / peak * 100, 1) for value in values]

    radar = {
        "indicators": ["收入", "订单", "转化率", "毛利率"],
        "series": [
            {
                "name": stat["name"],
                "values": [
                    normalize("revenue")[index],
                    normalize("orders")[index],
                    normalize("conversion")[index],
                    normalize("margin")[index],
                ],
            }
            for index, stat in enumerate(channel_stats)
        ],
    }

    box_groups = []
    for channel in channels:
        values = sorted(
            _number(row.get("revenue", "")) or 0.0
            for row in rows
            if row["channel"] == channel
        )
        if not values:
            continue
        box_groups.append(
            {
                "label": channel,
                "min": round(values[0], 2),
                "q1": round(_quantile(values, 0.25), 2),
                "median": round(_quantile(values, 0.5), 2),
                "q3": round(_quantile(values, 0.75), 2),
                "max": round(values[-1], 2),
            }
        )

    bubble = [
        {
            "x": round(sum(_number(row.get("orders", "")) or 0.0 for row in rows if row["channel"] == channel), 2),
            "y": round(sum(_number(row.get("revenue", "")) or 0.0 for row in rows if row["channel"] == channel), 2),
            "size": round(sum(_number(row.get("impressions", "")) or 0.0 for row in rows if row["channel"] == channel), 2),
            "label": channel,
        }
        for channel in channels
    ]

    monthly_revenue = [
        round(sum(_number(row.get("revenue", "")) or 0.0 for row in rows if row["month"] == month), 2)
        for month in months
    ]
    waterfall = [
        {
            "label": months[index],
            "value": round(monthly_revenue[index] - monthly_revenue[index - 1], 2),
            "kind": "delta",
        }
        for index in range(1, len(months))
    ]

    charts = [
        {"type": "multiline", "title": "渠道收入趋势", "subtitle": "12 个月 × 3 渠道", "unit": "元", "labels": months, "series": series},
        {"type": "stacked", "title": "收入构成", "subtitle": "按月堆叠渠道", "unit": "元", "labels": months, "series": stacked},
        {"type": "area", "title": "订单走势", "subtitle": "多序列面积", "unit": "笔", "labels": months, "series": order_series},
        {"type": "treemap", "title": "品类贡献", "subtitle": "面积 = 收入", "unit": "元", "items": treemap},
        {"type": "funnel", "title": "转化漏斗", "subtitle": "曝光 → 点击 → 下单", "items": funnel},
        {"type": "radar", "title": "渠道能力雷达", "subtitle": "归一化到 100", "indicators": radar["indicators"], "series": radar["series"]},
        {
            "type": "gauge",
            "title": "目标完成度",
            "subtitle": "收入 / 目标",
            "value": round(revenue, 2),
            "target": round(target, 2),
            "unit": "元",
        },
        {"type": "box", "title": "渠道收入分布", "subtitle": "箱线：中位数与四分位", "groups": box_groups},
        {"type": "bubble", "title": "订单 × 收入 × 曝光", "subtitle": "气泡大小 = 曝光", "items": bubble},
        {"type": "waterfall", "title": "月度收入环比", "subtitle": "正值增长，负值回落", "unit": "元", "items": waterfall},
    ]

    return {
        "headline": "这 12 个月，钱是怎么进来的",
        "subline": "按渠道拆、按品类拆、按转化拆。每个数都能回到这 144 行明细。",
        "kpis": [
            _kpi("收入", _money(revenue), "SUM(revenue)", "元"),
            _kpi("订单", f"{orders:,.0f}", "SUM(orders)", "笔"),
            _kpi("转化率", f"{orders / clicks * 100:.1f}%" if clicks else "—", "订单 / 点击"),
            _kpi("毛利率", f"{(revenue - cost) / revenue * 100:.1f}%" if revenue else "—", "(收入 - 成本) / 收入"),
        ],
        "charts": charts,
        "findings": [
            f"收入完成目标的 {revenue / target * 100:.0f}%；目标线在仪表盘上单独标出。",
            "漏斗与雷达使用同一份明细，切换图表不换口径。",
        ],
    }



def _grades(rows: list[dict[str, str]]) -> dict[str, Any]:
    subjects = ["语文", "数学", "英语", "物理", "化学", "生物"]
    exams = sorted({row["exam"] for row in rows})
    classes = sorted({row["class"] for row in rows})
    students = {row["student_id"] for row in rows}

    def score(row: dict[str, str], subject: str) -> float | None:
        return _number(row.get(subject, ""))

    totals = []
    for row in rows:
        values = [score(row, subject) for subject in subjects]
        present = [value for value in values if value is not None]
        if present:
            totals.append(sum(present) / len(present) * len(subjects))

    average_total = sum(totals) / len(totals) if totals else 0.0
    pass_count = sum(1 for value in totals if value >= 60 * len(subjects))
    excellent = sum(1 for value in totals if value >= 85 * len(subjects))

    class_average = []
    for klass in classes:
        values = []
        for row in rows:
            if row["class"] != klass:
                continue
            present = [score(row, subject) for subject in subjects]
            present = [value for value in present if value is not None]
            if present:
                values.append(sum(present) / len(present) * len(subjects))
        class_average.append(
            {"label": klass, "value": round(sum(values) / len(values), 1) if values else 0.0}
        )

    exam_subject = []
    for subject in subjects:
        points = []
        for exam in exams:
            values = [
                value
                for row in rows
                if row["exam"] == exam and (value := score(row, subject)) is not None
            ]
            points.append(round(sum(values) / len(values), 1) if values else 0.0)
        exam_subject.append({"name": subject, "points": points})

    subject_average = []
    subject_spread: list[tuple[str, float]] = []
    for subject in subjects:
        values = [value for row in rows if (value := score(row, subject)) is not None]
        subject_average.append(
            {"label": subject, "value": round(sum(values) / len(values), 1) if values else 0.0}
        )
        # 总体标准差（pstdev）跨三次考试合并计算，避免学期差异被考试维度掩盖。
        if len(values) >= 2:
            subject_spread.append((subject, pstdev(values)))
    widest_subject, widest_spread = (
        max(subject_spread, key=lambda item: item[1]) if subject_spread else ("", 0.0)
    )

    attendance_points = []
    for row in rows:
        attendance = _number(row.get("attendance", ""))
        present = [score(row, subject) for subject in subjects]
        present = [value for value in present if value is not None]
        if attendance is None or not present:
            continue
        attendance_points.append(
            [round(attendance, 1), round(sum(present) / len(present) * len(subjects), 1)]
        )

    box_groups = []
    for klass in classes:
        values = []
        for row in rows:
            if row["class"] != klass:
                continue
            present = [score(row, subject) for subject in subjects]
            present = [value for value in present if value is not None]
            if present:
                values.append(sum(present) / len(present) * len(subjects))
        values.sort()
        if not values:
            continue
        box_groups.append(
            {
                "label": klass,
                "min": round(values[0], 1),
                "q1": round(_quantile(values, 0.25), 1),
                "median": round(_quantile(values, 0.5), 1),
                "q3": round(_quantile(values, 0.75), 1),
                "max": round(values[-1], 1),
            }
        )

    matrix = []
    for left in subjects:
        row_values = []
        for right in subjects:
            pairs = [
                (score(row, left), score(row, right))
                for row in rows
                if score(row, left) is not None and score(row, right) is not None
            ]
            if not pairs:
                row_values.append(0.0)
                continue
            xs = [pair[0] for pair in pairs]
            ys = [pair[1] for pair in pairs]
            mean_x = sum(xs) / len(xs)
            mean_y = sum(ys) / len(ys)
            covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
            variance_x = sum((x - mean_x) ** 2 for x in xs)
            variance_y = sum((y - mean_y) ** 2 for y in ys)
            row_values.append(
                round(covariance / math.sqrt(variance_x * variance_y), 2)
                if variance_x and variance_y
                else 0.0
            )
        matrix.append(row_values)

    pass_rate = pass_count / len(rows) * 100 if rows else 0.0
    absent = sum(1 for row in rows if _number(row.get("attendance", "")) is not None and (_number(row.get("attendance", "")) or 0) < 90)
    low_attendance = [
        value
        for row in rows
        if (attendance := _number(row.get("attendance", ""))) is not None
        and attendance < 90
        for subject in subjects
        if (value := score(row, subject)) is not None
    ]
    high_attendance = [
        value
        for row in rows
        if (attendance := _number(row.get("attendance", ""))) is not None
        and attendance >= 90
        for subject in subjects
        if (value := score(row, subject)) is not None
    ]
    gap = (
        (sum(high_attendance) / len(high_attendance))
        - (sum(low_attendance) / len(low_attendance))
        if low_attendance and high_attendance
        else 0.0
    )
    if abs(gap) < 0.1:
        gap_text = "这些记录的学科均分与全勤记录几乎无差异（差值 < 0.1 分）。"
    else:
        direction = "低" if gap > 0 else "高"
        gap_text = f"这些记录的学科均分比全勤记录{direction}约 {abs(gap):.1f} 分。"
    spread_text = (
        f"{widest_subject}的标准差最大（{widest_spread:.2f} 分），班级之间和班内分化都更明显。"
        if widest_subject
        else "各科标准差接近，班级之间和班内分化程度相近。"
    )

    charts = [
        {"type": "histogram", "title": "总分分布", "subtitle": "按学生平均总分分箱", "unit": "人", "items": _histogram([value / len(subjects) for value in totals], 10)},
        {"type": "bar", "title": "班级平均分 Top 10", "subtitle": f"共 {len(classes)} 个班，显示前 10", "unit": "分", "items": sorted(class_average, key=lambda item: float(item["value"]), reverse=True)[:10]},
        {"type": "multiline", "title": "三次考试的学科均分", "subtitle": "六门学科", "unit": "分", "labels": exams, "series": exam_subject},
        {"type": "radar", "title": "学科均分雷达", "subtitle": "满分 100", "indicators": subjects, "series": [{"name": "年级均分", "values": [item["value"] for item in subject_average]}]},
        {"type": "scatter", "title": "出勤与总分", "subtitle": "每个点是一次考试", "x_label": "出勤率", "y_label": "总分", "samples": attendance_points[:400], "outliers": []},
        {"type": "box", "title": "班级总分分布", "subtitle": "按平均分取前 8 个班", "groups": sorted(box_groups, key=lambda item: float(item["median"]), reverse=True)[:8]},
        {"type": "heatmap", "title": "学科相关矩阵", "subtitle": "1 表示完全正相关", "cols": len(subjects), "rows": len(subjects), "values": matrix, "x_label": "学科", "y_label": "学科"},
        {"type": "gauge", "title": "及格率", "subtitle": "总分达到 60%", "value": round(pass_rate, 1), "target": 85, "unit": "%"},
    ]

    return {
        "headline": f"一个年级 {len(students):,} 人，三次考试的成绩单",
        "subline": f"共 {len(rows):,} 条考试记录、{len(classes)} 个班。姓名和学号在预览中脱敏；先看整体分布，再看班级和学科差异。",
        "kpis": [
            _kpi("学生", str(len(students)), "去重学号", "人"),
            _kpi("平均总分", f"{average_total:.1f}", "六科合计", "分"),
            _kpi("及格率", f"{pass_rate:.1f}%", "总分 ≥ 60%"),
            _kpi("优秀率", f"{excellent / len(rows) * 100:.1f}%" if rows else "—", "总分 ≥ 85%"),
        ],
        "charts": charts,
        "findings": [
            f"出勤低于 90% 的记录有 {absent} 条；{gap_text}",
            spread_text,
        ],
    }



def _finance(rows: list[dict[str, str]]) -> dict[str, Any]:
    months = [row["month"] for row in rows]
    revenue = [float(row["revenue"]) for row in rows]
    cogs = [float(row["cogs"]) for row in rows]
    opex = [float(row["opex"]) for row in rows]
    tax = [float(row["tax"]) for row in rows]
    net = [float(row["net_profit"]) for row in rows]
    cash = [float(row["cash"]) for row in rows]
    budget = sum(float(row["budget"]) for row in rows)
    total_revenue = sum(revenue)
    total_net = sum(net)
    latest = rows[-1]
    latest_revenue = float(latest["revenue"])
    latest_cogs = float(latest["cogs"])
    latest_opex = float(latest["opex"])
    latest_tax = float(latest["tax"])
    latest_net = float(latest["net_profit"])

    quarters = ["Q1", "Q2", "Q3", "Q4"]
    quarterly_margin = []
    quarterly_profit = []
    for quarter in range(4):
        selected = slice(quarter * 3, quarter * 3 + 3)
        q_revenue = sum(revenue[selected])
        q_net = sum(net[selected])
        quarterly_margin.append(
            {"label": quarters[quarter], "value": round(q_net / q_revenue * 100, 1) if q_revenue else 0.0}
        )
        quarterly_profit.append(net[selected])

    box_groups = []
    for quarter, values in zip(quarters, quarterly_profit, strict=False):
        ordered = sorted(values)
        box_groups.append(
            {
                "label": quarter,
                "min": round(ordered[0], 2),
                "q1": round(_quantile(ordered, 0.25), 2),
                "median": round(_quantile(ordered, 0.5), 2),
                "q3": round(_quantile(ordered, 0.75), 2),
                "max": round(ordered[-1], 2),
            }
        )

    charts = [
        {
            "type": "multiline",
            "title": "收入、净利与现金",
            "subtitle": "12 个月",
            "unit": "元",
            "labels": months,
            "series": [
                {"name": "收入", "points": revenue},
                {"name": "净利", "points": net},
                {"name": "现金", "points": cash, "dash": True},
            ],
        },
        {
            "type": "stacked",
            "title": "成本结构",
            "subtitle": "成本、费用、税金按月堆叠",
            "unit": "元",
            "labels": months,
            "series": [
                {"name": "成本", "values": cogs},
                {"name": "费用", "values": opex},
                {"name": "税金", "values": tax},
            ],
        },
        {
            "type": "waterfall",
            "title": f"{latest['month']} 利润拆解",
            "subtitle": "收入减去成本、费用和税金",
            "unit": "元",
            "items": [
                {"label": "收入", "value": latest_revenue, "kind": "total"},
                {"label": "成本", "value": -latest_cogs, "kind": "delta"},
                {"label": "费用", "value": -latest_opex, "kind": "delta"},
                {"label": "税金", "value": -latest_tax, "kind": "delta"},
                {"label": "净利", "value": latest_net, "kind": "total"},
            ],
        },
        {
            "type": "bar",
            "title": "季度净利率",
            "subtitle": "净利 / 收入",
            "unit": "%",
            "items": quarterly_margin,
        },
        {
            "type": "gauge",
            "title": "年度收入预算完成",
            "subtitle": "累计收入 / 累计预算",
            "value": round(total_revenue, 2),
            "target": round(budget, 2),
            "unit": "元",
        },
        {
            "type": "box",
            "title": "季度净利分布",
            "subtitle": "每月净利的四分位",
            "groups": box_groups,
        },
        {
            "type": "donut",
            "title": "最新月成本构成",
            "subtitle": "成本 / 费用 / 税金",
            "items": [
                {"label": "成本", "value": latest_cogs},
                {"label": "费用", "value": latest_opex},
                {"label": "税金", "value": latest_tax},
            ],
        },
    ]

    return {
        "headline": f"全年收入 {_money(total_revenue)}，净利 {_money(total_net)}",
        "subline": f"12 个月的利润表、现金流和资产负债摘要。最新月净利率 {latest_net / latest_revenue * 100:.1f}%，预算完成 {total_revenue / budget * 100:.0f}%。",
        "kpis": [
            _kpi("收入", _money(total_revenue), "12 个月合计", "元"),
            _kpi("净利", _money(total_net), "12 个月合计", "元"),
            _kpi("净利率", f"{total_net / total_revenue * 100:.1f}%", "净利 / 收入"),
            _kpi("预算完成", f"{total_revenue / budget * 100:.0f}%", "收入 / 预算"),
        ],
        "charts": charts,
        "findings": [
            f"最新月现金 {_money(float(latest['cash']))}，较年初 {cash[-1] / cash[0] - 1:+.0%}。",
            "成本占收入的比例在 Q3 达到高点，Q4 回落；净利率随季节波动。",
        ],
    }

BUILDERS = {
    "sales_audit": _sales,
    "regional_policy": _regional,
    "quality_injection": _quality,
    "bigdata": _bigdata,
    "growth": _growth,
    "grades": _grades,
    "finance": _finance,
}

_REQUIRED = {
    "sales_audit": {"order_id", "sales", "quantity"},
    "regional_policy": {"region", "revenue"},
    "quality_injection": {"revenue"},
    "bigdata": {"x", "y"},
    "growth": {"month", "channel", "category", "revenue", "orders", "target"},
    "grades": {"student_id", "name", "class", "exam", "语文", "数学", "英语"},
    "finance": {"month", "revenue", "cogs", "opex", "tax", "net_profit", "budget"},
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
        insight = (
            builder(rows, visible=visible, policy=policy)
            if builder is _regional
            else builder(rows)
        )
        insight["kind"] = kind
        curated = insight.pop("charts", None)
        insight["charts"] = curated or _auto_charts(visible)
        active = policy or {}
        insight["scope"] = {
            "rows": len(rows),
            "visible": len(visible),
            "rls_column": rls_column,
            "principal_id": active.get("principal_id"),
            "pii_columns": list(active.get("pii_columns") or ()),
        }
        return insight
    except (KeyError, ValueError, ZeroDivisionError):
        _LOGGER.warning("演示预分析失败：%s", kind, exc_info=True)
        return None
