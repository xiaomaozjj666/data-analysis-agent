"""图表生成相关的命名常量。

从 ``charts.py`` 拆分而来，本身无逻辑，仅提供配置阈值与正则模式。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 命名常量（图表相关）
# ---------------------------------------------------------------------------

#: 分组图表 reindex 笛卡尔积上限。50×50 = 2500 单元格已足够，
#: 超过此值图表不可读且 reindex 主导内存。调用方仅看到实际观测组合。
_MAX_REINDEX_COMBINATIONS = 2_500

#: 图表标题清理后的最大字符数，避免前端溢出。
_CHART_TITLE_MAX_CHARS = 30

_BOOLEAN_VALUE_LABELS = {False: "否", True: "是"}
_SAMPLE_COUNT_COLUMN = "__sample_count__"
_HAS_RECORDS_COLUMN = "__has_records__"
#: 预计算的 hover 文本列，避免无记录 bar 在 hover 中显示 nan。
_HOVER_TEXT_COLUMN = "__hover_text__"

# 图表类型的中文短名，用于生成"柱状图_01"这种简洁文件名。
# 用户在前端看到的产物名仍以 LLM 给的 title 为准（经 _humanize_chart_title 清理），
# 文件名 stem 仅作为磁盘上的稳定标识，不再把整段标题塞进去。
_CHART_TYPE_LABELS_ZH: dict[str, str] = {
    "bar": "柱状图",
    "line": "折线图",
    "area": "面积图",
    "scatter": "散点图",
    "scatter_3d": "三维散点",
    "histogram": "直方图",
    "box": "箱线图",
    "violin": "小提琴图",
    "pie": "饼图",
    "heatmap": "热力图",
    "correlation_heatmap": "相关性热力图",
    "scatter_matrix": "散点矩阵",
    "sunburst": "旭日图",
    "treemap": "矩形树图",
}

# LLM 常在 title 后面追加的内部技术标记，例如：
#   "客户评分按产品分布_ANOVA_p_0_0012_η²_0_546"
#   "销量_vs_营收散点图_极端离群值主导"
#   "区域销售_n_2"
# 这些前缀在 UI 上展示给用户会造成"看不懂"。下面这些正则用来把第一段
# 人类可读部分取出来，并去掉所有 _n_N、_样本_N、ANOVA、p=、η² 等标记。
# 注意：之前用 _ANOVA.*$ 贪婪匹配到字符串末尾，会误删 LLM 可能给的合法
# 副标题（如 "客户评分分布_ANOVA_用户洞察" → "客户评分分布" 丢了"用户洞察"）。
# 现在的非贪婪模式只匹配技术标记本身（数字、p 值、η² 等已知后缀），不
# 会吞掉后面的可读内容。
_CHART_TITLE_TECHNICAL_PATTERNS = [
    re.compile(r"_n_\d+", re.IGNORECASE),
    re.compile(r"_样本_\d+", re.IGNORECASE),
    # _ANOVA 后面跟可选的 p 值/η²/effect_size/F 值等数字串，但不吞掉中文。
    re.compile(r"_ANOVA(?:_[a-zA-Z0-9_]+)?(?=_|$)", re.IGNORECASE),
    re.compile(r"_(?:p|p_value|pvalue)\s*[=:]?\s*[\d._-]+", re.IGNORECASE),
    re.compile(r"_η²\s*[\d._-]+", re.IGNORECASE),
    re.compile(r"_effect_size\s*[\d._-]+", re.IGNORECASE),
    re.compile(r"_F\s*[\d._-]+", re.IGNORECASE),
    # 离群值标记只删标记本身（"极端离群值主导" / "含离群值"），不吞后面内容。
    re.compile(r"_极端离群值(?:主导)?(?=_|$)", re.IGNORECASE),
    re.compile(r"_离群值(?:主导)?(?=_|$)", re.IGNORECASE),
    re.compile(r"_主导$", re.IGNORECASE),
    re.compile(r"_含异常值(?=_|$)", re.IGNORECASE),
    re.compile(r"_含离群值(?=_|$)", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# 图表意义性防护常量
# ---------------------------------------------------------------------------

#: 唯一值占比超过此阈值且非浮点列，视为标识符列（浮点测量值天然近唯一，豁免）。
_ID_LIKE_UNIQUE_RATIO = 0.95
#: 行数少于此值时不做唯一占比判定，避免小样本误报。
_ID_CHECK_MIN_ROWS = 30
#: 列名以这些后缀结尾时视为标识符命名（英文正则 + 中文后缀）。
_ID_NAME_PATTERN = re.compile(r"(?:^|[_\s-])(?:id|uuid|guid|key|code)s?$", re.IGNORECASE)
_ID_NAME_SUFFIXES_ZH = ("编号", "序号", "单号", "工号", "学号", "ID", "Id")
#: 饼图超过此类别数后扇区不可读，应传 top_n 或改用柱状图。
_PIE_MAX_CATEGORIES = 20
#: 分类 color 图例超过此数量后不可读。
_COLOR_MAX_CATEGORIES = 30
#: 类别轴（bar/box/violin/heatmap）无 top_n 时允许的最大类别数。
_CATEGORY_AXIS_MAX = 60

#: x 轴承担"类别轴"角色的图型，需要完整的 ID/基数校验。
#: histogram 不在此列——它的 x 是连续数值轴（非类别轴），整数类型的连续
#: 值（如年龄、分数、人数）天然近唯一，对它做 strict ID 检查会误拦合法分布图。
#: 常量列检查（_nunique <= 1）仍然独立生效，不会放过无信息列。
_CATEGORY_X_CHART_TYPES = {"bar", "pie", "box", "violin", "heatmap", "sunburst", "treemap"}

# ---------------------------------------------------------------------------
# 大数据统计图服务端聚合阈值
# ---------------------------------------------------------------------------

#: 统计图服务端聚合阈值（行）。低于此值仍走原始数据路径，视觉与行为不变。
STAT_AGG_THRESHOLD = 50_000

#: 小提琴图抽样上限：形状由 KDE 决定，两万个样本与全量肉眼无差别。
VIOLIN_SAMPLE_ROWS = 20_000
