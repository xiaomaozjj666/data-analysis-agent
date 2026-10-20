"""把一次分析固化成可重跑的 recipe.json。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from data_agent._workspace_bundles import _atomic_write_text
from data_agent.serialization import json_text, to_jsonable
from data_agent.workspace import DataWorkspace

RECIPE_FILENAME = "analysis_recipe.json"


def data_fingerprint(workspace: DataWorkspace) -> dict[str, Any]:
    frame = workspace.dataframe
    sample = frame.head(5).to_csv(index=False).encode("utf-8")
    digest = hashlib.sha256(sample).hexdigest()[:16]
    return {
        "rows": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(column): str(frame[column].dtype) for column in frame.columns[:40]},
        "sample_sha256_16": digest,
    }


def collect_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    from langchain_core.messages import AIMessage

    for message in messages:
        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
            for call in message.tool_calls:
                args = call.get("args", {})
                calls.append({"name": str(call.get("name")), "args": args if isinstance(args, dict) else {}})
    return calls


def build_recipe(
    *,
    task: str,
    plan: list[dict[str, Any]],
    completed_steps: list[dict[str, Any]],
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    steps = []
    for item in completed_steps:
        steps.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "tool_calls": item.get("tool_calls") or [],
            }
        )
    return {
        "version": 1,
        "task": task,
        "data_fingerprint": fingerprint,
        "plan": plan,
        "steps": steps,
    }


def save_recipe(workspace: DataWorkspace, recipe: dict[str, Any]) -> str:
    path = workspace.artifacts_dir / RECIPE_FILENAME
    _atomic_write_text(path, json_text(to_jsonable(recipe)))
    workspace.register_artifact(path, "recipe", "可重跑的分析配方")
    return str(path)


def load_recipe(workspace: DataWorkspace) -> dict[str, Any] | None:
    path = workspace.artifacts_dir / RECIPE_FILENAME
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def replay_recipe(workspace: DataWorkspace, recipe: dict[str, Any], tools: list[Any]) -> dict[str, Any]:
    """按配方顺序重放工具调用，不再走规划器。"""
    tool_map = {item.name: item for item in tools}
    results: list[dict[str, Any]] = []
    skipped = 0
    for step in recipe.get("steps") or []:
        for call in step.get("tool_calls") or []:
            name = call.get("name")
            args = call.get("args") or {}
            tool = tool_map.get(name)
            if tool is None:
                skipped += 1
                results.append({"name": name, "status": "skipped", "detail": "工具不存在"})
                continue
            try:
                output = tool.invoke(args)
                results.append({"name": name, "status": "ok", "detail": str(output)[:4000]})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "status": "error", "detail": str(exc)[:800]})
    return {
        "task": recipe.get("task", ""),
        "fingerprint_now": data_fingerprint(workspace),
        "fingerprint_then": recipe.get("data_fingerprint"),
        "calls": results,
        "skipped": skipped,
    }


def replay_report_markdown(replay: dict[str, Any]) -> str:
    then = replay.get("fingerprint_then") or {}
    now = replay.get("fingerprint_now") or {}
    changed = then.get("rows") != now.get("rows") or then.get("columns") != now.get("columns")
    lines = [
        "## 结论速览",
        f"- 已按上次分析配方重跑，共执行 {len(replay.get('calls') or [])} 次工具调用。",
        f"- 数据规模：{now.get('rows')} 行 × {len(now.get('columns') or [])} 列"
        + ("（与上次不同，请核对口径）" if changed else "（与上次一致）"),
        "",
        "## 关键发现",
        "下列结果来自工具原始输出，不是模型改写。",
        "",
    ]
    for index, call in enumerate(replay.get("calls") or [], start=1):
        lines.append(f"### {index}. {call.get('name')}（{call.get('status')}）")
        lines.append("")
        lines.append("```")
        lines.append(str(call.get("detail") or "")[:1500])
        lines.append("```")
        lines.append("")
    lines.extend(
        [
            "## 注意事项与建议",
            "- 配方重跑不会重新规划；若列名或业务口径已变，请发起新的分析。",
            "- 图表会按原参数再生；请打开产物中心核对。",
        ]
    )
    return "\n".join(lines)
