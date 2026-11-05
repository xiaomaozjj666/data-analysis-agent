"""按 HTTP 客户端隔离的运行时配置。

``runtime_settings`` 保留为无上下文调用（CLI、旧集成与测试）的默认配置；
Web 请求由中间件绑定一个随机、长期的客户端作用域。作用域只保存用户显式
覆盖的字段，未覆盖字段仍继承环境变量与默认配置，避免不同浏览器互相覆盖
API Key、thinking 与 reasoning_effort。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

runtime_settings: dict[str, Any] = {
    "api_key": "",
    "thinking_enabled": None,
    "reasoning_effort": None,
}
runtime_settings_lock = threading.RLock()

# ContextVar 同时隔离 async task 与线程上下文；分析路由在启动 worker 前
# 合成独立 AgentSettings，因此 worker 不会读取另一个请求的配置。
_current_scope: ContextVar[str | None] = ContextVar("data_agent_runtime_scope", default=None)
_scoped_settings: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MAX_SCOPES = 10_000


@contextmanager
def bind_runtime_scope(scope: str | None) -> Iterator[None]:
    """在当前请求上下文绑定配置作用域，退出时无条件恢复。"""
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


def has_runtime_scope() -> bool:
    """当前调用是否处于 Web 客户端作用域。"""
    return _current_scope.get() is not None


def runtime_settings_snapshot() -> dict[str, Any]:
    """返回默认配置与当前作用域覆盖项的一致快照。"""
    scope = _current_scope.get()
    with runtime_settings_lock:
        merged = dict(runtime_settings)
        if scope is not None and scope in _scoped_settings:
            _scoped_settings.move_to_end(scope)
            merged.update(_scoped_settings[scope])
        return merged


def update_runtime_settings(**values: Any) -> None:
    """更新当前作用域；无作用域时保持旧的进程级默认行为。"""
    scope = _current_scope.get()
    with runtime_settings_lock:
        if scope is None:
            runtime_settings.update(values)
            return
        if scope not in _scoped_settings and len(_scoped_settings) >= _MAX_SCOPES:
            # 限制匿名客户端产生的状态总量；最久未使用的配置优先淘汰。
            _scoped_settings.popitem(last=False)
        target = _scoped_settings.setdefault(scope, {})
        target.update(values)
        _scoped_settings.move_to_end(scope)
