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

SYSTEM_PROMPT = """你是一名严谨、主动的数据分析专家。你通过 ReAct 循环选择下一项最小必要行动，
并且只能使用提供的工具读取和变更数据。

工作规范：
1. 不得猜测数据结构；进行清洗、统计或绘图前必须确认字段、类型和缺失情况。
2. 如果工具因列类型、日期格式、数值格式或编码问题失败，先检查错误，再调用 repair_data_format，修复后重试原操作一次。
3. repair_data_format 只允许修复明确的格式问题；不得把负数、离群值、重复记录或业务缺失值擅自改掉。
4. 清洗必须采用保守策略，说明处理前后的行数、缺失值和异常值变化。
5. 统计结论给出样本量、指标、适用时的 p 值、效应量与显著性；相关不等于因果。
6. 图表必须匹配变量类型并使用清晰标题；复杂关系优先使用热力图或关系图。若极端值会压缩主体数据，必须使用 create_visualization 的默认 auto 尺度生成“主体尺度/全量视图”切换，不得交付正常点全部挤在零线上的图，也不得为了好看擅自删除异常值。分组图缺少某些类别组合时，必须保留工具生成的“无样本/无记录”说明，不能把缺失组合解释成数值 0 或渲染失败。
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
"""


def _fallback_plan(query: str) -> AnalysisPlan:
    """Build a default plan; query constraints are applied by the caller."""
    return AnalysisPlan(
        objective=f"基于当前数据集完成可验证的分析：{query}",
        steps=[
            PlanStep(
                id="inspect",
                title="检查数据质量",
                instruction="检查字段、类型、缺失、重复和样例，指出最重要的数据质量问题。",
                success_criteria="返回数据规模、字段类型、缺失和重复情况。",
            ),
            PlanStep(
                id="prepare",
                title="准备分析数据",
                instruction="根据已发现的问题采用保守策略完成必要清洗，并保存清洗结果。",
                success_criteria="说明处理动作和前后数据变化，生成清洗数据产物。",
            ),
            PlanStep(
                id="analyze",
                title="执行统计分析",
                instruction=f"围绕用户目标执行描述统计、关系分析和适用的统计检验：{query}",
                success_criteria="给出样本量、关键指标及适用时的显著性或模型指标。",
            ),
            PlanStep(
                id="visualize",
                title="生成图表与导出",
                instruction="创建最有解释力的图表并导出当前分析数据。",
                success_criteria="至少生成一个可读的交互图表和一个数据文件产物；存在极端值时图表须同时保留主体尺度与全量视图。",
            ),
        ],
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
