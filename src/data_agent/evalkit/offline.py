"""确定性离线 Agent 评测：真实 LangGraph/工具链 + 脚本化 ChatModel。

该模式不读取 API Key、不访问模型服务，也不用手工填写通过结果。每条用例
从 JSON 中的真实表格数据建立 DataWorkspace，完整执行 validate→plan→
execute→replan→finalize，再把实际工具轨迹、计划与报告摘要与断言比较。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.evalkit.models import CaseResult, SuiteResult
from data_agent.workspace import DataWorkspace

_CASE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")


class OfflineScriptedChatModel(BaseChatModel):
    """按固定脚本驱动一次 inspect_data 工具调用的离线模型。"""

    _bound_tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "offline-scripted-eval"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        clone = self.model_copy(deep=True)
        clone._bound_tool_names = {
            getattr(item, "name", None) or getattr(item, "__name__", "") for item in tools
        }
        return clone

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if "AnalysisPlan" in self._bound_tool_names:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnalysisPlan",
                        "id": "offline_call_plan",
                        "type": "tool_call",
                        "args": {
                            "objective": "离线验证分析流程",
                            "steps": [
                                {
                                    "id": "inspect",
                                    "title": "检查数据",
                                    "instruction": "检查数据质量",
                                    "success_criteria": "返回数据概况",
                                }
                            ],
                        },
                    }
                ],
            )
        elif "ReplanDecision" in self._bound_tool_names:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ReplanDecision",
                        "id": "offline_call_replan",
                        "type": "tool_call",
                        "args": {"done": True, "rationale": "检查完成", "remaining_steps": []},
                    }
                ],
            )
        elif any(isinstance(item, ToolMessage) for item in messages):
            message = AIMessage(content="已检查数据；工作流和 ReAct 工具调用均正常。")
        elif "inspect_data" not in self._bound_tool_names:
            message = AIMessage(content="Plan-and-Execute 与 ReAct 分析流程已完成。")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "inspect_data",
                        "args": {"sample_rows": 3},
                        "id": "offline_call_inspect",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def load_offline_cases(path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    """读取并严格校验离线套件，拒绝结构不明的手工结果文件。"""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("离线套件顶层必须包含 schema_version=1。")
    suite_id = payload.get("suite_id")
    cases = payload.get("cases")
    if not isinstance(suite_id, str) or not suite_id.strip():
        raise ValueError("离线套件缺少 suite_id。")
    if not isinstance(cases, list) or not cases:
        raise ValueError("离线套件 cases 必须是非空数组。")

    seen: set[str] = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{where} 必须是对象。")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not _CASE_ID_RE.fullmatch(case_id):
            raise ValueError(f"{where}.case_id 只能包含字母、数字、下划线与连字符。")
        if case_id in seen:
            raise ValueError(f"离线套件存在重复 case_id：{case_id}")
        seen.add(case_id)
        if not isinstance(case.get("task"), str) or not case["task"].strip():
            raise ValueError(f"{where}.task 必须是非空字符串。")
        dataset = case.get("dataset")
        if not isinstance(dataset, dict):
            raise ValueError(f"{where}.dataset 必须是对象。")
        columns = dataset.get("columns")
        rows = dataset.get("rows")
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(item, str) or not item for item in columns)
            or len(set(columns)) != len(columns)
        ):
            raise ValueError(f"{where}.dataset.columns 必须是非空且不重复的字符串数组。")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{where}.dataset.rows 必须是非空数组。")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError(f"{where}.dataset.rows 的每行长度必须等于 columns。")
        expect = case.get("expect")
        if not isinstance(expect, dict):
            raise ValueError(f"{where}.expect 必须是对象。")
        if expect.get("rows") != len(rows) or expect.get("columns") != len(columns):
            raise ValueError(f"{where}.expect 的行列数必须与 dataset 一致。")
        if expect.get("column_names") != columns:
            raise ValueError(f"{where}.expect.column_names 必须与 dataset.columns 一致。")
        for field in ("tool_calls", "plan_ids", "completed_step_ids", "response_contains"):
            if not isinstance(expect.get(field), list) or any(
                not isinstance(item, str) for item in expect[field]
            ):
                raise ValueError(f"{where}.expect.{field} 必须是字符串数组。")
    return suite_id, cases


def _run_case(case: dict[str, Any], work_root: Path) -> dict[str, Any]:
    case_id = case["case_id"]
    dataset = case["dataset"]
    source = work_root / f"{case_id}.csv"
    pd.DataFrame(dataset["rows"], columns=dataset["columns"]).to_csv(source, index=False)
    workspace = DataWorkspace(work_root / "runs", session_id=f"eval_{case_id}")
    workspace.load(source)
    settings = AgentSettings(
        api_key="offline-no-network",
        max_iterations=5,
        max_analysis_seconds=60,
        runs_dir=work_root / "runs",
    )
    result = DataAnalysisAgent(
        workspace,
        settings=settings,
        model=OfflineScriptedChatModel(),
    ).run(case["task"])

    actual = {
        "tool_calls": [item["name"] for item in result.trace if item.get("type") == "tool_call"],
        "plan_ids": [item["id"] for item in result.plan],
        "completed_step_ids": [item["id"] for item in result.completed_steps],
        "dataset_rows": result.dataset_profile["rows"],
        "dataset_columns": result.dataset_profile["columns"],
        "column_names": [item["name"] for item in result.dataset_profile["column_info"]],
        "response_sha256": hashlib.sha256(result.response.encode("utf-8")).hexdigest(),
    }
    expected = case["expect"]
    failures: list[str] = []
    comparisons = (
        ("tool_calls", actual["tool_calls"], expected["tool_calls"]),
        ("plan_ids", actual["plan_ids"], expected["plan_ids"]),
        ("completed_step_ids", actual["completed_step_ids"], expected["completed_step_ids"]),
        ("dataset_rows", actual["dataset_rows"], expected["rows"]),
        ("dataset_columns", actual["dataset_columns"], expected["columns"]),
        ("column_names", actual["column_names"], expected["column_names"]),
    )
    for label, observed, wanted in comparisons:
        if observed != wanted:
            failures.append(f"{label}: expected={wanted!r}, actual={observed!r}")
    for fragment in expected["response_contains"]:
        if fragment not in result.response:
            failures.append(f"response 缺少片段：{fragment!r}")
    return {"mode": "offline_fake", "actual": actual, "failures": failures}


def _run_all(cases: list[dict[str, Any]], suite: SuiteResult, work_root: Path) -> None:
    for case in cases:
        started = time.perf_counter()
        try:
            details = _run_case(case, work_root)
            passed = not details["failures"]
        except Exception as exc:  # 单条失败必须转成门禁结果，而非跳过该用例。
            details = {
                "mode": "offline_fake",
                "actual": {},
                "failures": [f"{type(exc).__name__}: {str(exc)[:500]}"],
            }
            passed = False
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        suite.cases[case["case_id"]] = CaseResult(
            case_id=case["case_id"],
            passed=passed,
            cost_usd=0.0,
            latency_ms=latency_ms,
            details=details,
        )


def run_offline_suite(path: str | Path, work_dir: str | Path | None = None) -> SuiteResult:
    """执行完整离线套件；所有结果均来自当前源码的真实运行。"""
    suite_id, cases = load_offline_cases(path)
    suite = SuiteResult(variant=suite_id, source=str(Path(path)))
    # 防止开发机上的 LangSmith 配置把离线运行变成外部网络调用。
    with patch.dict(os.environ, {"LANGSMITH_TRACING": "false"}, clear=False):
        if work_dir is not None:
            root = Path(work_dir)
            root.mkdir(parents=True, exist_ok=True)
            _run_all(cases, suite, root)
        else:
            with tempfile.TemporaryDirectory(prefix="data-agent-eval-") as temporary:
                _run_all(cases, suite, Path(temporary))
    return suite
