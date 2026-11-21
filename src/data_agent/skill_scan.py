"""技能包静态安全扫描（对标 NVIDIA SkillSpector 的评分思路）。

背景（2026-11 GitHub 趋势）：Agent Skills 已是标准分发格式（anthropics/skills
198k★），随之出现专门的技能安全扫描器（NVIDIA/SkillSpector 一个月 27k★）——
研究表明约 26% 的公开技能含漏洞、5.2% 有恶意意图。安装「别人给的技能包」
等价于把外部指令引入 Agent 上下文，必须像装依赖一样先扫再装。

设计原则（与本仓库「无验证不合入」纪律一致）：
- **纯静态、绝不执行**被扫内容：只做文本/结构检查，规则来自 SkillSpector
  公开类别的本地化子集（指令覆盖、隐藏文本、数据外传、凭据访问、供应链、
  过度授权），不做 LLM 复核、不做 OSV 在线查询。
- **评分体系沿用其公开口径**：critical +50 / high +25 / medium +10 / low +5；
  包内存在可执行内容（配方含 ``run_python_code`` 或额外可执行文件）时 ×1.3。
  ≤20 LOW(SAFE) / ≤50 MEDIUM(CAUTION) / ≤80 HIGH / >80 CRITICAL，
  HIGH/CRITICAL 属 "DO NOT INSTALL"。
- 扫描只读；与安装解耦：``install_skill`` 默认拒绝 HIGH/CRITICAL，
  显式 ``accept_risk=True`` 才能越过并留下记录。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: 约定文件（与 data_agent.skill 保持一致；此处不 import 以避免循环依赖）。
SKILL_MD_NAME = "SKILL.md"
RECIPE_JSON_NAME = "analysis_recipe.json"
MANIFEST_JSON_NAME = "skill.json"
BUNDLE_FILES: tuple[str, ...] = (SKILL_MD_NAME, RECIPE_JSON_NAME, MANIFEST_JSON_NAME)

#: 可执行内容判定：额外文件后缀 / 目录名。
_EXEC_SUFFIXES = frozenset({".sh", ".py", ".pyc", ".exe", ".bat", ".cmd", ".ps1", ".js", ".mjs"})
_EXEC_DIRNAMES = frozenset({"scripts", "bin"})

#: 严重度 → 加分（SkillSpector 公开口径）。
_SEVERITY_POINTS = {"critical": 50, "high": 25, "medium": 10, "low": 5}

#: 静态规则：(规则 ID, 严重度, 编译正则)。模式为中英双语子集，逐字匹配不做语义判断。
_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (rule_id, severity, re.compile(pattern, re.IGNORECASE))
    for rule_id, severity, pattern in (
        ("P1-INJECT-EN", "high",
         r"(ignore|disregard|override|forget)\s+(all\s+)?(previous|prior|above|earlier|system)\s+"
         r"(instructions|prompts?|rules?|guardrails?)"),
        ("P1-INJECT-ZH", "high",
         r"忽略(之前|以上|前面|系统|安全|所有)?(的)?(所有)?(指令|提示|规则)"),
        ("P2-HIDDEN-ZWSP", "medium",
         r"[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\ufeff]"),
        ("P2-HIDDEN-HTML-CMD", "medium",
         r"<!--[^>]{0,240}(执行|run\s|execute|curl|wget|python\s)[^>]{0,240}-->"),
        ("E-EXFIL-CURLPIPE", "critical",
         r"(curl|wget)\b[^|;\n]{0,200}\|\s*(ba)?sh"),
        ("E-EXFIL-POST", "medium",
         r"(curl\s+-[Td]|httpx\.(post|put)|requests\.(post|put)"
         r"|fetch\([^)]{0,120}method:\s*[\"'](POST|PUT))"),
        ("E-ENV-HARVEST", "high",
         r"(os\.environ|os\.getenv|process\.env|getenv)\s*[\.\[\"']{0,3}"
         r"[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"),
        ("PE-CRED-ACCESS", "high",
         r"(\.ssh[/\\]|id_rsa|\.aws[/\\]credentials|\.netrc|\.kube[/\\]config|\.env\b)"),
        ("SC-B64DECODE", "high", r"base64\s+(-d|--decode)"),
        ("SC-DYNEVAL", "medium", r"\b(eval|exec)\s*\("),
        ("AG-RMRF", "critical", r"rm\s+-rf?\s+([/~]|[A-Za-z]:\\)"),
        ("AG-SUDO", "medium", r"\bsudo\b"),
        ("AG-CHMOD", "medium", r"chmod\s+(-R\s+)?777"),
        ("AG-SYSPATH", "medium", r"(/etc/passwd|/etc/shadow|C:\\Windows\\)"),
        ("SC-UNPINNED", "low", r"pip3?\s+install\s+[A-Za-z0-9_.\-\[\]]+(?![=<>~])"),
    )
)

_VERDICT_BANDS: tuple[tuple[int, str, str], ...] = (
    (20, "LOW", "SAFE"),
    (50, "MEDIUM", "CAUTION"),
    (80, "HIGH", "DO NOT INSTALL"),
)
_CRITICAL_BAND = ("CRITICAL", "DO NOT INSTALL")

#: install 门槛：超过该分数（HIGH/CRITICAL 档）默认拒绝。
INSTALL_BLOCK_SCORE = 80


def _severity_for_score(score: int) -> tuple[str, str]:
    for ceiling, severity, verdict in _VERDICT_BANDS:
        if score <= ceiling:
            return severity, verdict
    return _CRITICAL_BAND


def _front_matter(text: str) -> dict[str, str] | None:
    """解析 SKILL.md 顶部的 YAML front-matter（仅支持 ``key: value`` 平铺）。"""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, _, value = line.partition(":")
        if key.strip() and value.strip():
            fields[key.strip()] = value.strip()
    return fields or None


def _line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _snippet(text: str, position: int, length: int = 120) -> str:
    return " ".join(text[position : position + length].split())[:120]


def scan_skill_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    """静态扫描技能包目录，返回评分/分级/逐条发现（绝不执行其中内容）。

    Raises:
        FileNotFoundError: 目录不存在。
    """
    bundle = Path(bundle_dir).expanduser().resolve()
    if not bundle.is_dir():
        raise FileNotFoundError(f"技能包目录不存在：{bundle}")

    findings: list[dict[str, Any]] = []
    files_scanned: list[str] = []
    texts: dict[str, str] = {}
    front_matter: dict[str, str] | None = None
    manifest: dict[str, Any] = {}

    # 1) 约定文件：缺失记 medium（install 侧另有强校验，这里保证报告完整）
    for filename in BUNDLE_FILES:
        path = bundle / filename
        if not path.is_file():
            findings.append(
                {
                    "id": "SC-MISSING-FILE",
                    "severity": "medium",
                    "file": filename,
                    "line": 0,
                    "snippet": "约定文件缺失",
                }
            )
            continue
        files_scanned.append(filename)
        texts[filename] = path.read_text(encoding="utf-8", errors="replace")

    # 2) 逐规则扫描约定文件文本
    for filename, text in texts.items():
        for rule_id, severity, pattern in _RULES:
            for match in pattern.finditer(text):
                findings.append(
                    {
                        "id": rule_id,
                        "severity": severity,
                        "file": filename,
                        "line": _line_of(text, match.start()),
                        "snippet": _snippet(text, match.start()),
                    }
                )
        if filename == SKILL_MD_NAME:
            front_matter = _front_matter(text)

    # 3) 额外可执行内容：不在约定内的可执行文件/脚本目录
    executable = False
    for child in sorted(bundle.rglob("*")):
        relative = child.relative_to(bundle)
        if child.is_dir():
            if relative.parts and relative.parts[0] in _EXEC_DIRNAMES:
                executable = True
            continue
        if relative.as_posix() in BUNDLE_FILES:
            continue
        if child.suffix.lower() in _EXEC_SUFFIXES:
            executable = True
            findings.append(
                {
                    "id": "SC-EXTRA-EXEC",
                    "severity": "medium",
                    "file": relative.as_posix(),
                    "line": 0,
                    "snippet": "约定外可执行文件（不会被 install 复制，但应审查来源）",
                }
            )

    # 4) 配方含 run_python_code：技能在消费方具备执行任意代码的能力 → ×1.3
    if '"run_python_code"' in texts.get(RECIPE_JSON_NAME, ""):
        executable = True

    # 5) front-matter 一致性（SKILL.md 规范：name/description 必填）
    manifest_text = texts.get(MANIFEST_JSON_NAME)
    if manifest_text:
        try:
            loaded = json.loads(manifest_text)
            manifest = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            manifest = {}
        slug = str(manifest.get("slug") or "")
        if front_matter is not None and slug:
            fm_name = str(front_matter.get("name") or "")
            if fm_name and fm_name != slug:
                findings.append(
                    {
                        "id": "FM-NAME-MISMATCH",
                        "severity": "low",
                        "file": SKILL_MD_NAME,
                        "line": 1,
                        "snippet": f"front-matter name={fm_name!r} != skill.json slug={slug!r}",
                    }
                )

    # 6) 评分
    score = sum(_SEVERITY_POINTS.get(item["severity"], 0) for item in findings)
    if executable:
        score = min(100, int(score * 1.3))
    severity, verdict = _severity_for_score(score)

    return {
        "bundle": str(bundle),
        "score": int(score),
        "severity": severity,
        "verdict": verdict,
        "executable": executable,
        "findings": findings,
        "files_scanned": files_scanned,
        "front_matter": front_matter,
        "slug": str(manifest.get("slug") or ""),
    }
