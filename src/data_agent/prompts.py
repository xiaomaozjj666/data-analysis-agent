"""提示词、命名常量、用户约束识别和工具错误处理辅助函数。

包含系统提示词、各节点使用的字符预算常量、用户意图识别正则模式表、
步骤过滤规则、回退计划生成、格式错误检测和工具错误装饰器。
从 agent.py 拆分以保持模块职责单一。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from data_agent.models import AnalysisPlan, PlanStep

# ---------------------------------------------------------------------------
# 命名常量：消除散落在各节点中的魔法数字
# ---------------------------------------------------------------------------

#: 工具调用结果写入 trace 时的最大字符数，防止单条 trace 膨胀到 MB 级。
_TRACE_DETAIL_MAX_CHARS = 4_000

#: 规划提示词中数据概况的最大字符数，避免超大 profile 撑爆 context window。
_PLAN_PROFILE_MAX_CHARS = 12_000

#: 重规划审查载荷的最大字符数。
_REPLAN_PAYLOAD_MAX_CHARS = 16_000

#: 最终报告汇总时所有步骤 summary 的总字符预算。
_FINALIZE_EVIDENCE_BUDGET = 30_000

#: 每步 summary 在 finalize 中至少保留的字符数。
_FINALIZE_PER_STEP_MIN_CHARS = 800

#: 格式修复重试时展示给 LLM 的错误文本最大字符数。
_FORMAT_ERROR_DISPLAY_MAX_CHARS = 3_000

# ---------------------------------------------------------------------------
# 国际化提示词：PROMPTS 按 language 索引，zh 为默认回退。
# 模板使用 {name} 占位符，由调用方通过 str.format() 填充。
# ---------------------------------------------------------------------------

#: 支持的语言集合；不在其中的 language 回退到 zh。
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en")

PROMPTS: dict[str, dict[str, Any]] = {
    "zh": {
        "system_prompt": """你是一名严谨、主动的数据分析专家。你通过 ReAct 循环选择下一项最小必要行动，
并且只能使用提供的工具读取和变更数据。

工作规范：
1. 不得猜测数据结构；进行清洗、统计或绘图前必须确认字段、类型和缺失情况。
2. 如果工具因列类型、日期格式、数值格式或编码问题失败，先检查错误，再调用 repair_data_format，修复后重试原操作一次。
3. repair_data_format 只允许修复明确的格式问题；不得把负数、离群值、重复记录或业务缺失值擅自改掉。
4. 清洗必须采用保守策略，说明处理前后的行数、缺失值和异常值变化。
5. 统计结论给出样本量、指标、适用时的 p 值、效应量与显著性；相关不等于因果。
6. 图表必须匹配变量类型并使用清晰标题；复杂关系优先使用热力图或关系图。create_visualization 的 chart_type 默认 "auto"，工具会根据列的类型与格式自动选择最合适的图型（时间序列→折线图、两数值→散点图、分类+数值→柱状图或饼图、分布→直方图、层级→旭日图、多数值列→相关性热力图、多维→散点矩阵）；大多数情况下直接用默认的 auto 即可，只有当你有明确意图（如强制饼图/箱线图）时才显式传 chart_type 覆盖。若极端值会压缩主体数据，必须使用 create_visualization 的默认 auto 尺度生成“主体尺度/全量视图”切换，不得交付正常点全部挤在零线上的图，也不得为了好看擅自删除异常值。分组图缺少某些类别组合时，必须保留工具生成的“无样本/无记录”说明，不能把缺失组合解释成数值 0 或渲染失败。
6a. 每张图表必须回答一个具体的分析问题，严禁无意义图表：
   - 禁止对 ID/编号/序号等标识符列画分布图、饼图或分组图（它们逐行唯一，分布必然均匀无信息）；
   - 禁止对只有单一取值的常量列画图；
   - 类别数超过 20 的列做饼图/柱状图时必须传 top_n（建议 10-20）聚焦头部，或改用直方图/热力图；
   - 选列前先看数据概况中每列的唯一值数（unique）：接近行数的是标识符，等于 1 的是常量，都不适合作图；
   - 工具会拒绝无意义的图表配置并返回修正建议，收到这类错误时按建议换列或传参重试，不要反复尝试同一无效配置。
7. 只能引用工具实际返回的数字和文件，不得编造结果。
8. 不展示隐藏的内部推理，只简要说明已执行的动作和可验证结果。
9. 当前只完成计划中指定的步骤，不要擅自重复已经完成的工作。
10. transform_data 只生成派生视图，不会改变主数据；不得把筛选视图当作最终清洗数据导出。

分析深度要求：
11. 统计分析时优先选择最能揭示数据特征的指标：分布形态（偏度/峰度）、离散程度、分位数而非仅仅均值。
12. 发现显著关系时，主动补充效应量和置信区间，帮助用户判断实际意义而非仅仅统计显著性。
13. 多维度数据优先使用分组对比、小倍数图或热力图揭示模式，避免将所有信息塞进一张图。
14. 每步执行完毕后，用一句话总结本步核心发现，必须包含具体数字和它对分析目标意味着什么，
    便于后续步骤和最终报告直接引用（例："华东区收入 120 万最高，是西北区的 2.3 倍，区域差异是收入的主要驱动"）。
""",
        "plan_template": """为数据分析任务制定 2 到 6 个可执行步骤。第一步必须检查数据，最后应包含必要的图表和导出。不要写空泛步骤，每步都要能由数据工具完成。
步骤设计原则：
- 检查步骤要具体指出需要关注的字段和质量问题
- 统计步骤要明确方法（如相关、回归、分组对比、分布检验）
- 图表步骤要指定图表类型和展示维度，每张图必须回答一个具体分析问题；禁止对 ID/编号列、常量列作图，高基数类别列需聚焦头部 top_n
- 避免重复步骤，每步应有独立价值

用户目标：{query}
数据概况：{profile_text}""",
        "execution_template": """总目标：{objective}
当前计划步骤：{step_title}
具体任务：{step_instruction}
完成标准：{step_success_criteria}
数据概况：{profile_brief}
已完成步骤：
{completed_text}

只执行当前步骤。使用工具获得证据，然后用简短文字报告实际结果。""",
        "retry_template": """{execution_prompt}

上一次工具调用失败：{format_error}
自动修复结果：{repair_result}
请只重试当前步骤，不要扩大任务范围。""",
        "replan_template": """审查数据分析计划的执行进度。根据已经获得的证据判断是否可以结束；否则只返回仍然必要的后续步骤，删除重复或没有价值的步骤。
{payload}""",
        "finalize_template": """请基于以下工具执行结果，写一份给业务负责人看的中文数据分析报告。读者不懂统计、时间有限，只想快速知道三件事：结论是什么、凭什么、接下来怎么办。

硬性要求：
1. 只能引用执行结果中实际出现的数字和文件，不得编造数据或未验证的推断；
2. 全文 1200-2000 字，信息密度优先，禁止“通过分析可知”“综上所述”这类空话套话；
3. 按以下章节组织，每节用二级标题（## ）：
   - ## 结论速览：3-5 条要点，第一条必须直接回答用户的分析目标；每条一个核心判断，关键数字 **加粗**，让读者 30 秒看懂全局；
   - ## 关键发现：按业务价值从高到低排序，每条发现按三步写——先一句话说清发现了什么，再列出支撑它的具体数字，最后补一句这对业务意味着什么；
   - ## 数据与处理：简要说明数据规模、质量问题和做过的清洗动作（前后行数/缺失变化）；
   - ## 图表与产物：逐个说明生成的图表应该看什么、得出什么印象，并提及可下载的数据文件；
   - ## 注意事项与建议：指出数据或方法的局限性，再给 2-3 条具体可执行的下一步建议。
4. 引用统计指标时必须当场用大白话解释，例如写“差异显著（p=0.001，即这种差距只有 0.1% 的可能是随机巧合）”，不允许只堆术语不解释；
5. 数字对比尽量换算成读者有感的形式（倍数、百分比、排名），而不是只列原始值；
6. 如需表格，使用 Markdown 表格语法，列数不超过 5 列，表头用业务语言而非字段名。

用户目标：{query}

执行结果：
{evidence}""",
        "fallback_plan": {
            "objective_template": "基于当前数据集完成可验证的分析：{query}",
            "steps": [
                {
                    "id": "inspect",
                    "title": "检查数据质量",
                    "instruction": "检查字段、类型、缺失、重复和样例，指出最重要的数据质量问题。",
                    "success_criteria": "返回数据规模、字段类型、缺失和重复情况。",
                },
                {
                    "id": "prepare",
                    "title": "准备分析数据",
                    "instruction": "根据已发现的问题采用保守策略完成必要清洗，并保存清洗结果。",
                    "success_criteria": "说明处理动作和前后数据变化，生成清洗数据产物。",
                },
                {
                    "id": "analyze",
                    "title": "执行统计分析",
                    "instruction": "围绕用户目标执行描述统计、关系分析和适用的统计检验：{query}",
                    "success_criteria": "给出样本量、关键指标及适用时的显著性或模型指标。",
                },
                {
                    "id": "visualize",
                    "title": "生成图表与导出",
                    "instruction": "创建最有解释力的图表并导出当前分析数据。",
                    "success_criteria": "至少生成一个可读的交互图表和一个数据文件产物；存在极端值时图表须同时保留主体尺度与全量视图。",
                },
            ],
        },
    },
    "en": {
        "system_prompt": """You are a rigorous, proactive data analysis expert. You select the next minimal necessary action through a ReAct loop, and you may only read and modify data using the provided tools.

Working standards:
1. Never guess the data structure; before any cleaning, statistics, or plotting, confirm the fields, types, and missing values.
2. If a tool fails due to column type, date format, numeric format, or encoding issues, inspect the error first, then call repair_data_format, and after the fix retry the original operation once.
3. repair_data_format may only fix clear format issues; never arbitrarily alter negative numbers, outliers, duplicate records, or business missing values.
4. Cleaning must follow a conservative strategy, reporting the row counts, missing values, and outlier changes before and after processing.
5. Statistical conclusions must include the sample size, metrics, and — when applicable — p-values, effect sizes, and significance; correlation does not imply causation.
6. Charts must match variable types and use clear titles; prefer heatmaps or relationship graphs for complex relationships. When extreme values would compress the main data, you must use the default auto scale of create_visualization to produce a "main-scale / full-range view" toggle; never deliver a chart where the normal points are all crammed onto the zero line, and never delete outliers just to make the chart look nice. When a grouped chart is missing certain category combinations, keep the tool-generated "no sample / no record" note; do not interpret a missing combination as the value 0 or a rendering failure.
6a. Every chart must answer a concrete analytical question; meaningless charts are strictly forbidden:
   - Never plot distributions, pies, or grouped charts on identifier columns (ID / serial number / code) — they are unique per row, so their distribution is uniformly uninformative;
   - Never plot constant columns that hold a single value;
   - When a categorical column has more than 20 categories, pass top_n (10-20 recommended) to focus on the head, or switch to a histogram/heatmap;
   - Before picking columns, check each column's unique count in the data overview: near-row-count means identifier, 1 means constant — neither is chartable;
   - The tool rejects meaningless chart configurations and returns correction advice; when you receive such an error, switch columns or parameters as advised instead of retrying the same invalid configuration.
7. Only cite numbers and files actually returned by the tools; never fabricate results.
8. Do not reveal hidden internal reasoning; briefly state only the actions performed and the verifiable results.
9. Complete only the steps specified in the current plan; do not repeat work that is already done.
10. transform_data only produces derived views and never alters the main data; do not export a filtered view as the final cleaned data.

Analysis depth requirements:
11. When running statistical analysis, prefer the metrics that best reveal the data's characteristics: distribution shape (skewness/kurtosis), dispersion, and quantiles rather than just the mean.
12. When a significant relationship is found, proactively add effect sizes and confidence intervals to help the user judge practical significance rather than statistical significance alone.
13. For multi-dimensional data, prefer grouped comparisons, small multiples, or heatmaps to reveal patterns; avoid cramming all information into a single chart.
14. After each step, summarize the core finding in one sentence that includes specific numbers and what they mean for the analysis objective, so subsequent steps and the final report can cite it directly (e.g., "East China has the highest revenue at 1.2M, 2.3x that of the Northwest; regional difference is the primary driver of revenue").
""",
        "plan_template": """Create 2 to 6 executable steps for the data analysis task. The first step must inspect the data, and the last should include the necessary charts and exports. Do not write vague steps; each step must be completable by the data tools.
Step design principles:
- Inspection steps should specifically call out the fields and quality issues to focus on
- Statistical steps should specify the method (e.g., correlation, regression, grouped comparison, distribution test)
- Charting steps should specify the chart type and dimensions to display; every chart must answer a concrete analytical question — never chart identifier or constant columns, and focus high-cardinality categories with top_n
- Avoid duplicate steps; each step should have independent value

User objective: {query}
Data overview: {profile_text}""",
        "execution_template": """Overall objective: {objective}
Current plan step: {step_title}
Specific task: {step_instruction}
Completion criteria: {step_success_criteria}
Data overview: {profile_brief}
Completed steps:
{completed_text}

Execute only the current step. Use the tools to gather evidence, then report the actual results in a brief text.""",
        "retry_template": """{execution_prompt}

The previous tool call failed: {format_error}
Automatic repair result: {repair_result}
Please retry only the current step; do not expand the task scope.""",
        "replan_template": """Review the execution progress of the data analysis plan. Based on the evidence gathered so far, decide whether the analysis can be concluded; otherwise, return only the subsequent steps that remain necessary, and remove any duplicates or steps that no longer add value.
{payload}""",
        "finalize_template": """Based on the tool execution results below, write a data analysis report in English for a business leader. The reader does not understand statistics, is short on time, and only wants to quickly know three things: what the conclusion is, what it is based on, and what to do next.

Hard requirements:
1. Only cite numbers and files that actually appear in the execution results; never fabricate data or unverified inferences;
2. Keep the full text to 1200-2000 words; prioritize information density and avoid empty filler phrases such as "through analysis we can see" or "in summary";
3. Organize the report into the following sections, each with a level-2 heading (## ):
   - ## Executive Summary: 3-5 key points, the first of which must directly answer the user's analysis objective; each point is one core judgment with the key numbers **bolded**, so the reader can grasp the big picture in 30 seconds;
   - ## Key Findings: ordered by business value from high to low; each finding follows three steps — first state in one sentence what was found, then list the specific numbers supporting it, and finally add one sentence on what it means for the business;
   - ## Data & Processing: briefly describe the data scale, quality issues, and cleaning actions taken (row counts / missing-value changes before and after);
   - ## Charts & Artifacts: explain one by one what to look at in each generated chart and what impression it gives, and mention the downloadable data files;
   - ## Caveats & Recommendations: point out the limitations of the data or methods, then give 2-3 specific, actionable next-step recommendations.
4. When citing statistical metrics, explain them in plain language on the spot — for example, write "the difference is significant (p=0.001, meaning there is only a 0.1% chance this gap is a random coincidence)"; never pile on jargon without explanation;
5. Convert numeric comparisons into reader-friendly forms (multiples, percentages, rankings) rather than listing only raw values;
6. If tables are needed, use Markdown table syntax with no more than 5 columns, and use business language rather than field names for the headers.

User objective: {query}

Execution results:
{evidence}""",
        "fallback_plan": {
            "objective_template": "Complete a verifiable analysis based on the current dataset: {query}",
            "steps": [
                {
                    "id": "inspect",
                    "title": "Inspect data quality",
                    "instruction": "Check the fields, types, missing values, duplicates, and samples, and call out the most important data quality issues.",
                    "success_criteria": "Return the data scale, field types, and missing/duplicate counts.",
                },
                {
                    "id": "prepare",
                    "title": "Prepare analysis data",
                    "instruction": "Apply a conservative strategy to complete the necessary cleaning based on the issues found, and save the cleaned result.",
                    "success_criteria": "Describe the actions taken and the before/after data changes, and produce a cleaned data artifact.",
                },
                {
                    "id": "analyze",
                    "title": "Run statistical analysis",
                    "instruction": "Run descriptive statistics, relationship analysis, and applicable statistical tests around the user's objective: {query}",
                    "success_criteria": "Provide the sample size, key metrics, and significance or model metrics when applicable.",
                },
                {
                    "id": "visualize",
                    "title": "Generate charts and exports",
                    "instruction": "Create the most explanatory charts and export the current analysis data.",
                    "success_criteria": "Produce at least one readable interactive chart and one data file artifact; when extreme values are present, the chart must keep both a main-scale and a full-range view.",
                },
            ],
        },
    },
}


def get_prompts(language: str = "zh") -> dict[str, Any]:
    """返回指定语言的提示词集合。

    Args:
        language: 语言代码（zh / en）。传入 None、空串或不支持的值时回退到 zh。

    Returns:
        包含 system_prompt / plan_template / execution_template /
        retry_template / replan_template / finalize_template / fallback_plan
        的字典。
    """
    key = (language or "zh").strip().lower()
    return PROMPTS.get(key, PROMPTS["zh"])


#: 向后兼容：旧的模块级常量名仍指向中文版本，已有导入无需改动。
SYSTEM_PROMPT = PROMPTS["zh"]["system_prompt"]


def _fallback_plan(query: str, prompts: dict[str, Any] | None = None) -> AnalysisPlan:
    """Build a default plan; query constraints are applied by the caller.

    Args:
        query: 用户的分析任务描述，用于填充 objective 和 analyze 步骤模板。
        prompts: 指定语言的提示词集合（由 ``get_prompts`` 返回）。为 None 时
            回退到中文版本，保持 ``_fallback_plan(query)`` 的向后兼容。
    """
    fp = (prompts or get_prompts("zh"))["fallback_plan"]
    steps = [
        PlanStep(
            id=step["id"],
            title=step["title"],
            instruction=step["instruction"].format(query=query),
            success_criteria=step["success_criteria"],
        )
        for step in fp["steps"]
    ]
    return AnalysisPlan(
        objective=fp["objective_template"].format(query=query),
        steps=steps,
    )


# ---------------------------------------------------------------------------
# 用户约束识别：用预编译正则模式集合替代裸子串匹配，同时覆盖中英文等价表达。
# ---------------------------------------------------------------------------

#: 只读意图模式（中英）。命中时过滤掉会修改主数据的步骤。
_READ_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"不修改|无需修改|不要修改|不要改动|保持原样|只读|只看"),
    re.compile(r"只检查|仅检查"),
    re.compile(r"no\s*modify|read\s*only|don'?t\s*change|no\s*changes", re.IGNORECASE),
)

#: 禁用图表意图模式（中英）。命中时过滤掉可视化相关步骤。
_NO_CHART_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"不生成图表|不要图表|不画图|无需绘图|不用画图|不用绘图|"
        r"不需要图表|不需要可视化|不用可视化|无需图表|不要可视化"
    ),
    re.compile(r"no\s*charts?|no\s*plots?|no\s*visuals?|don'?t\s*chart|without\s*charts?", re.IGNORECASE),
)

#: "仅检查数据质量"意图：需同时命中"检查"语义和"质量"语义。
_INSPECT_ONLY_CHECK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"只检查|仅检查"),
)
_INSPECT_ONLY_QUALITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"质量|quality", re.IGNORECASE),
)

#: 禁止格式修复意图模式（中英）。命中时跳过自动 repair_data_format 重试。
_FORMAT_REPAIR_BLOCKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"不修改|无需修改|只检查|仅检查"),
    re.compile(r"no\s*modify|read\s*only|check\s*only|don'?t\s*change|no\s*changes", re.IGNORECASE),
)


@dataclass(slots=True, frozen=True)
class _StepFilterRule:
    """Config-driven step filter rule for ``_apply_query_constraints``.

    When any of ``query_patterns`` matches the user query, steps are dropped if
    their id is in ``drop_step_ids`` OR any ``drop_step_keyword_patterns``
    matches the step's ``title + instruction``。新增约束类型只需在
    ``_STEP_FILTER_RULES`` 中追加条目，无需修改函数主体。
    """

    query_patterns: tuple[re.Pattern[str], ...]
    drop_step_ids: frozenset[str]
    drop_step_keyword_patterns: tuple[re.Pattern[str], ...]


#: 步骤过滤规则表：read_only / no_charts 等约束均在此声明。
_STEP_FILTER_RULES: tuple[_StepFilterRule, ...] = (
    _StepFilterRule(
        query_patterns=_READ_ONLY_PATTERNS,
        drop_step_ids=frozenset({"prepare", "clean", "transform", "export"}),
        drop_step_keyword_patterns=(re.compile(r"清洗|转换|导出"),),
    ),
    _StepFilterRule(
        query_patterns=_NO_CHART_PATTERNS,
        drop_step_ids=frozenset({"visualize", "chart", "plot"}),
        drop_step_keyword_patterns=(re.compile(r"图表|绘图|可视化"),),
    ),
)


def _any_pattern_match(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    """Return True if any precompiled pattern in ``patterns`` matches ``text``."""
    return any(pattern.search(text) for pattern in patterns)


def _apply_query_constraints(query: str, plan: AnalysisPlan) -> AnalysisPlan:
    """Keep explicit user constraints intact even when structured planning falls back.

    使用预编译正则模式集合（含中英文等价词）识别用户意图，步骤过滤逻辑由
    ``_STEP_FILTER_RULES`` 配置表驱动，避免裸子串匹配和散落的硬编码分支。
    """
    steps = list(plan.steps)
    # "仅检查数据质量" 是更强的约束：只保留 inspect 步骤。
    if (
        _any_pattern_match(_INSPECT_ONLY_CHECK_PATTERNS, query)
        and _any_pattern_match(_INSPECT_ONLY_QUALITY_PATTERNS, query)
    ):
        steps = [step for step in steps if step.id == "inspect"]
    # 通用约束规则：read_only / no_charts 等。
    for rule in _STEP_FILTER_RULES:
        if not _any_pattern_match(rule.query_patterns, query):
            continue
        steps = [
            step
            for step in steps
            if step.id not in rule.drop_step_ids
            and not _any_pattern_match(rule.drop_step_keyword_patterns, f"{step.title}{step.instruction}")
        ]
    if not steps:
        steps = [
            PlanStep(
                id="inspect",
                title="检查数据质量",
                instruction="只读取数据并检查字段、类型、缺失、重复和样例，不修改任何数据。",
                success_criteria="返回数据规模、字段类型、缺失和重复情况。",
            )
        ]
    return AnalysisPlan(objective=plan.objective, steps=steps)


def _is_recoverable_format_error(text: str) -> bool:
    """判断工具错误是否属于可通过 repair_data_format 自动修复的格式问题。

    仅匹配明确的类型/编码/日期格式错误标记；业务数据异常（如离群值、
    负数）不在此列，避免 Agent 擅自修改有效数据。
    """
    markers = (
        "dtype",
        "datetime",
        "could not convert",
        "not numeric",
        "不是数值列",
        "无法转换",
        "unable to parse",
        "time data",
        "日期格式",
        "数值格式",
        "编码",
    )
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in markers)


_ERROR_HUMANIZE_MAP: list[tuple[str, str]] = [
    ("could not convert string to float", "部分值无法转为数值，存在非数字内容"),
    ("could not convert", "数据类型转换失败，部分值格式不兼容"),
    ("KeyError", "引用了不存在的列名"),
    ("FileNotFoundError", "文件未找到，可能已被移动或删除"),
    ("ValueError", "数据格式不符合预期，请检查列类型和取值"),
    ("TypeError", "数据类型不匹配，部分列的格式可能需要清洗"),
    ("ParserError", "文件解析失败，格式可能不正确或已损坏"),
    ("UnicodeDecodeError", "文件编码无法识别，请尝试另存为 UTF-8"),
    ("IndexError", "引用了不存在的行或列位置"),
    ("ZeroDivisionError", "计算中出现除以零，数据可能存在全零列"),
    ("MemoryError", "数据量超出内存限制，请缩小分析范围"),
    ("PermissionError", "文件权限不足，无法读取或写入"),
    ("OverflowError", "数值超出计算范围"),
    ("datetime", "日期格式无法解析，请检查日期列格式"),
    ("time data", "时间数据格式不匹配"),
]


def _humanize_error(exc: Exception) -> str:
    """把 Python 异常转为面向用户的友好中文消息。

    技术细节保留在 trace 中供调试；summary 只展示用户可理解的描述，
    避免终端用户看到 ``ValueError: could not convert string to float: 'N/A'``
    这类晦涩信息。
    """
    raw = f"{type(exc).__name__}: {exc}"
    lowered = raw.lower()
    for pattern, human_text in _ERROR_HUMANIZE_MAP:
        if pattern.lower() in lowered:
            return human_text
    return f"执行过程中遇到问题（{type(exc).__name__}）"


def _query_allows_format_repair(query: str) -> bool:
    """Return False when the query explicitly forbids data modifications.

    通过 ``_FORMAT_REPAIR_BLOCKERS`` 预编译正则模式集合（含中英文等价词）
    识别禁止修改意图，替代原有的裸子串匹配。
    """
    return not _any_pattern_match(_FORMAT_REPAIR_BLOCKERS, query)


@wrap_tool_call
def _handle_tool_error(request: Any, handler: Any) -> ToolMessage:
    try:
        return handler(request)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        error_code = "format_error" if _is_recoverable_format_error(detail) else "tool_error"
        return ToolMessage(
            content=(
                f"工具执行失败：{detail}。"
                "如果原因与列类型、日期或数值格式有关，请先调用 repair_data_format，"
                "再用修正后的列名和参数重试；如果是业务数据异常，不要擅自修改。"
            ),
            tool_call_id=request.tool_call["id"],
            additional_kwargs={"error_code": error_code},
        )
