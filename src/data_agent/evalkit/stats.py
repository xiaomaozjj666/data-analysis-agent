"""配对显著性检验。

不引入 scipy：样本量小、只需要二项精确概率，``math.comb`` 足够，
也让评测工具能在只有标准库的环境里运行（CI、评审机器、离线容器）。
"""

from __future__ import annotations

import math


def mcnemar_exact(b: int, c: int) -> float:
    """双侧 McNemar 精确检验的 p 值。

    Args:
        b: 基线通过、候选失败的不一致对数。
        c: 基线失败、候选通过的不一致对数。

    Returns:
        双侧 p 值，范围 ``[0, 1]``。没有不一致对时返回 1.0（无证据）。

    只使用不一致对（b + c）；零假设是两者各占一半。小样本下精确二项
    检验比卡方近似可靠，这正是「只有十来条用例」的评测场景需要的。
    """
    if b < 0 or c < 0:
        raise ValueError("不一致对数不能为负。")
    total = b + c
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, k) for k in range(min(b, c) + 1))
    return min(1.0, 2 * tail / (2**total))


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 得分区间：小样本下比 Wald 区间稳定，不会越出 ``[0, 1]``。"""
    if total <= 0:
        return (0.0, 1.0)
    if not 0 <= successes <= total:
        raise ValueError("successes 必须位于 [0, total]。")
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))
