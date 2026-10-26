"""Agent 评测与统计护栏。

把「改动后感觉更好」变成可复现的判定：

- ``stats``：McNemar 精确检验与 Wilson 置信区间（仅标准库）。
- ``harness``：把基线 / 候选两次运行做配对比较，按统计显著性与成本护栏裁决。
- ``ledger``：把每次裁决连同指标、检验统计量落盘，便于回滚与复核。
- ``cli``：``data-agent-eval compare`` 命令行入口，可直接接进 CI。

离线设计：评测结果来自 JSONL（每行一个用例），不依赖模型调用，
因此 CI、评审机器与本地都能复现同一份结论。
"""

from data_agent.evalkit.harness import EvaluationReport, GatePolicy, evaluate
from data_agent.evalkit.models import CaseResult, SuiteResult, load_results
from data_agent.evalkit.stats import mcnemar_exact, wilson_interval

__all__ = [
    "CaseResult",
    "EvaluationReport",
    "GatePolicy",
    "SuiteResult",
    "evaluate",
    "load_results",
    "mcnemar_exact",
    "wilson_interval",
]
