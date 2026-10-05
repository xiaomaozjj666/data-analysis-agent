"""build_tools 闭包使用的数值阈值常量。

从 builder.py 提取的纯数值上限，被 build_tools 闭包内引用。
builder.py 通过 re-export 保持外部 import 路径不变。
"""

from __future__ import annotations

#: 散点矩阵支持的最大维度数，超过后图表不可读且渲染性能急剧下降。
_SCATTER_MATRIX_MAX_DIMENSIONS = 8

#: 卡方检验拒绝的最大基数，超过此值应建议用户合并类别。
_CHI_SQUARE_MAX_CARDINALITY = 100

#: groupby 结果返回的最大行数，防止高基数分组擑爆 context window。
_GROUPBY_MAX_ROWS = 500

#: transform_data 的 limit 参数上限。
_TRANSFORM_LIMIT_MAX = 1_000_000
