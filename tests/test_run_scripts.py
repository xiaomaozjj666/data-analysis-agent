"""Windows 一键启动脚本的静态守护。

CI 跑在 Linux 上没有 PowerShell，这里只做文本层面的约束检查；
PS 5.1 / pwsh 7 的语法解析与分支行为在本机实测，见 CHANGELOG。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_PS1 = ROOT / "run.ps1"


def _script() -> str:
    return RUN_PS1.read_bytes().decode("utf-8-sig")


def test_run_ps1_is_utf8_with_bom() -> None:
    """Windows PowerShell 5.1 不带 BOM 时按 ANSI 读含中文脚本，会解析失败。"""
    assert RUN_PS1.read_bytes().startswith(b"\xef\xbb\xbf")


def test_run_ps1_has_no_curly_quotes() -> None:
    """PowerShell 把中文弯引号当字符串定界符，混进字符串会提前截断。"""
    text = _script()
    assert not set(text) & {"“", "”", "‘", "’"}


def test_run_ps1_python_candidates_match_pyproject() -> None:
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    match = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", requires.replace(" ", ""))
    assert match, requires
    expected = [f"3.{minor}" for minor in range(int(match[2]) - 1, int(match[1]) - 1, -1)]

    declared = re.search(r"\$SupportedPythons = @\(([^)]*)\)", _script())
    assert declared
    assert re.findall(r'"(3\.\d+)"', declared[1]) == expected
    # 不再硬编码单一版本创建虚拟环境。
    assert "py -3.13 -m venv" not in _script()


def test_run_ps1_node_floor_covers_frontend_requirement() -> None:
    engines = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))[
        "engines"
    ]["node"]
    floor = re.fullmatch(r">=(\d+)", engines.strip())
    assert floor, engines
    text = _script()
    major = int(re.search(r"\$MinNodeMajor = (\d+)", text)[1])
    minor = int(re.search(r"\$MinNodeMinor = (\d+)", text)[1])
    # 脚本门槛不低于 package.json，且满足 vite 8 的 >=22.12。
    assert major >= int(floor[1])
    assert (major, minor) >= (22, 12)
    assert "Test-NodeVersion $NodeVersion" in text


def test_run_ps1_never_writes_dotenv() -> None:
    """.env 存密钥，脚本只提示复制模板，不替用户创建或写入。"""
    text = _script()
    assert "Copy-Item .env.example .env" in text  # 提示文本
    for line in text.splitlines():
        statement = line.strip()
        if statement.startswith(("#", "Write-Host")) or ".env" not in statement:
            continue
        assert not re.search(r"\b(Copy-Item|Set-Content|Out-File|New-Item)\b", statement), line


def test_run_ps1_failure_does_not_block_non_interactive_runs() -> None:
    text = _script()
    assert "if ($Interactive) { Read-Host" in text
    assert re.search(r"^\s*Read-Host", text, flags=re.MULTILINE) is None
    assert "exit 1" in text


def test_run_ps1_network_failures_get_actionable_hint() -> None:
    text = _script()
    assert text.count('Write-NetworkHint "') == 2  # pip 与 npm 各一处
    # 镜像建议只作用于单条命令，不改全局配置。
    assert "pip config" not in text
    assert "npm config set" not in text


def test_run_ps1_frontend_port_scans_range() -> None:
    """前端不再硬编码 5173 单端口，而是在 5173-5182 间找可用端口。"""
    text = _script()
    assert re.search(r"\$DefaultWebPort = 5173", text)
    assert re.search(r"\$WebPortAttempts = 10", text)
    assert "$DefaultWebPort + $Attempt" in text
    # 实际启动时把尝试的端口传给 vite，并保持 strictPort 以暴露端口竞态。
    assert '"--port", "$TryPort", "--strictPort"' in text


def test_run_ps1_frontend_health_checks_project_marker() -> None:
    """健康探测不能只看状态码：必须命中 Vite 客户端与本项目首页特征。

    首页特征只用 ASCII（``id="root"`` / ``/src/main.tsx``）：Windows PowerShell
    5.1 的 Invoke-WebRequest 对无 charset 的 HTML 按 ISO-8859-1 解码，中文标题
    会匹配失败（2026-11-21 实测）。
    """
    text = _script()
    assert "/@vite/client" in text
    assert "createHotContext" in text
    assert 'id="root"' in text
    assert "/src/main" in text
    assert "if (Test-FrontendHealth $TryUrl)" in text
    assert "Test-FrontendHealth $WebUrl" in text


def test_run_ps1_uses_actual_frontend_url() -> None:
    """日志、最终健康确认与打开的浏览器都使用实际选中的前端地址。"""
    text = _script()
    assert "$WebUrl = $TryUrl" in text
    assert 'Write-Host "[Data Analysis] Frontend ready: $WebUrl"' in text
    assert "Start-Process $WebUrl" in text
    # 前端必须连到实际后端端口，而不是假定 8000。
    assert "$Env:VITE_API_URL = $ApiUrl" in text


def test_run_bat_delegates_to_run_ps1() -> None:
    bat = (ROOT / "run.bat").read_text(encoding="utf-8")
    assert '-File "%~dp0run.ps1"' in bat
