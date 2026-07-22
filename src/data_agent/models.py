"""数据模型：Pydantic 模型、TypedDict 和 dataclass 定义。

包含 LangGraph 工作流的状态字典、LLM 结构化输出的计划/决策模型、
以及最终分析结果的数据类。从 agent.py 拆分以保持模块职责单一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    """分析计划中的单个可执行步骤。

    Attributes:
        id: 稳定的短标识符（如 ``inspect``、``visualize``），用于去重和引用。
        title: 面向用户展示的中文短标题。
        instruction: 传递给 ReAct 执行器的具体任务指令。
        success_criteria: 可观测的完成标准，供重规划器判断是否达标。
    """

    id: str = Field(description="Stable short step id, for example inspect or visualize")
    title: str = Field(description="Short Chinese title shown to the user")
    instruction: str = Field(description="Concrete instruction for the ReAct executor")
    success_criteria: str = Field(description="Observable completion criteria")


class AnalysisPlan(BaseModel):
    """LLM 结构化输出的完整分析计划。

    Attributes:
        objective: 一句话分析目标，贯穿所有步骤。
        steps: 2-8 个有序步骤，第一步必须是数据检查。
    """

    objective: str = Field(description="One-sentence analysis objective")
    steps: list[PlanStep] = Field(min_length=1, max_length=8)


class ReplanDecision(BaseModel):
    """重规划器的结构化决策输出。

    Attributes:
        done: 是否已有足够证据结束分析。
        rationale: 简短理由（不暴露内部推理链）。
        remaining_steps: 若未结束，返回仍需执行的后续步骤。
    """

    done: bool = Field(description="Whether enough evidence exists to finish")
    rationale: str = Field(description="Brief reason without hidden chain of thought")
    remaining_steps: list[PlanStep] = Field(default_factory=list, max_length=8)


class WorkflowState(TypedDict, total=False):
    """LangGraph 工作流的全局状态字典。

    所有节点通过返回 partial dict 来更新状态（reducer 语义为覆盖）。
    """

    query: str
    input_messages: list[BaseMessage]
    dataset_profile: dict[str, Any]
    objective: str
    plan: list[dict[str, str]]
    remaining_steps: list[dict[str, str]]
    current_step: dict[str, str]
    last_step_result: dict[str, Any]
    completed_steps: list[dict[str, Any]]
    response: str
    trace: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    replan_reason: str


@dataclass(slots=True)
class AnalysisResult:
    """一次完整分析的最终输出，由 finalize 节点组装。

    Attributes:
        response: Markdown 格式的中文分析报告。
        trace: 工具调用与结果的审计轨迹。
        artifacts: 生成的图表、数据文件等产物元数据。
        dataset_profile: 数据集概况快照。
        plan: 原始计划步骤列表。
        completed_steps: 各步骤执行结果（含 status 和 summary）。
        usage: 本次分析累计的 token 用量（prompt/completion/total_tokens），
            由 UsageAccumulator 在 LLM 调用结束时累计。None 表示未采集。
        reasoning: DeepSeek reasoning_content 的完整思考过程文本，
            由 ReasoningStreamCallback 累计。空字符串表示无思考过程。
    """

    response: str
    trace: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    dataset_profile: dict[str, Any]
    plan: list[dict[str, str]]
    completed_steps: list[dict[str, Any]]
    usage: dict[str, int] | None = None
    reasoning: str = ""


class AnalysisCancelled(RuntimeError):
    """Raised between workflow nodes when the user cancels an analysis."""
