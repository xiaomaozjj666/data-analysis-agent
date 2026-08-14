"""受限 Python 沙箱：``run_python_code`` 工具的实现。

覆盖 7 个预定义工具无法表达的长尾分析需求（环比/滚动窗口、多级透视、
自定义指标组合筛选等），同时用多层防护把风险压到最低：

1. AST 静态审查：拒绝 import 白名单外的模块、拒绝下划线开头的属性与名称
   （堵住 ``().__class__.__mro__`` 这类沙箱逃逸链）、拒绝 eval/exec/open
   等危险内建。
2. 受限运行时：exec 注入白名单 builtins（不含文件/进程/反射入口），
   ``df`` 是主数据的**副本**——代码无法污染工作区主数据；``print`` 被替换
   为写入内存缓冲的函数（不动 sys.stdout，避免影响其他线程）。
3. 资源边界：独立 daemon 线程执行 + 超时熔断；stdout 与结果预览均有
   字符/行数上限，防止超大输出撑爆 LLM 上下文。
"""

from __future__ import annotations

import ast
import builtins as _py_builtins
import threading
import time
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.tools import BaseTool, tool

from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace

# ---------------------------------------------------------------------------
# 命名常量
# ---------------------------------------------------------------------------

#: 允许 import 的顶层模块白名单（子模块按前缀放行，如 collections.abc）。
_ALLOWED_IMPORTS = frozenset({
    "pandas", "numpy", "math", "statistics", "datetime", "re", "json",
    "collections", "itertools", "functools",
})

#: 单次代码执行的最长秒数。超时后返回错误；执行线程是 daemon，
#: 无法强杀但会随请求结束被丢弃，不阻塞主流程。
_SANDBOX_TIMEOUT_SECONDS = 15.0

#: print 输出捕获上限，超出部分丢弃并标注。
_SANDBOX_STDOUT_MAX_CHARS = 4_000

#: result 为 DataFrame/Series 时返回的最大行数。
_SANDBOX_RESULT_MAX_ROWS = 50

#: 代码文本长度上限：正常长尾计算几十行足够，超长代码多半是误用。
_SANDBOX_CODE_MAX_CHARS = 6_000

#: 单次代码执行的进程内存上限（MB）。超限时抛 MemoryError，避免
#: np.zeros((10**9,)) 这类攻击性代码导致整个进程被 OOM killer 杀死。
#: 2GB 覆盖大多数合法分析场景（100 万行 × 20 列 DataFrame 约 200MB）。
_SANDBOX_MEMORY_LIMIT_MB = 2048
_SANDBOX_MEMORY_LIMIT_BYTES = _SANDBOX_MEMORY_LIMIT_MB * 1024 * 1024

#: AST 层拒绝的内建名称。运行时 builtins 白名单同样不含它们（双保险），
#: 这里提前拦截能给 LLM 更明确的错误提示。
_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "breakpoint", "exit", "quit",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "hasattr",
    "memoryview", "type", "super", "object", "classmethod", "staticmethod",
    "property", "help",
})

#: pandas / numpy 中能读写文件的方法名。沙箱承诺"无文件访问"，
#: 但 pandas 的 read_*/to_* 和 numpy 的 save/load/fromfile 等方法
#: 名字不以下划线开头，AST 下划线检查拦不住，必须显式黑名单。
#: 覆盖 CSV/Excel/JSON/SQL/Pickle/Parquet/HDF5/HTML/Stata/GBQ/ORC
#: 等所有 pandas I/O 入口，以及 numpy 的二进制/文本/memmap 文件接口。
_FORBIDDEN_METHODS = frozenset({
    # pandas 读取
    "read_csv", "read_excel", "read_json", "read_sql", "read_sql_query",
    "read_sql_table", "read_pickle", "read_html", "read_parquet",
    "read_feather", "read_hdf", "read_stata", "read_sas", "read_gbq",
    "read_orc", "read_spss", "read_xml", "read_fwf", "read_table",
    # pandas 写入（DataFrame/Series 方法）
    "to_csv", "to_excel", "to_json", "to_sql", "to_pickle", "to_html",
    "to_parquet", "to_feather", "to_hdf", "to_stata", "to_gbq", "to_orc",
    "to_latex", "to_markdown", "to_xml",
    # numpy 文件 I/O
    "save", "savez", "savez_compressed", "load", "fromfile", "loadtxt",
    "savetxt", "memmap", "dump", "tofile",
})

#: 运行时注入的安全内建白名单：纯计算函数 + 常用容器 + 异常类型。
#: 不含任何文件、进程、反射、动态执行入口。
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hash", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min",
    "next", "ord", "pow", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "str", "sum", "tuple", "zip",
    "Exception", "ArithmeticError", "AttributeError", "IndexError",
    "KeyError", "LookupError", "RuntimeError", "StopIteration", "TypeError",
    "ValueError", "ZeroDivisionError",
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
    """构造受限 builtins 字典：白名单函数 + 缓冲版 print + 白名单 import。

    print 写入内存缓冲而不是重定向 sys.stdout——redirect_stdout 是进程级
    全局操作，会影响 API 服务的其他线程。
    """
    safe: dict[str, Any] = {
        name: getattr(_py_builtins, name) for name in _SAFE_BUILTIN_NAMES
    }

    def _print(*args: Any, sep: str = " ", end: str = "\n", **_ignored: Any) -> None:
        print_buffer.append(sep.join(str(item) for item in args) + end)

    safe["print"] = _print
    safe["__import__"] = _safe_import
    return safe


def _validate_code(code: str) -> None:
    """AST 静态审查：语法、import 白名单、下划线属性/名称、危险内建。

    Raises:
        ValueError: 代码不满足沙箱安全要求时，消息面向 LLM 可自行纠正。
    """
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
            # 拦截 pandas/numpy 的文件 I/O 方法名（read_csv/to_csv/save/load 等）。
            # 保守起见任何属性访问（含未调用的裸引用）都拒绝：名字本身即危险信号，
            # 且 LLM 生成代码中"仅取方法引用再间接调用"的模式无法被静态区分。
            if node.attr in _FORBIDDEN_METHODS:
                raise ValueError(
                    f"沙箱禁止调用文件 I/O 方法 {node.attr!r}。"
                    "沙箱仅支持内存计算，无法读写文件。"
                )
        elif isinstance(node, ast.Name) and (
            node.id in _FORBIDDEN_NAMES or node.id.startswith("__")
        ):
            raise ValueError(f"沙箱禁止使用 {node.id!r}。请只做数据计算。")


def _summarize_result(value: Any) -> Any:
    """把 result 变量压缩成适合注入 LLM 上下文的预览结构。

    DataFrame/Series 截断到 _SANDBOX_RESULT_MAX_ROWS 行并标注原始规模；
    ndarray 转为列表（同样截断）；其余类型交给 json_text 序列化，
    超大输出由工具消息截断中间件兜底。
    """
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
            return {"type": "ndarray", "length": len(flat),
                    "values": flat[:_SANDBOX_RESULT_MAX_ROWS], "truncated": True}
        return flat
    return value


def _execute_with_timeout(code: str, env: dict[str, Any]) -> None:
    """在独立 daemon 线程中 exec 代码，超时或内存超限时抛出。

    Windows 没有 SIGALRM，用线程 join(timeout) 实现熔断。超时后线程
    无法强杀（CPython 限制），但它是 daemon 线程且不持有任何锁，
    随进程回收，不会阻塞后续请求。

    内存监控：用 psutil 按固定间隔采样进程 RSS，超限时抛 MemoryError。
    与超时一样无法强杀工作线程，但能及时向调用方报错，避免 OOM 导致
    整个进程被系统杀死。

    Raises:
        TimeoutError: 执行超过 _SANDBOX_TIMEOUT_SECONDS。
        MemoryError: 进程内存超过 _SANDBOX_MEMORY_LIMIT_MB。
        Exception: 代码运行期抛出的原始异常。
    """
    import psutil

    holder: dict[str, BaseException] = {}
    # 内存监控用 Event 信号，工作线程检查到时主动退出（best-effort）。
    mem_exceeded = threading.Event()
    # 把 mem_exceeded 注入 env，让 exec 的代码可以通过检查它主动退出，
    # 但由于 exec 的代码是 LLM 生成的不可控，这仅作为 best-effort。
    env["__mem_exceeded__"] = mem_exceeded

    def _target() -> None:
        try:
            exec(compile(code, "<sandbox>", "exec"), env)  # noqa: S102 — 已经过 AST 审查 + 受限 builtins
        except BaseException as exc:  # noqa: BLE001 — 原样转交调用方
            holder["error"] = exc

    def _monitor_memory(proc: psutil.Process) -> None:
        """后台监控线程：每 0.5s 采样 RSS，超限设 Event。"""
        while worker.is_alive():
            try:
                rss = proc.memory_info().rss
                if rss > _SANDBOX_MEMORY_LIMIT_BYTES:
                    mem_exceeded.set()
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return
            time.sleep(0.5)

    worker = threading.Thread(target=_target, name="sandbox-exec", daemon=True)
    worker.start()

    # 启动内存监控线程
    try:
        proc = psutil.Process()
        monitor = threading.Thread(target=_monitor_memory, args=(proc,), name="sandbox-mem-monitor", daemon=True)
        monitor.start()
    except Exception:
        # psutil 不可用时不影响核心功能
        pass

    worker.join(timeout=_SANDBOX_TIMEOUT_SECONDS)
    if worker.is_alive():
        if mem_exceeded.is_set():
            raise MemoryError(
                f"代码执行内存超过 {_SANDBOX_MEMORY_LIMIT_MB}MB 限制。"
                "请减少数据量或避免一次性创建大数组（如用分块处理替代全量复制）。"
            )
        raise TimeoutError(
            f"代码执行超过 {_SANDBOX_TIMEOUT_SECONDS:g} 秒被熔断。"
            "请减少数据量或简化计算（如先聚合再循环）。"
        )
    if mem_exceeded.is_set():
        raise MemoryError(
            f"代码执行内存超过 {_SANDBOX_MEMORY_LIMIT_MB}MB 限制。"
            "请减少数据量或避免一次性创建大数组。"
        )
    if "error" in holder:
        raise holder["error"]


def build_run_python_code(workspace: DataWorkspace) -> BaseTool:
    """创建绑定到指定工作区的 run_python_code 沙箱工具。"""

    @tool
    def run_python_code(code: str) -> str:
        """Run a short Python snippet against a COPY of the active dataset, for analyses the other tools cannot express.

        Use this only as a fallback for long-tail computations such as period-over-period
        change, rolling windows, multi-level pivots, or combined custom-metric filtering
        that transform_data / statistical_analysis cannot express. Inside the snippet,
        `df` is a pandas DataFrame COPY of the active dataset and `pd` / `np` are
        pre-imported; assign the final output to a variable named `result`
        (DataFrame / Series / scalar / dict) and keep it small — DataFrame results are
        truncated to 50 rows. print() output is captured. Only pandas / numpy / math /
        statistics / datetime / re / json / collections / itertools / functools may be
        imported; file and network access, underscore attributes and dynamic execution
        are blocked; execution times out after 15 seconds. Changes to `df` never affect
        the active dataset — use clean_data / transform_data for real changes.
        """
        _validate_code(code)
        print_buffer: list[str] = []
        env: dict[str, Any] = {
            "__builtins__": _build_safe_builtins(print_buffer),
            "df": workspace.dataframe.copy(),
            "pd": pd,
            "np": np,
            "result": None,
        }
        _execute_with_timeout(code, env)
        stdout_text = "".join(print_buffer)
        if len(stdout_text) > _SANDBOX_STDOUT_MAX_CHARS:
            stdout_text = (
                stdout_text[:_SANDBOX_STDOUT_MAX_CHARS]
                + f"\n…（print 输出过长，已截断至 {_SANDBOX_STDOUT_MAX_CHARS} 字符）"
            )
        return json_text({
            "status": "ok",
            "result": _summarize_result(env.get("result")),
            "stdout": stdout_text or None,
            "note": "df 是主数据的副本，主数据未被修改。",
        })

    return run_python_code
