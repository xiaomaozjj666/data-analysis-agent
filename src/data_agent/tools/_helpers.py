"""工具集内部通用辅助函数：列名可读化、数值紧凑格式化、坐标轴 nice ticks。

本模块仅包含无业务依赖的纯函数，供 charts / builder / echarts_engine 复用。
"""

from __future__ import annotations

import math

# 业务列名 → 中文可读标签映射。双引擎（Plotly / ECharts）共享同一份映射，
# 避免"同一列在两个引擎下显示不同标签"的语义分裂。仅收录语义无歧义的
# 常见业务列名；不确定含义的列名保持原文（数据工具的轴标签应可溯源）。
_COLUMN_LABELS: dict[str, str] = {
    "units": "销量",
    "revenue": "收入",
    "sales": "销售额",
    "profit": "利润",
    "product": "产品",
    "region": "区域",
    "channel": "渠道",
    "category": "类别",
    "customer_rating": "客户评分",
    "unit_price": "单价",
    "discount_rate": "折扣率",
    "order_date": "订单日期",
    "date": "日期",
    "count": "记录数",
    "is_returned": "是否退货",
    "quantity": "数量",
    "qty": "数量",
    "customer_segment": "客户细分",
    "price": "价格",
    "cost": "成本",
    "amount": "金额",
    "total": "总计",
    "name": "名称",
    "order_id": "订单编号",
    "customer_id": "客户编号",
    "product_id": "产品编号",
    "status": "状态",
    "type": "类型",
    "city": "城市",
    "province": "省份",
    "country": "国家",
    "month": "月份",
    "year": "年份",
    "week": "周",
    "time": "时间",
    "datetime": "日期时间",
    "age": "年龄",
    "gender": "性别",
    "email": "邮箱",
    "phone": "电话",
}


def _human_column_label(column: str | None) -> str:
    """将列名转为可读标签：先查业务映射，再回退下划线转空格。"""
    if not column:
        return ""
    key = str(column)
    if key in _COLUMN_LABELS:
        return _COLUMN_LABELS[key]
    return key.replace("_", " ").strip()


def _compact_number(value: float) -> str:
    """将数值格式化为紧凑显示（如 1.2M、35.6K），用于图表标注。"""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    if absolute >= 10:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _nice_num(x: float, round_: bool = True) -> float:
    """将数值对齐到 1/2/5/10 的倍数（经典 nice number 算法）。

    round_=True 时向上取整到最近的 nice 刻度（用于计算步长），
    round_=False 时向下取整（用于计算范围下界）。
    """
    if x == 0:
        return 0.0
    exp = math.floor(math.log10(abs(x)))
    fraction = abs(x) / (10.0 ** exp)
    if round_:
        if fraction <= 1.5:
            nice_fraction = 1.0
        elif fraction <= 3.0:
            nice_fraction = 2.0
        elif fraction <= 7.0:
            nice_fraction = 5.0
        else:
            nice_fraction = 10.0
    else:
        if fraction < 1.5:
            nice_fraction = 1.0
        elif fraction < 3.0:
            nice_fraction = 2.0
        elif fraction < 7.0:
            nice_fraction = 5.0
        else:
            nice_fraction = 10.0
    sign = -1.0 if x < 0 else 1.0
    return sign * nice_fraction * (10.0 ** exp)


def _nice_ticks(vmin: float, vmax: float, n: int = 5) -> tuple[float, float, float]:
    """计算"圆数"刻度范围与步长，返回 (nice_min, nice_max, step)。

    刻度对齐到 1/2/5/10 的倍数，刻度数约 n 个。处理边界：
    - vmin==vmax：扩展 ±0.5 后计算
    - vmin>vmax：交换
    - 全负数：取绝对值计算再取反
    """
    if vmin > vmax:
        vmin, vmax = vmax, vmin
    if vmin == vmax:
        if vmin == 0:
            return -0.5, 0.5, 0.5
        delta = abs(vmin) * 0.5 if abs(vmin) >= 1 else 0.5
        vmin -= delta
        vmax += delta

    rng = _nice_num(vmax - vmin, round_=False)
    if rng == 0:
        rng = abs(vmax) * 0.5 if vmax != 0 else 1.0
    step = _nice_num(rng / max(n - 1, 1), round_=True)
    if step == 0:
        step = 1.0
    nice_min = math.floor(vmin / step) * step
    nice_max = math.ceil(vmax / step) * step
    # 修正浮点误差：将 nice_min/max 对齐到 step 的有效精度
    nice_min = round(nice_min, 10)
    nice_max = round(nice_max, 10)
    return nice_min, nice_max, step


def _nice_axis_formatter(value: float) -> str:
    """坐标轴刻度大数值自适应单位格式化（中文万/亿优先）。

    - 绝对值 >= 1亿：除以 1亿，保留 1-2 位小数，后缀"亿"
    - 绝对值 >= 1万：除以 1万，保留 1-2 位小数，后缀"万"
    - 绝对值 >= 1000：千分位逗号
    - 绝对值 < 1 且 > 0：按精度保留小数位
    - 0 直接返回"0"
    - 负数先取绝对值格式化再加负号
    """
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    abs_val = abs(value)

    if abs_val >= 100_000_000:
        formatted = abs_val / 100_000_000
        text = f"{formatted:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{text}亿"
    if abs_val >= 10_000:
        formatted = abs_val / 10_000
        text = f"{formatted:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{text}万"
    if abs_val >= 1000:
        return f"{value:,.0f}"
    if abs_val >= 100:
        return f"{value:,.0f}"
    if abs_val >= 10:
        return f"{value:,.1f}".rstrip("0").rstrip(".")
    if abs_val >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if abs_val >= 0.01:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if abs_val >= 0.001:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if abs_val > 0:
        return f"{value:.2e}"
    return "0"  # pragma: no cover - 不可达：value==0 已在函数开头返回


def _plotly_axis_tickformat(value_range: tuple[float, float]) -> str:
    """根据数值范围返回 Plotly tickformat 字符串。

    Plotly 的 tickformat 使用 d3 格式：
    - 大数值用 ",.0f"（千分位整数）配合 separatethousands
    - 小数值按精度用 ".Nf"
    - 极小值用 ".2e" 科学计数
    """
    abs_max = max(abs(value_range[0]), abs(value_range[1]))
    if abs_max == 0:
        return ",.0f"
    if abs_max >= 1000:
        return ",.0f"
    if abs_max >= 10:
        return ",.1f"
    if abs_max >= 1:
        return ".2f"
    if abs_max >= 0.01:
        return ".3f"
    if abs_max >= 0.001:
        return ".4f"
    return ".2e"
