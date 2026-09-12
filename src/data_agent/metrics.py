"""指标语义层：把业务口径落成可版本化的定义，并在规划阶段注入提示词。

要解决的问题是跨会话口径漂移：如果「净销售额」每次都靠模型现场推断，今天可能
含退货、明天可能不含，同一份数据在不同会话里得出不同结论。这里不改动任何计算
路径，只做两件事——加载用户登记的口径定义，挑出与本次问题相关的条目，渲染成
规划提示词里的一段约束文本。

设计取舍：
- 没有定义文件时整个特性静默失效，既有行为一字不变（默认关闭，显式启用）。
- 定义文件结构错误时直接报错而不是跳过：静默忽略用户登记的指标口径，会让分析
  在"看起来正常"的情况下用错定义，比直接失败危险得多。
- 只做匹配与注入，不做自动计算。把口径变成可执行表达式需要 SQL 或沙箱配合，
  属于后续工作。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 工作区内约定使用的指标定义文件名。
METRICS_FILENAME = "metrics.json"

#: 可用环境变量指定一份全局指标定义，供同一部署下的多个会话复用。
METRICS_PATH_ENV = "DATA_AGENT_METRICS_PATH"

#: 单次注入的最大指标条数，防止定义文件很大时挤占规划提示词的预算。
_MAX_SELECTED_METRICS = 6

#: 单个字段注入时的最大字符数。
_FIELD_MAX_CHARS = 300


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """一条业务指标口径。

    Attributes:
        name: 指标标准名称，例如「净销售额」。
        description: 业务口径说明，回答"这个指标到底算什么"。
        expression: 可选的算式描述，例如 ``销售额 - 退货金额``。
        aliases: 可选别名，用于把用户口语说法映射到标准名称。
        source_columns: 可选依赖字段列表。
    """

    name: str
    description: str
    expression: str = ""
    aliases: tuple[str, ...] = ()
    source_columns: tuple[str, ...] = ()


def _as_str_tuple(value: Any, *, field: str, index: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        # 序号按 1 起算：用户要拿着这条消息去 JSON 文件里定位那一项。
        raise ValueError(f"metrics 第 {index + 1} 项的 {field} 必须是字符串数组。")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _parse_entry(raw: Any, index: int) -> MetricDefinition:
    """把一条原始记录解析为 MetricDefinition，结构不对时报错而非跳过。"""
    if not isinstance(raw, dict):
        raise ValueError(f"metrics 第 {index + 1} 项必须是对象。")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"metrics 第 {index + 1} 项缺少 name。")
    description = str(raw.get("description") or "").strip()
    if not description:
        raise ValueError(
            f"metrics 第 {index + 1} 项（{name}）缺少 description，无法判断口径含义。"
        )
    return MetricDefinition(
        name=name,
        description=description,
        expression=str(raw.get("expression") or "").strip(),
        aliases=_as_str_tuple(raw.get("aliases"), field="aliases", index=index),
        source_columns=_as_str_tuple(raw.get("source_columns"), field="source_columns", index=index),
    )


def parse_metric_definitions(payload: Any) -> list[MetricDefinition]:
    """解析已读入内存的定义载荷，支持 ``{"metrics": [...]}`` 与裸数组两种形式。"""
    if isinstance(payload, dict):
        if "metrics" not in payload:
            raise ValueError("指标定义文件缺少顶层 metrics 字段。")
        entries = payload["metrics"]
    else:
        entries = payload
    if not isinstance(entries, list):
        raise ValueError("metrics 必须是数组。")
    return [_parse_entry(item, index) for index, item in enumerate(entries)]


def load_metric_definitions(path: str | Path) -> list[MetricDefinition]:
    """从文件加载指标定义。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: JSON 无法解析或结构不符合约定。
    """
    source = Path(path).expanduser()
    text = source.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"指标定义文件不是合法 JSON：{exc}") from exc
    return parse_metric_definitions(payload)


def load_metrics_for_workspace(root: str | Path) -> list[MetricDefinition]:
    """加载适用于当前会话的指标定义。

    优先使用 ``DATA_AGENT_METRICS_PATH`` 指向的全局定义文件，其次查找工作区
    根目录下的 ``metrics.json``。两者都不存在时返回空列表，规划流程按原有
    行为继续，不做任何注入。

    Raises:
        ValueError: 找到了定义文件但内容不合法。此处刻意不降级为"忽略并继续"：
            分析会用错误的口径得出看似正常的结论，比直接失败代价大得多。
    """
    configured = os.environ.get(METRICS_PATH_ENV, "").strip()
    candidate: Path | None = None
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            candidate = configured_path
    if candidate is None:
        workspace_file = Path(root).expanduser() / METRICS_FILENAME
        if workspace_file.is_file():
            candidate = workspace_file
    if candidate is None:
        return []
    try:
        return load_metric_definitions(candidate)
    except ValueError as exc:
        raise ValueError(f"指标定义文件 {candidate} 解析失败：{exc}") from exc


def _score(query_lower: str, definition: MetricDefinition) -> int:
    """按名称与别名在问题中出现的次数打分。"""
    score = 0
    name = definition.name.strip().lower()
    if name and name in query_lower:
        score += 3
    for alias in definition.aliases:
        alias_lower = alias.strip().lower()
        if alias_lower and alias_lower in query_lower:
            score += 2
    return score


def select_metrics(
    query: str,
    definitions: Sequence[MetricDefinition],
    limit: int = _MAX_SELECTED_METRICS,
) -> list[MetricDefinition]:
    """挑出要注入的指标定义。

    与问题直接相关的条目优先，其余按定义文件中的原始顺序补足——登记的指标口径
    本身就是"本次分析必须遵守的上下文"，而不是一組等待命中的搜索结果；若只注入
    命中项，像"分析这份销售数据"这样没有点名指标的问题就会完全看不到定义。
    """
    if not definitions or limit <= 0:
        return []
    query_lower = query.lower()
    indexed = list(enumerate(definitions))
    indexed.sort(key=lambda item: (-_score(query_lower, item[1]), item[0]))
    return [definition for _, definition in indexed[:limit]]


def _truncate(text: str, limit: int = _FIELD_MAX_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def render_metric_context(
    query: str,
    definitions: Sequence[MetricDefinition],
    limit: int = _MAX_SELECTED_METRICS,
) -> str:
    """渲染供规划提示词注入的口径约束文本；没有定义时返回空串。"""
    selected = select_metrics(query, definitions, limit)
    if not selected:
        return ""
    lines = ["本会话已登记的指标口径（必须优先采用以下定义，不得自行重新定义同名指标，也不得把别名当作独立指标重复计算）："]
    for item in selected:
        segment = f"- {item.name}"
        if item.aliases:
            segment += f"（别名：{'、'.join(item.aliases)}）"
        segment += f"：{_truncate(item.description)}"
        if item.expression:
            segment += f"；计算口径：{_truncate(item.expression)}"
        if item.source_columns:
            segment += f"；依赖字段：{'、'.join(item.source_columns)}"
        lines.append(segment)
    hidden = len(definitions) - len(selected)
    if hidden > 0:
        lines.append(f"（另有 {hidden} 条已登记指标未在此列出；如分析涉及，请先在结论中说明口径来源。）")
    return "\n".join(lines)
