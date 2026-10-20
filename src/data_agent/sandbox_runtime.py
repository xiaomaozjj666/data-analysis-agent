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


def _validate_code(code: str) -> None:
    """AST 静态审查：语法、import 白名单、下划线属性/名称、危险内建。"""
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
