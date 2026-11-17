"""把分析配方导出为可版本化的技能包（SKILL.md + analysis_recipe.json）。

设计目标（来源：2026-11 竞品实地复核，见 docs/competitive-analysis.md 第六节）：
Trae 的 `SKILL.md`（指令/脚本/资源 + 项目级/全局双目录 + 市场分发）与
Excel Copilot 的 custom skill（可录制、可复用）都证明"技能是可复用资产"。
本项目已有的 ``analysis_recipe.json`` 已经固化了工具调用参数，这里只把它
包装成人和 Agent 都能读的技能包，不改动重跑路径（重跑仍走 recipe.replay_recipe）。

取舍：
- **只导出配方，不导出 LLM 推理**：配方是确定性工具调用，重跑不需要模型；
  导出物里明确写出这条边界，避免被误当成"可替代分析的黑盒"。
- **不覆盖已有目录**：除非显式 ``force``；技能包会被拷进其他 Agent 的技能
  目录，静默覆盖会让别人手里的旧版本凭空变化。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from data_agent._workspace_bundles import _atomic_write_text
from data_agent.recipe import RECIPE_FILENAME

#: 技能包内文件约定。
SKILL_DOC = "SKILL.md"
MANIFEST_FILE = "skill.json"
SKILL_VERSION = 1
_MAX_NAME_CHARS = 64


def _slug(value: str) -> str:
    """把任务名转成文件系统安全的 slug（保留中英文与数字，其余折叠为 -）。"""
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip(), flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-_")[:_MAX_NAME_CHARS]
    return text or "analysis-skill"


def _recipe_digest(recipe: dict[str, Any]) -> str:
    canonical = json.dumps(recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _tool_names(recipe: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for step in recipe.get("steps") or []:
        for call in step.get("tool_calls") or []:
            name = str(call.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _render_skill_doc(
    recipe: dict[str, Any],
    *,
    name: str,
    created_at: str,
    recipe_sha256: str,
) -> str:
    fingerprint = recipe.get("data_fingerprint") or {}
    columns = fingerprint.get("columns") or []
    lines = [
        f"# {name}",
        "",
        f"> 由 data-analysis-agent 的分析配方导出（技能格式 v{SKILL_VERSION}，{created_at}）。",
        "",
        "## 适用场景",
        "",
        str(recipe.get("task") or "（配方未记录任务描述）"),
        "",
        "## 输入与前置条件",
        "",
        f"- 数据规模（导出时）：{fingerprint.get('rows', '未知')} 行 × {len(columns)} 列",
        f"- 列：{'、'.join(str(column) for column in columns[:30]) or '（未记录）'}",
        f"- 数据样本指纹：`{fingerprint.get('sample_sha256_16', '')}`",
        "- 指标口径：导出目录不内嵌 metrics.json；复用时把会话里的指标定义放到"
        " 数据目录，或设置 `DATA_AGENT_METRICS_PATH`。",
        "",
        "## 执行步骤（确定性工具调用）",
        "",
    ]
    step_index = 0
    for step in recipe.get("steps") or []:
        calls = step.get("tool_calls") or []
        if not calls:
            continue
        lines.append(f"### {step.get('title') or step.get('id') or '步骤'}")
        for call in calls:
            step_index += 1
            args = call.get("args") or {}
            summary = json.dumps(args, ensure_ascii=False)[:300]
            lines.append(f"{step_index}. `{call.get('name')}` — 参数：`{summary}`")
        lines.append("")
    if step_index == 0:
        lines.extend(["（配方中没有可重放的工具调用。）", ""])
    lines.extend(
        [
            "## 复用方式",
            "",
            "```bash",
            "# 按配方重跑（不需要 LLM，沿用导出时的工具参数）",
            "python -m data_agent.skill run --bundle <本目录> --data <数据文件>",
            "```",
            "",
            "也可以把本目录作为技能包登记到其他 Agent 的技能目录；"
            "`analysis_recipe.json` 是机器可读版本，`skill.json` 是清单。",
            "",
            "## 边界（请先读）",
            "",
            "- 固化的是**工具调用参数**，不是模型推理过程；结论解释仍需重新生成。",
            "- 数据 schema 变化（列名/类型/行数显著变化）时先核对上面的指纹，不要直接重放。",
            f"- 配方内容哈希：`{recipe_sha256}`；修改配方后应重新导出并升版本。",
            "",
        ]
    )
    return "\n".join(lines)


def build_skill_bundle(
    recipe: dict[str, Any],
    *,
    name: str | None = None,
    created_at: str | None = None,
) -> dict[str, str]:
    """构建技能包内容（不落盘），返回 ``{文件名: 文本}``。"""
    if not isinstance(recipe, dict) or not isinstance(recipe.get("task"), str):
        raise ValueError("配方必须是包含 task 字段的 JSON 对象。")
    task = recipe["task"].strip() or "analysis"
    skill_name = (name or task)[:120]
    timestamp = created_at or datetime.now(UTC).isoformat(timespec="seconds")
    digest = _recipe_digest(recipe)
    tools = _tool_names(recipe)
    manifest = {
        "name": skill_name,
        "slug": _slug(skill_name),
        "version": SKILL_VERSION,
        "created_at": timestamp,
        "task": task,
        "recipe_sha256": digest,
        "data_fingerprint": recipe.get("data_fingerprint") or {},
        "tools": tools,
        "files": [SKILL_DOC, RECIPE_FILENAME, MANIFEST_FILE],
    }
    return {
        SKILL_DOC: _render_skill_doc(
            recipe, name=skill_name, created_at=timestamp, recipe_sha256=digest
        ),
        RECIPE_FILENAME: json.dumps(recipe, ensure_ascii=False, indent=2) + "\n",
        MANIFEST_FILE: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }


def load_session_recipe(session_root: str | Path) -> tuple[Path, dict[str, Any]]:
    """从会话目录读出 `analysis_recipe.json`（不存在或损坏时报错）。"""
    base = Path(session_root).expanduser()
    path = base / "artifacts" / RECIPE_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"会话目录中没有 {RECIPE_FILENAME}：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 不是 JSON 对象。")
    return path, payload


def export_skill(
    session_root: str | Path,
    out_dir: str | Path,
    *,
    name: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """把会话配方导出为 ``<out_dir>/<slug>/`` 技能包，返回摘要。"""
    recipe_path, recipe = load_session_recipe(session_root)
    bundle = build_skill_bundle(recipe, name=name)
    manifest = json.loads(bundle[MANIFEST_FILE])
    target = Path(out_dir).expanduser().resolve() / manifest["slug"]
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        if not force:
            raise FileExistsError(f"技能目录已存在：{target}（确认覆盖请加 force）")
        # 先把旧包改名而不是直接删除：Windows 上目标目录被杀软/编辑器占用时
        # os.replace 会失败，直接 rmtree 会留下"旧包没了、新包也没成"的状态。
        backup = target.with_name(f".{target.name}.bak-{uuid4().hex[:8]}")
        os.replace(target, backup)
    staging = Path(tempfile.mkdtemp(prefix=".skill-", dir=target.parent))
    try:
        for filename, text in bundle.items():
            _atomic_write_text(staging / filename, text)
        os.replace(staging, target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    else:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "path": str(target),
        "name": manifest["name"],
        "slug": manifest["slug"],
        "version": manifest["version"],
        "recipe": str(recipe_path),
        "files": manifest["files"],
        "recipe_sha256": manifest["recipe_sha256"],
        "tools": manifest["tools"],
    }


def run_bundle(bundle_dir: str | Path, data_path: str | Path) -> dict[str, Any]:
    """按导出的技能包重跑：加载数据 → 逐步调用配方工具。

    依赖 LangChain 工具集，因此只在真正执行时导入；导出/查看技能包不需要
    模型与 API Key。
    """
    from data_agent.recipe import replay_recipe
    from data_agent.tools.builder import build_tools
    from data_agent.workspace import DataWorkspace

    bundle = Path(bundle_dir).expanduser()
    recipe_path = bundle / RECIPE_FILENAME
    if not recipe_path.is_file():
        raise FileNotFoundError(f"技能包缺少 {RECIPE_FILENAME}：{recipe_path}")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(recipe, dict):
        raise ValueError(f"{recipe_path} 不是 JSON 对象。")
    manifest_path = bundle / MANIFEST_FILE
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = str(manifest.get("recipe_sha256") or "") if isinstance(manifest, dict) else ""
        actual = _recipe_digest(recipe)
        if expected and expected != actual:
            raise ValueError(
                "技能包内的 analysis_recipe.json 与 skill.json 的配方哈希不一致，"
                "可能被改动过；请重新导出后再重跑。"
            )
    # 每次重跑用独立临时工作区并在结束时清理：固定 session id 会让并发重跑
    # 争用同一 artifacts 目录，且 runs/ 下的目录永不被 TTL 回收。
    temp_root = Path(tempfile.mkdtemp(prefix="data-agent-skill-"))
    try:
        workspace = DataWorkspace(temp_root, session_id=f"skill-{_slug(recipe.get('task', ''))}")
        workspace.load(data_path)
        tools = build_tools(workspace)
        return replay_recipe(workspace, recipe, tools)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data-agent-skill", description="分析配方技能包")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="把会话配方导出为技能包")
    export.add_argument(
        "--session", required=True, help="会话目录（含 artifacts/analysis_recipe.json）"
    )
    export.add_argument("--out", required=True, help="技能包输出目录")
    export.add_argument("--name", help="技能名称（默认取任务描述）")
    export.add_argument("--force", action="store_true", help="覆盖已存在的技能目录")
    run = sub.add_parser("run", help="按技能包重跑（不需要 LLM）")
    run.add_argument("--bundle", required=True, help="技能包目录")
    run.add_argument("--data", required=True, help="数据文件路径")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_skill(args.session, args.out, name=args.name, force=args.force)
        else:
            result = run_bundle(args.bundle, args.data)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
