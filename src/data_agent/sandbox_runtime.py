"""沙箱子进程与父进程共用的审查规则、受限 builtins 与结果压缩。

故意不依赖 langchain / FastAPI，这样 ``python -m data_agent.sandbox_worker``
启动时不会把整个 Web 栈拉进子进程。

白名单分两层：基础库（pandas/numpy/标准库）与可选统计库（scipy / scikit-learn /
statsmodels，命令 ``pip install "data-analysis-agent[stats]"``）。可选库未安装时
import 会转成可操作的中文安装提示；安装后也只保留内存计算能力——它们的
任意路径读写、网络抓取与另起进程入口在 ``_FORBIDDEN_*`` 集合里逐条拦截。
"""

from __future__ import annotations

import ast
import builtins as _py_builtins
import sys
from contextlib import suppress
from typing import Any

import numpy as np
import pandas as pd

# 基础模块白名单：纯内存计算与标准库工具。
_BASE_ALLOWED_IMPORTS = frozenset(
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

# 可选科学计算依赖：由 ``pip install "data-analysis-agent[stats]"`` 安装。
# 未安装时 import 由 ``_safe_import`` 翻译成安装提示；安装后同样只保留内存计算
# 能力（文件 I/O / 网络 / 并行子进程入口见下方 _FORBIDDEN_* 集合）。
_OPTIONAL_STATS_IMPORTS = frozenset({"scipy", "sklearn", "statsmodels"})
_STATS_EXTRAS_HINT = 'pip install "data-analysis-agent[stats]"'

_ALLOWED_IMPORTS = _BASE_ALLOWED_IMPORTS | _OPTIONAL_STATS_IMPORTS

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

# 统计库（scipy / sklearn / statsmodels）额外的高危属性名。白名单放宽到这三个库
# 后，真正要守的是它们的"任意路径读写 / 网络抓取 / 另起进程"入口；纯数学 API
# （scipy.stats、sklearn.linear_model、statsmodels.OLS…）不受影响。
_FORBIDDEN_STATS_ATTR_NAMES = frozenset(
    {
        # 任意路径文件读写：scipy.io / scipy.sparse / sklearn.datasets / statsmodels.iolib
        "loadmat",
        "savemat",
        "whosmat",
        "mminfo",
        "mmread",
        "mmwrite",
        "hb_read",
        "hb_write",
        "readsav",
        "netcdf_file",
        "netcdf_variable",
        "wavfile",
        "arff",
        "load_npz",
        "save_npz",
        "load_svmlight_file",
        "load_svmlight_files",
        "dump_svmlight_file",
        "load_files",
        "load_pickle",
        "save_pickle",
        # 网络抓取（其余 fetch_* 由前缀规则兜底）
        "fetch_openml",
        "fetch_file",
        "webuse",
        "get_rdataset",
        "get_rdatasets",
        "download_all",
        "clear_cache",
        "clear_data_home",
        # 打开浏览器 / 并行进程（joblib loky 会另起子进程，内存与超时护栏对
        # 孙进程不成立）
        "webdoc",
        "Parallel",
        "parallel_backend",
        "delayed",
        "joblib",
    }
)

# 统计库中已知带 I/O / 网络 / 进程能力的子模块（前缀匹配，含其后代模块）。
_FORBIDDEN_MODULE_PATHS = frozenset(
    {
        "scipy.io",  # loadmat/savemat/mmread/wavfile/netcdf：任意路径读写
        "scipy.datasets",  # 经 pooch 下载网络数据，并写入/清理缓存目录
        "statsmodels.iolib",  # pickle 与结果文件读写
        "statsmodels.tools.web",  # webdoc 直接调 webbrowser 打开浏览器
    }
)

# 模块路径里出现这些段即拒绝（换别名或写完整路径都无法绕过）。
_FORBIDDEN_MODULE_SEGMENTS = frozenset({"io", "iolib", "smpickle", "web"})

# 前缀规则：sklearn.datasets.fetch_* 以及未来新增的抓取函数一并拒绝。
_FORBIDDEN_ATTR_PREFIXES = ("fetch_",)

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


def _import_not_allowed_message(name: str) -> str:
    return (
        f"沙箱禁止导入模块 {name!r}。允许的模块：{', '.join(sorted(_ALLOWED_IMPORTS))}。"
        f"统计建模依赖 scipy / scikit-learn / statsmodels（{_STATS_EXTRAS_HINT}）；"
        "文件与网络访问一律不可用。"
    )


def _missing_optional_dependency_message(module: str) -> str:
    return (
        f"沙箱可选依赖 {module!r} 未安装，无法执行该计算。"
        f"请安装统计建模可选依赖：{_STATS_EXTRAS_HINT}"
        "（包含 scipy / scikit-learn / statsmodels），或改用 statistical_analysis "
        "等预定义工具。"
    )


def _forbidden_module_message(name: str, blocked: str) -> str:
    return (
        f"沙箱禁止导入 {name!r}：{blocked} 提供文件/网络 I/O 或进程控制入口。"
        "沙箱只允许内存计算。"
    )


def _forbidden_member_message(module: str, member: str) -> str:
    return (
        f"沙箱禁止导入 {module}.{member}：该入口用于文件/网络 I/O 或进程控制。"
        "沙箱只允许内存计算。"
    )


def _is_forbidden_member(name: str) -> bool:
    """成员名是否属于危险入口（用于 import 语句的成员与属性名审查）。"""
    return (
        name in _FORBIDDEN_NAMES
        or name in _FORBIDDEN_METHODS
        or name in _FORBIDDEN_ATTR_NAMES
        or name in _FORBIDDEN_STATS_ATTR_NAMES
        or name.startswith(_FORBIDDEN_ATTR_PREFIXES)
    )


def _forbidden_module_path(path: str) -> str | None:
    """返回命中的危险模块前缀（``scipy.io.loadmat`` → ``scipy.io``），未命中返回 None。"""
    if not path:
        return None
    for blocked in _FORBIDDEN_MODULE_PATHS:
        if path == blocked or path.startswith(blocked + "."):
            return blocked
    return None


def _check_import_name(name: str) -> None:
    """import 语句的模块名校验：顶层白名单 + 危险子模块段 + 危险模块前缀。"""
    if name.split(".")[0] not in _ALLOWED_IMPORTS:
        raise ValueError(_import_not_allowed_message(name))
    blocked = _forbidden_module_path(name)
    if blocked is not None:
        raise ValueError(_forbidden_module_message(name, blocked))
    for segment in name.split("."):
        if segment in _FORBIDDEN_MODULE_SEGMENTS:
            raise ValueError(
                f"沙箱禁止导入 {name!r}（{segment!r} 是文件 I/O 入口）。沙箱只允许内存计算。"
            )


def _check_import_member(module: str, member: str) -> None:
    """``from module import member`` 的成员名校验（拦 fetch_*/io/loadmat/Parallel 等）。"""
    path = f"{module}.{member}" if module else member
    blocked = _forbidden_module_path(path)
    if blocked is not None:
        raise ValueError(_forbidden_module_message(path, blocked))
    for segment in path.split("."):
        if segment in _FORBIDDEN_MODULE_SEGMENTS:
            raise ValueError(
                f"沙箱禁止导入 {path!r}（{segment!r} 是文件 I/O 入口）。沙箱只允许内存计算。"
            )
    if _is_forbidden_member(member):
        raise ValueError(_forbidden_member_message(module, member))


def _pin_joblib_single_process() -> None:
    """把 joblib 默认后端钉在 threading：沙箱只允许单进程计算。

    sklearn 的 ``n_jobs>1`` / ``cross_val_score`` / ``GridSearchCV`` 走 joblib，
    默认 loky 后端会另起子进程：子进程内存不计入直接子进程的 RSS 监控，超时收割
    也只看直接子进程。改写默认后端后并行仍在同一进程内（线程），RSS 与超时护栏
    继续生效。joblib 未加载或版本改结构时静默跳过，不影响统计库导入。
    """
    parallel = sys.modules.get("joblib.parallel")
    if parallel is None:
        return
    with suppress(Exception):
        parallel.DEFAULT_BACKEND = "threading"  # type: ignore[attr-defined]
    with suppress(Exception):
        parallel.DEFAULT_PROCESS_BACKEND = "threading"  # type: ignore[attr-defined]


def _stats_not_installed_hint(exc: ModuleNotFoundError, name: str) -> ImportError | None:
    """把"可选统计库整体未安装"的 ModuleNotFoundError 翻译成可操作的 ImportError。

    只有顶层模块缺失（``exc.name`` 不含点且与请求的顶层一致）才改写：
    已安装包内部子模块损坏、或其第三方依赖缺失时保持原异常，避免把
    "装坏了" 误报成 "没装"。
    """
    missing = exc.name or ""
    if "." in missing or missing != name.split(".")[0] or missing not in _OPTIONAL_STATS_IMPORTS:
        return None
    return ImportError(_missing_optional_dependency_message(missing))


def _safe_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """白名单版 __import__：只放行 _ALLOWED_IMPORTS 中的顶层模块。

    这是 AST 审查之外的第二道闸门（子进程内生效）：同样拦统计库的 I/O / 网络 /
    进程子模块，并把 "可选依赖未安装" 翻译成可操作的中文安装提示。
    """
    if level != 0 or name.split(".")[0] not in _ALLOWED_IMPORTS:
        raise ImportError(_import_not_allowed_message(name))
    blocked = _forbidden_module_path(name)
    if blocked is not None:
        raise ImportError(_forbidden_module_message(name, blocked))
    for segment in name.split("."):
        if segment in _FORBIDDEN_MODULE_SEGMENTS:
            raise ImportError(
                f"沙箱禁止导入 {name!r}（{segment!r} 是文件 I/O 入口）。沙箱只允许内存计算。"
            )
    for member in fromlist or ():
        if member == "*":
            raise ImportError(
                "沙箱禁止使用 * 导入（星号导入会绕过文件/网络入口审查）："
                "请写明要导入的名字。"
            )
        if _is_forbidden_member(member):
            raise ImportError(_forbidden_member_message(name, member))
    try:
        module = _py_builtins.__import__(name, globals, locals, fromlist, level)
    except ModuleNotFoundError as exc:
        hinted = _stats_not_installed_hint(exc, name)
        if hinted is not None:
            raise hinted from exc
        raise
    if name.split(".")[0] in _OPTIONAL_STATS_IMPORTS:
        _pin_joblib_single_process()
    return module


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


def _attribute_chain_parts(node: ast.AST) -> list[str]:
    """把属性链拆成 ``[根, 属性…]``；根不是 Name 时返回空列表。"""
    attrs: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        return [cur.id, *reversed(attrs)]
    return []


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """收集本地名 → 模块路径的映射（``import scipy as sp`` → ``sp: scipy``）。

    用途：把 ``sp.datasets.face`` 还原成 ``scipy.datasets.face``，让模块前缀
    拦截换个别名也绕不过去。只做静态、保守的解析——根名没有 import 来源时
    不解析（例如用户变量 ``datasets = {...}``）。
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases.setdefault(alias.asname, alias.name)
                else:
                    # ``import scipy.stats`` 只把顶层名 ``scipy`` 绑进命名空间。
                    top = alias.name.split(".")[0]
                    aliases.setdefault(top, top)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    aliases.setdefault(alias.asname or alias.name, f"{node.module}.{alias.name}")
    return aliases


def _resolved_module_path(node: ast.AST, aliases: dict[str, str]) -> str:
    """把属性链还原成模块路径（无法静态判定时返回空串）。"""
    parts = _attribute_chain_parts(node)
    if not parts:
        return ""
    root = aliases.get(parts[0])
    if root is None:
        return ""
    return ".".join([root, *parts[1:]])


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
    aliases = _collect_import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import_name(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                raise ValueError("沙箱禁止相对导入（from . import …）。")
            module = node.module or ""
            _check_import_name(module)
            for alias in node.names:
                if alias.name == "*":
                    raise ValueError(
                        "沙箱禁止使用 * 导入：星号导入会把库内部子模块（如 scipy.io）"
                        "直接绑成变量，绕过文件/网络入口审查。请写明要导入的名字。"
                    )
                _check_import_member(module, alias.name)
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
            # 统计库 I/O / 网络 / 进程入口：属性名层面直接拒绝（fetch_* 走前缀）。
            if node.attr in _FORBIDDEN_STATS_ATTR_NAMES or node.attr.startswith(
                _FORBIDDEN_ATTR_PREFIXES
            ):
                raise ValueError(
                    f"沙箱禁止访问危险属性 {node.attr!r}（统计库的文件/网络/进程入口）。"
                    "沙箱只允许内存计算。"
                )
            # 别名还原后的模块路径拦截：``sp.datasets.face`` → scipy.datasets.face，
            # 换别名或写完整的 ``import scipy.datasets`` 都拦得住。
            resolved = _resolved_module_path(node, aliases)
            blocked = _forbidden_module_path(resolved)
            if blocked is not None:
                raise ValueError(
                    f"沙箱禁止访问 {blocked!r} 内部模块（属性链 {resolved!r}）："
                    "该模块提供文件/网络 I/O 或进程控制入口。"
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
    """在当前进程执行已审查代码，返回 result 预览与 stdout。

    ``scipy`` / ``sklearn`` / ``statsmodels`` 不预导入——用户代码里写 import 时才
    加载（未安装时由 ``_safe_import`` 给出安装提示），不写就不付导入成本。
    """
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
