"""沙箱子进程入口的集成测试：经 ``python -m data_agent.sandbox_worker`` 真实拉起。

``run_user_code`` 的审查规则在 test_sandbox_source 等处已有覆盖；这里验证的是
**进程边界**——父进程写入 DataFrame 副本，子进程执行代码并把结果写成 JSON，
失败时返回结构化错误而不是让父进程面对裸异常。沙箱子进程刻意只加载
pandas/numpy，启动开销在秒级，三个用例的总耗时可接受。
"""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
from pathlib import Path

import pandas as pd

PYTHON = sys.executable


def _run_worker(tmp_path: Path, code: str, frame: pd.DataFrame | None) -> tuple[int, dict, str]:
    code_path = tmp_path / "code.py"
    frame_path = tmp_path / "frame.pkl"
    out_path = tmp_path / "out.json"
    code_path.write_text(code, encoding="utf-8")
    with frame_path.open("wb") as handle:
        pickle.dump(frame, handle)
    proc = subprocess.run(
        [PYTHON, "-m", "data_agent.sandbox_worker", str(code_path), str(frame_path), str(out_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    return proc.returncode, payload, proc.stderr


def test_worker_executes_code_and_writes_result(tmp_path):
    frame = pd.DataFrame({"a": [1, 2, 3]})
    code = "result = int(df['a'].sum())"
    returncode, payload, _stderr = _run_worker(tmp_path, code, frame)
    assert returncode == 0
    assert payload["status"] == "ok"
    assert payload["result"] == 6


def test_worker_reports_structured_error(tmp_path):
    frame = pd.DataFrame({"a": [1]})
    returncode, payload, _stderr = _run_worker(tmp_path, "result = df['missing']", frame)
    assert returncode == 1
    assert payload["status"] == "error"
    assert payload["exc_type"] == "KeyError"


def test_worker_usage_error_when_arguments_are_wrong(tmp_path):
    proc = subprocess.run(
        [PYTHON, "-m", "data_agent.sandbox_worker", "only-one-arg"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2
    assert "usage: sandbox_worker CODE_PATH FRAME_PATH OUT_PATH" in proc.stderr


# ---------------------------------------------------------------------------
# 进程内 main()：子进程方式不进覆盖率统计，这里直接调用以守住分支
# ---------------------------------------------------------------------------


def _write_inputs(tmp_path: Path, code: str, frame: pd.DataFrame) -> list[str]:
    code_path = tmp_path / "code.py"
    frame_path = tmp_path / "frame.pkl"
    out_path = tmp_path / "out.json"
    code_path.write_text(code, encoding="utf-8")
    with frame_path.open("wb") as handle:
        pickle.dump(frame, handle)
    return [str(code_path), str(frame_path), str(out_path)]


def test_worker_main_success_in_process(tmp_path):
    from data_agent.sandbox_worker import main

    argv = _write_inputs(tmp_path, "result = int(df['a'].sum())", pd.DataFrame({"a": [1, 2, 3]}))
    assert main(argv) == 0
    payload = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["result"] == 6


def test_worker_main_series_and_ndarray_summaries(tmp_path):
    """result 为 Series / ndarray 时走各自的摘要分支。"""
    from data_agent.sandbox_worker import main

    frame = pd.DataFrame({"a": [1, 2, 3]})
    argv = _write_inputs(tmp_path, "result = df['a']", frame)
    assert main(argv) == 0
    payload = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["result"]["type"] == "series"

    (tmp_path / "nd").mkdir()
    argv = _write_inputs(tmp_path / "nd", "import numpy as np\nresult = np.array([1, 2])", frame)
    assert main(argv) == 0
    payload = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    assert payload["status"] == "ok"


def test_worker_main_structured_error_in_process(tmp_path):
    from data_agent.sandbox_worker import main

    argv = _write_inputs(tmp_path, "result = df['missing']", pd.DataFrame({"a": [1]}))
    assert main(argv) == 1
    payload = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert payload["exc_type"] == "KeyError"
