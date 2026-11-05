"""沙箱对抗性回归测试：验证 env 白名单 + 桥接属性链防护。

覆盖原 PoC：
- 子进程不再透传 DEEPSEEK_API_KEY / APP_ACCESS_TOKEN 等敏感环境变量。
- AST 校验拒绝通过 ``pd.io.common.os`` / ``pandas.compat.os`` / ``np.core`` 等
  桥接属性链逃逸 import 白名单的代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 让测试可以独立 import data_agent（不依赖 conftest 的 sys.path 注入顺序）。
PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from data_agent.sandbox_runtime import _validate_code  # noqa: E402
from data_agent.tools._sandbox import (  # noqa: E402
    _ENV_ALLOWED_EXACT,
    _ENV_FORBIDDEN_SUBSTRINGS,
    _build_child_env,
)

# ---------------------------------------------------------------------------
# 子进程环境变量白名单
# ---------------------------------------------------------------------------


def test_child_env_drops_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-leak-me")
    monkeypatch.setenv("APP_ACCESS_TOKEN", "app-token-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("PATH", "C:/Windows")
    env = _build_child_env("/tmp/pkg")
    assert "DEEPSEEK_API_KEY" not in env
    assert "APP_ACCESS_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    # 白名单中的无害变量保留
    assert env.get("PATH") == "C:/Windows"


def test_child_env_whitelist_is_exact_match() -> None:
    """白名单本身不能包含任何敏感前缀。"""
    for name in _ENV_ALLOWED_EXACT:
        upper = name.upper()
        for tag in _ENV_FORBIDDEN_SUBSTRINGS:
            assert tag not in upper, f"白名单 {name} 命中敏感前缀 {tag}"


def test_child_env_filters_pythonpath_with_sensitive_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(["/safe/path", "/secret/DEEPSEEK_API_KEY_stuff", "/another"]),
    )
    env = _build_child_env("/pkg")
    pypath = env.get("PYTHONPATH", "")
    assert "/safe/path" in pypath
    assert "/another" in pypath
    assert "DEEPSEEK_API_KEY_stuff" not in pypath


def test_child_env_injects_package_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _build_child_env("/pkg/root")
    assert env["PYTHONPATH"].split(os.pathsep)[0] == "/pkg/root"


# ---------------------------------------------------------------------------
# AST 桥接属性链防护（原 PoC：pd.io.common.os.environ 可访问 os.environ）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        # 原 PoC：通过 pandas 内部模块桥接到 os
        "result = pd.io.common.os.environ",
        "result = pd.io.common.os",
        "result = pd.io.common",
        "result = pd.io",
        "result = pd.compat.os",
        "result = pd._libs",
        "result = pd.core",
        # numpy 桥接
        "result = np.core",
        "result = np.compat",
        "result = np.io",
        # 通过 import 白名单模块的属性链
        "import pandas as p\nresult = p.io.common.os.system('echo 1')",
        "import pandas\nresult = pandas.io.common.os",
        # 任意表达式访问危险属性名
        "x = df\nresult = x.os",
        "result = some_module.environ",
        "result = some_module.system",
        "result = some_module.subprocess",
        "result = some_module.importlib",
        "result = some_module.builtins",
        "result = some_module.eval",
        "result = some_module.exec",
        "result = some_module.open",
        "result = some_module.__import__",
    ],
)
def test_validate_code_rejects_bridge_chains(code: str) -> None:
    with pytest.raises(ValueError):
        _validate_code(code)


@pytest.mark.parametrize(
    "code",
    [
        # 常见合法 pandas / numpy 用法应当放行
        "result = pd.DataFrame({'a': [1, 2]})",
        "result = pd.concat([df, df])",
        "result = np.mean([1, 2, 3])",
        "result = np.array([1, 2, 3]).sum()",
        "result = df.groupby('a').sum()",
        "result = df['a'].mean()",
        "import math\nresult = math.sqrt(4)",
        "import statistics\nresult = statistics.median([1, 2, 3])",
        "import json\nresult = json.dumps({'a': 1})",
        "result = pd.Series([1, 2, 3]).rolling(2).mean()",
    ],
)
def test_validate_code_allows_safe_usage(code: str) -> None:
    _validate_code(code)  # 不应抛异常


def test_validate_code_rejects_underscore_attrs() -> None:
    """下划线属性仍然禁止（保留原有护栏）。"""
    with pytest.raises(ValueError):
        _validate_code("result = df.__class__")


def test_validate_code_rejects_forbidden_import() -> None:
    with pytest.raises(ValueError):
        _validate_code("import os\nresult = os.environ")


def test_validate_code_rejects_forbidden_names() -> None:
    for code in (
        "result = eval('1')",
        "result = exec('1')",
        "result = open('x').read()",
        "result = getattr(df, 'shape')",
    ):
        with pytest.raises(ValueError):
            _validate_code(code)
