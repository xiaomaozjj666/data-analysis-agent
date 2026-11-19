"""kaleido 1.5.1 PNG 导出竞态的兼容补丁（就绪等待）。

现象：kaleido 1.5.1 + choreographer 1.4.0 每次 ``write_image`` 都会先启动一个无头
浏览器，再导航到内置页面。``_KaleidoTab.navigate`` 在 ``Page.loadEventFired`` 之后
就返回，而此时 ``vendor/kaleido_scopes.js`` 可能还没执行完，随后取到的 JS 上下文里
没有 ``kaleido_scopes``，调用即报 ``ReferenceError: kaleido_scopes is not defined``。
本机实测失败率约 60%~80%；重试、常驻 server、关闭 MathJax 都无效。

做法：包装 ``_KaleidoTab.navigate``，原导航完成后用 ``Runtime.evaluate`` 每 0.1 秒轮询
``typeof kaleido_scopes === 'object'``，就绪即返回，上限 10 秒。超时不抛错，只记日志，
把原本的报错路径原样留给调用方，因此补丁不会引入新的失败模式。额外等待约 1.4 秒。

这是绕 kaleido 1.5.1 的竞态；kaleido 升级后应先去掉本补丁复测，确认修复即可整体删除
（连同 builder / artifacts 里的 ``install()`` 调用与 ``tests/test_kaleido_compat.py``）。

安全边界：仅当已安装的 kaleido 版本恰为 1.5.1，且预期的私有类与方法都存在时才打补丁；
版本或结构不符就不动，只写一条 debug 日志，绝不让导入或导出因此失败。导入本模块不会加载
kaleido（保持 ``import data_agent.api`` 的启动速度），kaleido 只在 ``install()`` 内惰性导入。
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import importlib.metadata
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

#: 唯一已验证存在该竞态的 kaleido 版本；其他版本不打补丁。
_SUPPORTED_KALEIDO_VERSION = "1.5.1"
#: 就绪判定表达式：kaleido_scopes.js 执行完后会在全局挂出 kaleido_scopes 对象。
_READY_EXPRESSION = "typeof kaleido_scopes === 'object'"
#: 轮询间隔（秒）。
_POLL_INTERVAL = 0.1
#: 等待上限（秒）。
_WAIT_LIMIT = 10.0
#: 标记属性：挂在被包装后的 navigate 上，用于识别"已打过补丁"。
_MARKER = "_data_agent_ready_wait"

_lock = threading.Lock()
_installed = False


def _installed_kaleido_version() -> str | None:
    """返回已安装 kaleido 的版本号；未安装返回 None（不导入 kaleido 本体）。"""
    try:
        return importlib.metadata.version("kaleido")
    except importlib.metadata.PackageNotFoundError:
        return None


def _is_ready(response: Any) -> bool:
    """解析 ``Runtime.evaluate`` 的返回，判断 kaleido_scopes 是否已就绪。

    只有明确拿到布尔真值才算就绪；协议报错、缺字段、异常形态一律视为未就绪。
    """
    if not isinstance(response, dict) or "error" in response:
        return False
    result = response.get("result")
    inner = result.get("result") if isinstance(result, dict) else None
    return isinstance(inner, dict) and inner.get("value") is True


async def _wait_until_ready(
    tab: Any,
    context_id: int | None,
    *,
    limit: float = _WAIT_LIMIT,
    interval: float = _POLL_INTERVAL,
) -> bool:
    """在 ``context_id`` 对应的 JS 上下文里轮询 kaleido_scopes，就绪返回 True。

    超过 ``limit`` 秒仍未就绪返回 False；单次 evaluate 失败（页面刷新、会话关闭等）
    当作"还没好"继续轮询，不向上抛。``context_id`` 为空时不带上下文参数，落到当前页面。
    """
    params: dict[str, Any] = {"expression": _READY_EXPRESSION, "returnByValue": True}
    if context_id is not None:
        params["contextId"] = context_id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while True:
        try:
            if _is_ready(await tab.send_command("Runtime.evaluate", params=params)):
                return True
        except Exception as exc:  # noqa: BLE001 - 轮询期间任何单次失败都不应中断导出
            log.debug("kaleido 就绪探测单次失败（继续轮询）：%s", exc)
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)


def _wrap_navigate(original: Any) -> Any:
    """返回带就绪等待的 navigate；原方法的参数、返回值与异常原样透传。"""

    @functools.wraps(original)
    async def navigate_and_wait(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        ready = await _wait_until_ready(
            getattr(self, "tab", None), getattr(self, "_current_js_id", None)
        )
        if not ready:
            log.warning(
                "kaleido_scopes 在 %.0f 秒内未就绪，按原流程继续（可能触发 ReferenceError）",
                _WAIT_LIMIT,
            )
        return result

    setattr(navigate_and_wait, _MARKER, True)
    return navigate_and_wait


#: 表示"服务器环境缺 Chrome / 浏览器依赖"的异常类名（按类名匹配，免得为判错而导入 kaleido）。
_ENV_MISSING_ERROR_NAMES = frozenset({"ChromeNotFoundError", "BrowserDepsError"})
#: plotly 在 kaleido 未安装或版本过低时抛的是普通 RuntimeError，只能按其固定文案识别。
_KALEIDO_REQUIRED_MARKER = "Image export requires the Kaleido package"
#: 沿异常链向下查找的最大层数，防止异常链成环。
_MAX_CHAIN_DEPTH = 8

CHROME_MISSING_HINT = (
    "服务器没有可用的 Chrome/Chromium，无法在服务端渲染 PNG。"
    "请在服务器上安装 Chrome，或执行 kaleido_get_chrome 下载；"
    "无法安装时可在图表预览页使用浏览器内的「导出 PNG」按钮。"
)
KALEIDO_MISSING_HINT = (
    "服务器未安装图片渲染依赖（kaleido）。"
    '请执行 pip install "kaleido>=1.5.1,<2" 后重试；'
    "无法安装时可在图表预览页使用浏览器内的「导出 PNG」按钮。"
)


def render_env_problem(exc: BaseException) -> str | None:
    """判断渲染异常是否由"服务器环境缺失"引起，是则返回可操作的中文提示，否则返回 None。

    识别三类：kaleido 本身没装（``ImportError`` 或 plotly 的固定提示）、找不到 Chrome
    （``ChromeNotFoundError``，plotly 会把它改包成普通 ``RuntimeError``，因此要沿
    ``__cause__`` / ``__context__`` 往下找）、浏览器缺系统依赖（``BrowserDepsError``）。
    其余异常（页面脚本报错、超时、内存不足等）都属于"渲染失败"，返回 None。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ImportError) or _KALEIDO_REQUIRED_MARKER in str(current):
            return KALEIDO_MISSING_HINT
        if any(cls.__name__ in _ENV_MISSING_ERROR_NAMES for cls in type(current).__mro__):
            return CHROME_MISSING_HINT
        current = current.__cause__ or current.__context__
    return None


def is_installed() -> bool:
    """补丁当前是否已生效。"""
    return _installed


def install() -> bool:
    """安装就绪等待补丁；返回补丁是否生效。幂等且线程安全，永不抛异常。

    kaleido 版本不是 1.5.1、未安装，或私有类/方法结构与预期不符时返回 False 且不改动
    任何东西。版本/结构不符的结果不缓存：每次调用都重新判定，成本只是一次元数据读取。
    """
    global _installed
    with _lock:
        if _installed:
            return True
        version = _installed_kaleido_version()
        if version != _SUPPORTED_KALEIDO_VERSION:
            log.debug(
                "kaleido 版本为 %s，不是 %s，跳过就绪等待补丁", version, _SUPPORTED_KALEIDO_VERSION
            )
            return False
        try:
            module = importlib.import_module("kaleido._kaleido_tab")
            tab_cls = module._KaleidoTab
            original = tab_cls.navigate
            if not asyncio.iscoroutinefunction(original):
                raise TypeError("_KaleidoTab.navigate 不是协程函数")
        except Exception as exc:  # noqa: BLE001 - 结构不符属预期降级，不是错误
            log.debug("kaleido 内部结构与预期不符，跳过就绪等待补丁：%s", exc)
            return False
        if getattr(original, _MARKER, False):
            # 其他入口（如重载模块后的旧状态）已经包装过，不重复包装。
            _installed = True
            return True
        tab_cls.navigate = _wrap_navigate(original)
        _installed = True
        log.debug("已安装 kaleido %s 就绪等待补丁", version)
        return True
