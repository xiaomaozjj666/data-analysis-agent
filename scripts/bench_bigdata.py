"""大数据路径基准：工作区加载 / 相关分析 / 图表抽样 / 密度视图 / 核验复算。

目的
----
项目此前针对大数据做过若干优化（相关分析 p 值由 t 分布导出、常数列 O(n)
判定、图表嵌入抽样、WebGL 阈值、密度视图、SQLite 大表限制载入行数）。
本脚本把"优化后到底多快、峰值内存多少"变成可复跑的实测数字，而不是口头
声明。输出为单个 JSON，字段含义见各 ``_bench_*`` 函数。

用法::

    .venv\\Scripts\\python.exe scripts/bench_bigdata.py
    .venv\\Scripts\\python.exe scripts/bench_bigdata.py --rows 100000 --repeat 5

默认生成 200,000 行 × 7 列数值数据；核验复算单独生成 1,000,000 行 CSV。
所有临时文件都写在系统临时目录，脚本结束时自动删除。退出码：0 = 全部
基准项完成；1 = 至少一项失败（失败原因写在该项的 ``error`` 字段）。
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from data_agent.audit_schema import parse_schema, read_metrics  # noqa: E402
from data_agent.chart_sampling import sample_plotly_figure_for_embed  # noqa: E402
from data_agent.density import (  # noqa: E402
    DENSITY_MIN_POINTS,
    build_density_view,
    should_use_density,
)
from data_agent.tools._plotly_render import (  # noqa: E402
    _PLOTLY_WEBGL_THRESHOLD,
    _plotly_webgl_if_large,
)
from data_agent.tools._stats import run_statistical_analysis  # noqa: E402
from data_agent.workspace import DataWorkspace  # noqa: E402

#: 生成数据时的列数（含目标列），与工程笔记中 200k × 7 的记录保持一致。
DEFAULT_COLUMNS = 7


def _generate_numeric_csv(path: Path, rows: int, seed: int) -> None:
    """生成带相关性的 7 列数值 CSV（前 6 列正态，target 由它们线性组合）。"""
    rng = np.random.default_rng(seed)
    data: dict[str, Any] = {
        f"metric_{i + 1}": rng.normal(0.0, 1.0, rows) for i in range(DEFAULT_COLUMNS - 1)
    }
    target = 0.4 * data["metric_1"] - 0.3 * data["metric_2"] + 0.2 * data["metric_3"]
    target = target + rng.normal(0.0, 0.5, rows)
    data["target"] = target
    frame = pd.DataFrame(data)
    frame.to_csv(path, index=False)
    del frame, data


def _generate_audit_csv(path: Path, rows: int, seed: int) -> None:
    """流式生成核验用 CSV，避免为 100 万行同时在内存里堆一份 DataFrame。"""
    rng = np.random.default_rng(seed)
    regions = ["东区", "西区", "南区", "北区"]
    dates = ["2026-01-05", "2026-02-11", "2026-03-20"]
    chunk = 50_000
    written = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["order_id", "region", "amount", "event_date"])
        while written < rows:
            size = min(chunk, rows - written)
            amounts = rng.uniform(1.0, 1000.0, size)
            region_pick = rng.integers(0, len(regions), size)
            date_pick = rng.integers(0, len(dates), size)
            for offset in range(size):
                index = written + offset
                writer.writerow(
                    [
                        f"O{index:08d}",
                        regions[int(region_pick[offset])],
                        f"{amounts[offset]:.2f}",
                        dates[int(date_pick[offset])],
                    ]
                )
            written += size


def _measure(
    func: Callable[[], Any], repeat: int, warmup: int = 1
) -> tuple[dict[str, Any], Any]:
    """跑 warmup 次预热，再计时 repeat 次，返回 (耗时统计, 最后一次返回值)。"""
    result: Any = None
    for _ in range(warmup):
        result = func()
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        result = func()
        samples.append(time.perf_counter() - started)
    return (
        {
            "seconds_median": round(statistics.median(samples), 6),
            "seconds_min": round(min(samples), 6),
            "seconds_max": round(max(samples), 6),
            "samples": [round(value, 6) for value in samples],
        },
        result,
    )


def _peak_extra_mb() -> float:
    """自 tracemalloc 启动（或上次 reset_peak）以来的额外峰值内存。"""
    _, peak = tracemalloc.get_traced_memory()
    return round(peak / 1024**2, 2)


def _bench_workspace(csv_path: Path, root: Path, repeat: int) -> dict[str, Any]:
    """workspace.load（含首次 profile）与冷/热 profile 的耗时和峰值内存。"""
    workspace = DataWorkspace(root, session_id="bench")

    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    profile = workspace.load(csv_path)
    load_seconds = time.perf_counter() - started
    load_peak = _peak_extra_mb()
    tracemalloc.stop()

    # 让 setter 清空 profile 缓存，测一次"冷" profile（nunique/duplicated 全表扫描）。
    workspace.dataframe = workspace.dataframe
    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    recalculated = workspace.profile(sample_rows=5)
    profile_cold_seconds = time.perf_counter() - started
    profile_cold_peak = _peak_extra_mb()
    tracemalloc.stop()

    # 不同 sample_rows 复用全量统计缓存，只重建样例行。
    profile_warm_seconds, _ = _measure(lambda: workspace.profile(sample_rows=8), repeat, warmup=1)

    return {
        # 供后续基准项复用同一个已加载的工作区，输出前由 main 弹出。
        "_workspace": workspace,
        "rows": profile["rows"],
        "columns": profile["columns"],
        "source_memory_mb": profile["memory_mb"],
        "load_seconds": round(load_seconds, 6),
        "load_peak_mb": load_peak,
        "profile_cold_seconds": round(profile_cold_seconds, 6),
        "profile_cold_peak_mb": profile_cold_peak,
        "profile_warm": profile_warm_seconds,
        "profile_duplicate_rows": recalculated["duplicate_rows"],
    }


def _bench_correlation(df: pd.DataFrame, repeat: int) -> dict[str, Any]:
    """相关分析：7 个数值列两两组合，p 值走 t 分布导出路径。"""
    stats, result = _measure(lambda: run_statistical_analysis(df, "correlation"), repeat)
    columns = list(result["columns"])
    return {
        "columns": len(columns),
        "pairs": len(columns) * (len(columns) - 1) // 2,
        **stats,
    }


def _bench_chart_embed(df: pd.DataFrame, repeat: int) -> dict[str, Any]:
    """图表嵌入抽样：散点（等距）与折线（LTTB）各测一次。"""
    import plotly.graph_objects as go

    output: dict[str, Any] = {}
    specs = (
        ("scatter", go.Scattergl(x=df["metric_1"], y=df["metric_2"], mode="markers")),
        ("line", go.Scatter(x=df["metric_1"], y=df["metric_2"], mode="lines")),
    )
    for name, trace in specs:
        figure = go.Figure(data=[trace])
        payload = json.loads(figure.to_json())
        stats, result = _measure(
            lambda payload=payload: sample_plotly_figure_for_embed(payload), repeat
        )
        _, before, after = result
        output[name] = {
            "original_points": before,
            "embedded_points": after,
            "payload_json_mb": round(len(figure.to_json()) / 1024**2, 3),
            **stats,
        }
    return output


def _bench_density(df: pd.DataFrame, repeat: int) -> dict[str, Any]:
    """密度视图的阈值判定与 200k 点聚合耗时。"""
    x = df["metric_1"].to_numpy()
    y = df["metric_2"].to_numpy()
    stats, view = _measure(lambda: build_density_view(x, y), repeat)
    if view is None:
        raise RuntimeError("build_density_view 对 200k 点返回 None")
    return {
        "threshold": DENSITY_MIN_POINTS,
        "should_use_at_threshold": should_use_density(DENSITY_MIN_POINTS),
        "should_use_below_threshold": should_use_density(DENSITY_MIN_POINTS - 1),
        "should_use_at_rows": should_use_density(len(df)),
        "bins": view.bin_label,
        "panels": len(view.panels),
        "occupied_cells": view.overall.occupied_cells if view.overall else 0,
        "max_count": view.max_count,
        **stats,
    }


def _bench_webgl(df: pd.DataFrame, repeat: int) -> dict[str, Any]:
    """WebGL 阈值：>10k 行的散点/折线切换为 scattergl，边界值保持原样。

    ``--rows`` 小于阈值时把数据平铺到"阈值 + 1"行，保证边界判定始终被真正
    执行，而不是只因为数据太小而全落回 ``scatter``。
    """
    import plotly.graph_objects as go

    threshold = _PLOTLY_WEBGL_THRESHOLD
    if len(df) > threshold:
        probe = df
    else:
        copies = (threshold + 1 + len(df) - 1) // len(df)
        probe = pd.concat([df] * copies, ignore_index=True).iloc[: threshold + 1]

    def trace_type(frame: pd.DataFrame, chart_type: str) -> str:
        figure = go.Figure(data=[go.Scatter(x=frame["metric_1"], y=frame["metric_2"])])
        switched = _plotly_webgl_if_large(figure, frame, chart_type)
        return str(switched.data[0].type)

    stats, _ = _measure(lambda: trace_type(probe, "scatter"), repeat)
    return {
        "threshold": threshold,
        "probe_rows": len(probe),
        "above_threshold_type": trace_type(probe, "scatter"),
        "at_threshold_type": trace_type(probe.iloc[:threshold], "scatter"),
        "below_threshold_type": trace_type(probe.iloc[: threshold - 1], "scatter"),
        "line_above_threshold_type": trace_type(probe, "line"),
        **stats,
    }


def _bench_audit(csv_path: Path, rows: int) -> dict[str, Any]:
    """核验复算：逐行 csv.DictReader + Decimal，无 DataFrame 中间层。"""
    specs = parse_schema(
        {
            "metrics": [
                {"name": "金额合计", "column": "amount", "aggregation": "sum", "unit": "元"},
                {"name": "订单数", "column": "order_id", "aggregation": "count", "unit": "条"},
                {"name": "区域数", "column": "region", "aggregation": "count_distinct", "unit": "个"},
            ]
        }
    )
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    read = read_metrics(csv_path, specs)
    elapsed = time.perf_counter() - started
    peak = _peak_extra_mb()
    tracemalloc.stop()
    return {
        "rows": rows,
        "file_mb": round(csv_path.stat().st_size / 1024**2, 2),
        "metrics": read.metrics,
        "evidence_rows": read.evidence["rows"],
        "evidence_columns": read.evidence["columns"],
        "seconds": round(elapsed, 6),
        "peak_mb": peak,
    }


def _run_section(name: str, func: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "name": name, **func()}
    except Exception as exc:  # noqa: BLE001 - 基准脚本要把单项失败变成可读字段
        return {"ok": False, "name": name, "error": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="大数据路径实测基准（输出 JSON）。")
    parser.add_argument("--rows", type=int, default=200_000, help="工作区/分析数据行数")
    parser.add_argument(
        "--audit-rows", type=int, default=1_000_000, help="核验复算 CSV 行数"
    )
    parser.add_argument("--repeat", type=int, default=3, help="可重复操作的计时轮数")
    parser.add_argument("--seed", type=int, default=20261119, help="生成数据的随机种子")
    args = parser.parse_args(argv)
    if args.rows < 100 or args.audit_rows < 100 or args.repeat < 1:
        parser.error("--rows/--audit-rows 至少 100，--repeat 至少 1")

    report: dict[str, Any] = {
        "params": {
            "rows": args.rows,
            "columns": DEFAULT_COLUMNS,
            "audit_rows": args.audit_rows,
            "repeat": args.repeat,
            "seed": args.seed,
        },
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "plotly": __import__("plotly").__version__,
        },
        "sections": {},
    }

    with tempfile.TemporaryDirectory(prefix="bench-bigdata-") as temp:
        temp_dir = Path(temp)
        data_csv = temp_dir / "bigdata.csv"
        audit_csv = temp_dir / "audit.csv"

        _generate_numeric_csv(data_csv, args.rows, args.seed)
        report["sections"]["dataset_file_mb"] = round(data_csv.stat().st_size / 1024**2, 2)

        workspace_section = _run_section(
            "workspace",
            lambda: _bench_workspace(data_csv, temp_dir / "workspace", args.repeat),
        )
        workspace = workspace_section.pop("_workspace", None)
        report["sections"]["workspace"] = workspace_section
        df = None
        if workspace_section["ok"] and workspace is not None:
            df = workspace.dataframe
            report["sections"]["correlation"] = _run_section(
                "correlation", lambda frame=df: _bench_correlation(frame, args.repeat)
            )
            report["sections"]["chart_embed"] = _run_section(
                "chart_embed", lambda frame=df: _bench_chart_embed(frame, args.repeat)
            )
            report["sections"]["density"] = _run_section(
                "density", lambda frame=df: _bench_density(frame, args.repeat)
            )
            report["sections"]["webgl"] = _run_section(
                "webgl", lambda frame=df: _bench_webgl(frame, args.repeat)
            )

        del df
        gc.collect()

        _generate_audit_csv(audit_csv, args.audit_rows, args.seed + 1)
        report["sections"]["audit_read_metrics"] = _run_section(
            "audit_read_metrics", lambda: _bench_audit(audit_csv, args.audit_rows)
        )

    failed = [
        key
        for key, value in report["sections"].items()
        if isinstance(value, dict) and value.get("ok") is False
    ]
    report["ok"] = not failed
    report["failed_sections"] = failed
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
