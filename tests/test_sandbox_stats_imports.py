"""沙箱可选统计依赖（scipy / scikit-learn / statsmodels）的导入与边界回归测试。

覆盖三件事：

1. 白名单加入 scipy / sklearn / statsmodels（含子模块），基础库保持不变；
2. 三个库的任意路径 I/O、网络抓取与并行进程入口仍被 AST 与运行时拦截——
   白名单扩展没有放松 ``os`` / ``subprocess`` / ``socket`` / ``open(`` 等边界；
3. 可选依赖未安装时给出可操作的安装提示；本机已安装时真实导入并跑一个最小
   scipy / sklearn 计算（statsmodels 未安装时对应用例 skip）。
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

# 让测试可以独立 import data_agent（不依赖 conftest 的 sys.path 注入顺序）。
PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from data_agent.sandbox_runtime import (  # noqa: E402
    _ALLOWED_IMPORTS,
    _BASE_ALLOWED_IMPORTS,
    _OPTIONAL_STATS_IMPORTS,
    _safe_import,
    _validate_code,
    run_user_code,
)
from data_agent.tools._sandbox import build_run_python_code  # noqa: E402


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # 半初始化/命名冲突时按未安装处理
        return False


STATS_INSTALLED = {module: _installed(module) for module in sorted(_OPTIONAL_STATS_IMPORTS)}
requires_scipy = pytest.mark.skipif(not STATS_INSTALLED["scipy"], reason="未安装可选依赖 scipy")
requires_sklearn = pytest.mark.skipif(
    not STATS_INSTALLED["sklearn"], reason="未安装可选依赖 scikit-learn"
)
requires_statsmodels = pytest.mark.skipif(
    not STATS_INSTALLED["statsmodels"], reason="未安装可选依赖 statsmodels"
)


@pytest.fixture(autouse=True)
def _restore_joblib_backend():
    """沙箱钩子会把 joblib 默认后端钉成 threading；用例结束后还原，避免串味。"""
    try:
        import joblib.parallel as parallel
    except Exception:  # noqa: BLE001 — joblib 缺失时无需还原
        yield
        return
    original = (parallel.DEFAULT_BACKEND, parallel.DEFAULT_PROCESS_BACKEND)
    try:
        yield
    finally:
        parallel.DEFAULT_BACKEND, parallel.DEFAULT_PROCESS_BACKEND = original


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------


def test_whitelist_adds_optional_stats_and_keeps_base() -> None:
    assert _OPTIONAL_STATS_IMPORTS == {"scipy", "sklearn", "statsmodels"}
    assert {"scipy", "sklearn", "statsmodels"} <= _ALLOWED_IMPORTS
    # 原有基础库一个都不能少。
    assert _BASE_ALLOWED_IMPORTS <= _ALLOWED_IMPORTS
    assert _BASE_ALLOWED_IMPORTS == {
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


@pytest.mark.parametrize(
    "code",
    [
        "import scipy.stats as st\nresult = st.norm.cdf(0)",
        "from scipy import stats\nresult = stats.linregress([1, 2, 3], [2, 4, 7])",
        "from scipy import optimize\nresult = optimize.minimize_scalar(lambda x: (x - 1) ** 2)",
        "import sklearn.linear_model as lm\nresult = lm.LinearRegression()",
        "from sklearn.linear_model import LinearRegression\nresult = LinearRegression()",
        "from sklearn.ensemble import RandomForestClassifier",
        "from sklearn.metrics import r2_score",
        "from sklearn.datasets import make_classification, load_iris",
        "import statsmodels.api as sm\nresult = sm.OLS",
        "from statsmodels.formula.api import ols",
    ],
)
def test_validate_code_allows_stats_usage(code: str) -> None:
    _validate_code(code)  # 不应抛异常


# ---------------------------------------------------------------------------
# 统计库的 I/O / 网络 / 进程入口仍被拦截
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        # scipy.io：loadmat / savemat / mmread / wavfile / netcdf 等任意路径读写
        "import scipy.io",
        "from scipy import io",
        "import scipy.io.wavfile as wf",
        "from scipy.io import loadmat",
        # scipy.datasets：pooch 网络下载 + 缓存目录写入/清理
        "import scipy.datasets",
        "from scipy import datasets",
        "import scipy as sp\nresult = sp.datasets.face()",
        # scipy.sparse 的 npz 读写
        "import scipy.sparse as ss\nresult = ss.save_npz('x.npz', None)",
        # statsmodels.iolib / tools.web
        "import statsmodels.iolib as iolib",
        "from statsmodels import iolib",
        "from statsmodels.tools import web",
        "from statsmodels.iolib import load_pickle",
        # statsmodels 数据集抓取
        "from statsmodels.datasets import webuse",
        "from statsmodels.datasets import get_rdataset",
        "import statsmodels.api as sm\nresult = sm.webuse('x')",
        # sklearn 网络抓取 / 任意路径读取 / 缓存清理
        "from sklearn.datasets import fetch_openml",
        "from sklearn.datasets import fetch_file",
        "import sklearn.datasets as ds\nresult = ds.fetch_openml('x')",
        "from sklearn.datasets import load_svmlight_file",
        "from sklearn.datasets import load_files",
        "from sklearn.datasets import dump_svmlight_file",
        "from sklearn.datasets import clear_data_home",
        # joblib 并行进程入口（n_jobs 之外的显式入口）
        "from sklearn.utils.parallel import Parallel",
        "from sklearn.utils.parallel import delayed",
        "from sklearn.utils import parallel_backend",
        # 星号导入会把库内部子模块直接绑成变量，绕过 I/O 入口审查
        "from scipy import *",
        "from sklearn import *",
    ],
)
def test_validate_code_rejects_stats_io_entrypoints(code: str) -> None:
    with pytest.raises(ValueError):
        _validate_code(code)


@pytest.mark.parametrize(
    "code",
    [
        # 白名单扩展不能放松既有边界
        "import os\nresult = os.environ",
        "from os import environ",
        "import subprocess\nresult = subprocess.run(['ls'])",
        "from subprocess import run",
        "import socket",
        "from socket import socket",
        "import ctypes",
        "import importlib",
        "result = open('x.txt')",
        "result = eval('1')",
        "result = exec('1')",
        "result = getattr(df, 'shape')",
        "result = df.__class__",
        "result = pd.read_csv('x.csv')",
        "result = df.to_csv('x.csv')",
        "result = np.load('x.npy')",
        # 既有桥接链防护（pd.io.common.os / np.core）不因新增库而失效
        "result = pd.io.common.os.environ",
        "result = pd.compat.os",
        "result = np.core",
        # ImportFrom 成员也不能是 I/O 入口
        "from pandas import read_csv",
        "from numpy import save",
    ],
)
def test_validate_code_still_blocks_core_escapes(code: str) -> None:
    with pytest.raises(ValueError):
        _validate_code(code)


# ---------------------------------------------------------------------------
# 子进程内的运行时闸门（AST 之外的第二道）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["os", "subprocess", "socket", "scipy.io", "scipy.datasets", "statsmodels.iolib"],
)
def test_safe_import_blocks_forbidden_modules(name: str) -> None:
    with pytest.raises(ImportError, match="禁止导入"):
        _safe_import(name)


def test_safe_import_blocks_forbidden_members() -> None:
    with pytest.raises(ImportError, match="禁止导入"):
        _safe_import("sklearn.datasets", fromlist=("fetch_openml",))
    with pytest.raises(ImportError, match="禁止导入"):
        _safe_import("scipy", fromlist=("io",))
    with pytest.raises(ImportError, match="星号"):
        _safe_import("scipy", fromlist=("*",))


# ---------------------------------------------------------------------------
# 未安装时的可行动提示
# ---------------------------------------------------------------------------


def test_missing_optional_dependency_message_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals=None,  # noqa: A002 — 与 __import__ 签名一致
        locals=None,  # noqa: A002
        fromlist=(),
        level: int = 0,
    ):
        if name.split(".")[0] == "statsmodels":
            raise ModuleNotFoundError("No module named 'statsmodels'", name="statsmodels")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError) as excinfo:
        _safe_import("statsmodels.api")
    message = str(excinfo.value)
    assert "statsmodels" in message
    assert 'pip install "data-analysis-agent[stats]"' in message
    assert "statistical_analysis" in message


def test_broken_submodule_is_not_misreported_as_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已安装包的子模块损坏时保留原始 ModuleNotFoundError，不误报"没装"。"""
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals=None,  # noqa: A002
        locals=None,  # noqa: A002
        fromlist=(),
        level: int = 0,
    ):
        if name.split(".")[0] == "scipy":
            raise ModuleNotFoundError(
                "No module named 'scipy.special._ufuncs'", name="scipy.special._ufuncs"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError):
        _safe_import("scipy.stats")


@pytest.mark.skipif(STATS_INSTALLED["statsmodels"], reason="本机已安装 statsmodels")
def test_run_user_code_missing_statsmodels_gives_install_hint() -> None:
    with pytest.raises(ImportError) as excinfo:
        run_user_code("import statsmodels.api as sm\nresult = None", pd.DataFrame({"a": [1, 2]}))
    assert "[stats]" in str(excinfo.value)


@pytest.mark.skipif(STATS_INSTALLED["statsmodels"], reason="本机已安装 statsmodels")
def test_tool_path_reports_missing_statsmodels_hint(workspace) -> None:
    """经真实子进程执行时，未安装提示仍能回到父进程（ImportError 可被翻译）。"""
    tool = build_run_python_code(workspace)
    with pytest.raises(ImportError) as excinfo:
        tool.invoke({"code": "import statsmodels.api as sm\nresult = None"})
    assert "[stats]" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 已安装时真实导入 + 最小计算
# ---------------------------------------------------------------------------


@requires_scipy
def test_run_user_code_scipy_minimal_computation() -> None:
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [2.0, 4.0, 6.5]})
    payload = run_user_code(
        "import scipy.stats as st\n"
        "fit = st.linregress(df['x'], df['y'])\n"
        "result = round(float(fit.slope), 6)\n",
        frame,
    )
    assert payload["status"] == "ok"
    # 斜率 = Sxy / Sxx = 4.5 / 2.0
    assert payload["result"] == pytest.approx(2.25)


@requires_sklearn
def test_run_user_code_sklearn_minimal_computation() -> None:
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [2.0, 4.0, 6.5]})
    payload = run_user_code(
        "from sklearn.linear_model import LinearRegression\n"
        "model = LinearRegression().fit(df[['x']], df['y'])\n"
        "result = round(float(model.coef_[0]), 6)\n",
        frame,
    )
    assert payload["status"] == "ok"
    assert payload["result"] == pytest.approx(2.25)


@requires_sklearn
def test_sklearn_import_pins_joblib_to_threads() -> None:
    """n_jobs>1 不得另起子进程：默认后端在导入 sklearn 后被钉成 threading。"""
    parallel = pytest.importorskip("joblib.parallel")
    parallel.DEFAULT_BACKEND = "loky"
    parallel.DEFAULT_PROCESS_BACKEND = "loky"
    _safe_import("sklearn")
    assert parallel.DEFAULT_BACKEND == "threading"
    assert parallel.DEFAULT_PROCESS_BACKEND == "threading"


@requires_statsmodels
def test_run_user_code_statsmodels_minimal_computation() -> None:
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0], "y": [2.0, 4.1, 5.9, 8.2]})
    payload = run_user_code(
        "import statsmodels.api as sm\n"
        "model = sm.OLS(df['y'], sm.add_constant(df['x'])).fit()\n"
        "result = round(float(model.params['x']), 3)\n",
        frame,
    )
    assert payload["status"] == "ok"
    # OLS 斜率 = 10.2 / 5.0
    assert payload["result"] == pytest.approx(2.04, abs=0.01)


@requires_scipy
def test_tool_path_imports_scipy_in_child_process(workspace) -> None:
    """完整链路：父进程 AST 审查 → 子进程受限 builtins → scipy 计算 → JSON 结果。"""
    tool = build_run_python_code(workspace)
    payload = json.loads(
        tool.invoke(
            {
                "code": (
                    "import scipy.stats as st\n"
                    "result = round(float(st.norm.cdf(0)), 6)"
                )
            }
        )
    )
    assert payload["status"] == "ok"
    assert payload["result"] == pytest.approx(0.5)
