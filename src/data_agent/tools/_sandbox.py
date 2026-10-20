"""受限 Python 沙箱：``run_python_code`` 工具的实现。

覆盖预定义工具无法表达的长尾分析。用户代码在**独立子进程**里跑：超时或内存
超限时父进程可以杀掉子进程（线程版做不到，C 扩展死循环会把分析线程占死）。

护栏：
1. AST 静态审查（父进程，失败则不启动子进程）
2. 子进程内受限 builtins + import 白名单
3. 子进程超时 ``kill``
4. 子进程 RSS 超限 ``kill``
5. 输出截断
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

import data_agent as _data_agent_pkg
from data_agent.sandbox_runtime import (
    _safe_import,
    _summarize_result,
    _validate_code,
)
from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace

# 测试通过 monkeypatch 本模块上的这两个常量控制超时/内存。
_SANDBOX_TIMEOUT_SECONDS = 15.0
_SANDBOX_MEMORY_LIMIT_MB = 2048
_SANDBOX_MEMORY_LIMIT_BYTES = _SANDBOX_MEMORY_LIMIT_MB * 1024 * 1024
_SANDBOX_STDOUT_MAX_CHARS = 4_000
_SANDBOX_CODE_MAX_CHARS = 6_000

_CHILD_EXCEPTIONS: dict[str, type[BaseException]] = {
    "TimeoutError": TimeoutError,
    "MemoryError": MemoryError,
    "ValueError": ValueError,
    "KeyError": KeyError,
    "TypeError": TypeError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "ImportError": ImportError,
    "RuntimeError": RuntimeError,
    "ZeroDivisionError": ZeroDivisionError,
    "ArithmeticError": ArithmeticError,
}


def _kill_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.wait()


def _raise_child_error(payload: dict[str, Any]) -> None:
    name = str(payload.get("exc_type") or "RuntimeError")
    message = str(payload.get("message") or "沙箱子进程失败。")
    exc_type = _CHILD_EXCEPTIONS.get(name, RuntimeError)
    raise exc_type(message)


def _execute_in_subprocess(code: str, frame: Any) -> dict[str, Any]:
    """把代码和 DataFrame 副本交给子进程，超时或超内存则杀掉。"""
    with tempfile.TemporaryDirectory(prefix="data-agent-sandbox-") as tmp:
        tmp_path = Path(tmp)
        code_path = tmp_path / "code.py"
        frame_path = tmp_path / "frame.pkl"
        out_path = tmp_path / "out.json"
        code_path.write_text(code, encoding="utf-8")
        with frame_path.open("wb") as handle:
            pickle.dump(frame, handle, protocol=4)

        package_root = str(Path(_data_agent_pkg.__file__).resolve().parents[1])
        child_env = os.environ.copy()
        existing = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = os.pathsep.join(
            item for item in (package_root, existing) if item
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "data_agent.sandbox_worker",
                str(code_path),
                str(frame_path),
                str(out_path),
            ],
            cwd=tmp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        deadline = time.monotonic() + float(_SANDBOX_TIMEOUT_SECONDS)
        child_ps = None
        try:
            import psutil

            child_ps = psutil.Process(proc.pid)
        except Exception:
            child_ps = None

        timed_out = False
        memory_exceeded = False
        while proc.poll() is None:
            if child_ps is not None:
                try:
                    if child_ps.memory_info().rss > _SANDBOX_MEMORY_LIMIT_BYTES:
                        memory_exceeded = True
                        _kill_process(proc)
                        break
                except Exception:
                    child_ps = None
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process(proc)
                break
            time.sleep(0.05)

        if memory_exceeded:
            raise MemoryError(
                f"代码执行内存超过 {_SANDBOX_MEMORY_LIMIT_MB}MB 限制。"
                "请减少数据量或避免一次性创建大数组（如用分块处理替代全量复制）。"
            )
        if timed_out or proc.returncode is None:
            raise TimeoutError(
                f"代码执行超过 {_SANDBOX_TIMEOUT_SECONDS:g} 秒被熔断。"
                "请减少数据量或简化计算（如先聚合再循环）。"
            )
        if not out_path.is_file():
            stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            raise RuntimeError(f"沙箱子进程异常退出（code={proc.returncode}）。{stderr[-500:]}")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        if payload.get("status") == "error":
            _raise_child_error(payload)
        stdout_text = payload.get("stdout") or ""
        if isinstance(stdout_text, str) and len(stdout_text) > _SANDBOX_STDOUT_MAX_CHARS:
            payload["stdout"] = (
                stdout_text[:_SANDBOX_STDOUT_MAX_CHARS]
                + f"\n…（print 输出过长，已截断至 {_SANDBOX_STDOUT_MAX_CHARS} 字符）"
            )
        return payload


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
        are blocked; execution times out after 15 seconds and is killed in a child
        process. Changes to `df` never affect the active dataset — use clean_data /
        transform_data for real changes.
        """
        _validate_code(code)
        payload = _execute_in_subprocess(code, workspace.dataframe.copy())
        return json_text(payload)

    return run_python_code


__all__ = [
    "build_run_python_code",
    "_safe_import",
    "_summarize_result",
    "_validate_code",
]
