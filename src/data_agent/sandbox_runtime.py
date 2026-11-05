"""沙箱子进程与父进程共用的审查规则、受限 builtins 与结果压缩。

故意不依赖 langchain / FastAPI，这样 ``python -m data_agent.sandbox_worker``
启动时不会把整个 Web 栈拉进子进程。
"""

from __future__ import annotations

import ast
import builtins as _py_builtins
from typing import Any

import numpy as np
import pandas as pd

_ALLOWED_IMPORTS = frozenset(
    {
        "pandas",
        "numpy",
        "math",
        "statistics",
        "datetime",
        "re",
        "json",
        "collections",
        "itertools",
        "functools",
    }
)

_SANDBOX_STDOUT_MAX_CHARS = 4_000
_SANDBOX_RESULT_MAX_ROWS = 50
_SANDBOX_CODE_MAX_CHARS = 6_000

_FORBIDDEN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "memoryview",
        "type",
        "super",
        "object",
        "classmethod",
        "staticmethod",
        "property",
        "help",
    }
)

# 危险属性名：任意表达式通过属性链到达这些名字都直接拒绝。
# 覆盖 os/sys/subprocess/importlib/builtins 等桥接逃逸（典型 PoC：
# ``pd.io.common.os.environ``、``np.__loader__``、``pd.compat.os``）。
_FORBIDDEN_ATTR_NAMES = frozenset(
    {
        # 模块桥接目标：os / sys / subprocess / importlib / builtins / pathlib / shutil
        "os",
        "sys",
        "subprocess",
        "importlib",
        "builtins",
        "pathlib",
        "shutil",
        "socket",
        "ctypes",
        "pickle",
        "platform",
        "signal",
        "multiprocessing",
        "threading",
        # 高危内建/反射入口（_FORBIDDEN_NAMES 只挡 Name，挡不住 Attribute）
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        # 危险 I/O 与进程控制
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "fork",
        "execv",
        "execve",
        "execl",
        "environ",
        "environb",
        "putenv",
        "unsetenv",
        "getenv",
        "listdir",
        "makedirs",
        "mkdir",
        "rmdir",
        "remove",
        "unlink",
        "rename",
        "chdir",
        "chmod",
        "kill",
        # pandas 已知桥接入口：pd.io.common 暴露 os / pd.compat 暴露 sys 等
        "io",
        "common",
        "compat",
        "_libs",
        "_config",
        "_typing",
        "core",
    }
)

# 已知会暴露 os/sys 子属性的"桥接根"（一旦出现在属性链根部，整链拒绝）。
# 例：``pd.io.common.os``、``np.core``、``pandas.compat``。
_BRIDGE_ROOT_NAMES = frozenset({"pd", "pandas", "np", "numpy"})

_FORBIDDEN_METHODS = frozenset(
    {
        "read_csv",
        "read_excel",
        "read_json",
        "read_sql",
        "read_sql_query",
        "read_sql_table",
        "read_pickle",
        "read_html",
        "read_parquet",
        "read_feather",
        "read_hdf",
        "read_stata",
        "read_sas",
        "read_gbq",
        "read_orc",
        "read_spss",
        "read_xml",
        "read_fwf",
        "read_table",
        "to_csv",
        "to_excel",
        "to_json",
        "to_sql",
        "to_pickle",
        "to_html",
        "to_parquet",
        "to_feather",
        "to_hdf",
        "to_stata",
        "to_gbq",
        "to_orc",
        "to_latex",
        "to_markdown",
        "to_xml",
        "save",
        "savez",
        "savez_compressed",
        "load",
        "fromfile",
        "loadtxt",
        "savetxt",
        "memmap",
        "dump",
        "tofile",
    }
)

_SAFE_BUILTIN_NAMES = (
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "callable",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "hash",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "ord",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "Exception",
    "ArithmeticError",
    "AttributeError",
    "IndexError",
    "KeyError",
    "LookupError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
)


def _safe_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """白名单版 __import__：只放行 _ALLOWED_IMPORTS 中的顶层模块。"""
    if level != 0 or name.split(".")[0] not in _ALLOWED_IMPORTS:
        raise ImportError(
            f"沙箱禁止导入模块 {name!r}。允许的模块：{', '.join(sorted(_ALLOWED_IMPORTS))}。"
        )
    return _py_builtins.__import__(name, globals, locals, fromlist, level)


def _build_safe_builtins(print_buffer: list[str]) -> dict[str, Any]:
    """构造受限 builtins 字典：白名单函数 + 缓冲版 print + 白名单 import。"""
    safe: dict[str, Any] = {name: getattr(_py_builtins, name) for name in _SAFE_BUILTIN_NAMES}

    def _print(*args: Any, sep: str = " ", end: str = "\n", **_ignored: Any) -> None:
        print_buffer.append(sep.join(str(item) for item in args) + end)

    safe["print"] = _print
    safe["__import__"] = _safe_import
    return safe


def _attribute_chain_root(node: ast.AST) -> str | None:
    """取属性链最左端的根 Name（如 ``pd.io.common.os`` 的根为 ``pd``）。"""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _validate_code(code: str) -> None:
    """AST 静态审查：语法、import 白名单、下划线属性/名称、危险内建、桥接属性链。"""
    if len(code) > _SANDBOX_CODE_MAX_CHARS:
        raise ValueError(
            f"代码过长（{len(code)} 字符，上限 {_SANDBOX_CODE_MAX_CHARS}）。"
            "请拆分成更小的计算，或改用预定义工具。"
        )
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"代码语法错误：{exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    raise ValueError(
                        f"沙箱禁止导入模块 {alias.name!r}。"
                        f"允许的模块：{', '.join(sorted(_ALLOWED_IMPORTS))}。"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or (node.module or "").split(".")[0] not in _ALLOWED_IMPORTS:
                raise ValueError(
                    f"沙箱禁止导入模块 {node.module!r}。"
                    f"允许的模块：{', '.join(sorted(_ALLOWED_IMPORTS))}。"
                )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError(
                    f"沙箱禁止访问下划线开头的属性 {node.attr!r}（防止逃逸受限环境）。"
                )
            if node.attr in _FORBIDDEN_METHODS:
                raise ValueError(
                    f"沙箱禁止调用文件 I/O 方法 {node.attr!r}。沙箱仅支持内存计算，无法读写文件。"
                )
            # 桥接防护：任何 ``X.<危险属性>`` 直接拒绝（覆盖 pd.io.common.os.environ、
            # pandas.compat.os、np.core 等绕过 import 白名单的桥接链）。
            if node.attr in _FORBIDDEN_ATTR_NAMES:
                raise ValueError(
                    f"沙箱禁止访问危险属性 {node.attr!r}（防止桥接逃逸）。"
                    "请只做数据计算，不要尝试访问操作系统或模块内部。"
                )
            # 桥接根防护：``pd.<...>``、``np.<...>`` 这类以预导入模块为根的
            # 属性链，只放行白名单内的"安全顶层 API"，禁止穿透到内部子模块。
            root = _attribute_chain_root(node)
            if root in _BRIDGE_ROOT_NAMES:
                # 允许 df/pd 的常见用法：``pd.DataFrame(...)``、``pd.concat``、
                # ``np.mean``、``np.array`` 等 —— 这些属性名本身不在黑名单里，
                # 上面已挡住。这里再补一刀：一旦属性链长度 ≥ 3（如 pd.io.common），
                # 默认拒绝，避免通过 ``pd.X.Y.Z`` 深层穿透到内部模块。
                depth = 0
                cur: ast.AST = node
                while isinstance(cur, ast.Attribute):
                    depth += 1
                    cur = cur.value
                if depth >= 2 and root in _BRIDGE_ROOT_NAMES:
                    # 取出链上所有属性名，任何一个命中黑名单都拒绝。
                    names: list[str] = []
                    cursor: ast.AST = node
                    while isinstance(cursor, ast.Attribute):
                        names.append(cursor.attr)
                        cursor = cursor.value
                    # 中间任何一段命中黑名单（含 common/compat/io/core 等）都拒绝
                    if any(name in _FORBIDDEN_ATTR_NAMES for name in names):
                        raise ValueError(
                            f"沙箱禁止穿透 {root!r} 内部模块（属性链 {'.'.join(reversed(names))!r}）。"
                        )
        elif isinstance(node, ast.Name) and (
            node.id in _FORBIDDEN_NAMES or node.id.startswith("__")
        ):
            raise ValueError(f"沙箱禁止使用 {node.id!r}。请只做数据计算。")


def _summarize_result(value: Any) -> Any:
    """把 result 变量压缩成适合注入 LLM 上下文的预览结构。"""
    if value is None:
        return None
    if isinstance(value, pd.DataFrame):
        return {
            "type": "dataframe",
            "rows": len(value),
            "columns": [str(column) for column in value.columns],
            "records": value.head(_SANDBOX_RESULT_MAX_ROWS),
            "truncated": len(value) > _SANDBOX_RESULT_MAX_ROWS,
        }
    if isinstance(value, pd.Series):
        return {
            "type": "series",
            "length": len(value),
            "values": value.head(_SANDBOX_RESULT_MAX_ROWS),
            "truncated": len(value) > _SANDBOX_RESULT_MAX_ROWS,
        }
    if isinstance(value, np.ndarray):
        flat = value.tolist()
        if isinstance(flat, list) and len(flat) > _SANDBOX_RESULT_MAX_ROWS:
            return {
                "type": "ndarray",
                "length": len(flat),
                "values": flat[:_SANDBOX_RESULT_MAX_ROWS],
                "truncated": True,
            }
        return flat
    return value


def run_user_code(code: str, frame: pd.DataFrame) -> dict[str, Any]:
    """在当前进程执行已审查代码，返回 result 预览与 stdout。"""
    print_buffer: list[str] = []
    env: dict[str, Any] = {
        "__builtins__": _build_safe_builtins(print_buffer),
        "df": frame,
        "pd": pd,
        "np": np,
        "result": None,
    }
    exec(compile(code, "<sandbox>", "exec"), env)  # noqa: S102 — AST + 受限 builtins
    stdout_text = "".join(print_buffer)
    if len(stdout_text) > _SANDBOX_STDOUT_MAX_CHARS:
        stdout_text = (
            stdout_text[:_SANDBOX_STDOUT_MAX_CHARS]
            + f"\n…（print 输出过长，已截断至 {_SANDBOX_STDOUT_MAX_CHARS} 字符）"
        )
    return {
        "status": "ok",
        "result": _summarize_result(env.get("result")),
        "stdout": stdout_text or None,
        "note": "df 是主数据的副本，主数据未被修改。",
    }
