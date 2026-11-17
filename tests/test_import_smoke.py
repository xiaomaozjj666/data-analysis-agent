"""应用装配冒烟：防止 routers 与 payload 常量漂移导致「ruff/mypy 绿但起不来」。"""

from __future__ import annotations


def test_api_module_imports() -> None:
    from data_agent import api

    assert api.app is not None
