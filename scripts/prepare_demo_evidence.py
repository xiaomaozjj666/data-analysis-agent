r"""一键准备现场证据包：不调用模型、不依赖外网，可在断网赛场重复执行。

流程（全部走应用自身的 API，in-process 运行）：

1. 用内置 ``sales_audit`` 演示数据创建会话；已有同款演示会话时直接复用；
2. 用标准演示草稿（销售额 17224.00 / 销量 462 / 订单数 462 / 市场份额 37%）
   调 ``POST /api/sessions/{id}/sales-audit`` 做独立复算；
3. 调 ``POST /api/sessions/{id}/sales-audit/confirm``，由服务端按原始 CSV
   重新复算并写入 ``audit_ledger.jsonl``；
4. 调 ``POST /api/sessions/{id}/sales-audit/bundle`` 保存
   ``evidence-<会话 ID>.zip``；
5. 调 ``scripts/independent_replay.py`` 只用标准库从头复算并校验证据包。

脚本幂等：默认复用已有的 ``sales_audit`` 演示会话（只追加新的账本记录），
重复运行不会无限堆积会话；``--fresh`` 强制新建，``--session-id`` 指定会话。

用法（仓库根目录）：

    .\.venv\Scripts\python.exe scripts\prepare_demo_evidence.py
    .\.venv\Scripts\python.exe scripts\prepare_demo_evidence.py --runs-dir runs --out-dir evidence
    .\.venv\Scripts\python.exe scripts\prepare_demo_evidence.py --fresh

默认 ``DATA_AGENT_RUNS_DIR`` 指向仓库的 ``runs/``，证据包输出到 ``evidence/``。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = REPO / "runs"
DEFAULT_OUT_DIR = REPO / "evidence"
DEMO_KIND = "sales_audit"

# 与演示页内置草稿一致：订单数 462 是预设错数，市场份额缺少全表口径。
DEMO_DRAFT = "\n".join(
    [
        "销售额合计：17224.00 元",
        "销量合计：462 件",
        "订单数：462 笔",
        "市场份额：37%",
    ]
)


def _find_demo_session(runs_dir: Path) -> str | None:
    """找最近一次的 sales_audit 演示会话（sample 路由会写 demo.json）。"""
    best: tuple[float, str] | None = None
    if not runs_dir.is_dir():
        return None
    for entry in runs_dir.iterdir():
        marker = entry / "demo.json"
        if not entry.is_dir() or not marker.is_file():
            continue
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("kind") != DEMO_KIND:
            continue
        input_dir = entry / "input"
        if not input_dir.is_dir() or not any(
            path.is_file() and path.suffix.lower() == ".csv" for path in input_dir.iterdir()
        ):
            continue
        mtime = marker.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, entry.name)
    return best[1] if best else None


def _expect(response: Any, expected: int, action: str) -> Any:
    if response.status_code != expected:
        detail = response.text[:400]
        raise SystemExit(f"{action}失败：HTTP {response.status_code} {detail}")
    return response


def _print_json_summary(label: str, payload: dict[str, Any]) -> None:
    metrics = payload.get("metrics") or {}
    if metrics:
        rendered = " / ".join(f"{name} {value}" for name, value in metrics.items())
        print(f"{label}：{rendered}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="准备有据经营简报的现场证据包，并做独立复算校验。"
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=DEFAULT_RUNS_DIR,
        help="会话数据目录（默认仓库 runs/，会设置为 DATA_AGENT_RUNS_DIR）。",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="证据包输出目录（默认仓库 evidence/）。",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="复用指定会话；默认复用最近的 sales_audit 演示会话，没有则新建。",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="强制新建演示会话，不复用已有会话。",
    )
    parser.add_argument(
        "--skip-replay",
        action="store_true",
        help="只生成证据包，不调用 independent_replay.py。",
    )
    args = parser.parse_args(argv)

    runs_dir = args.runs_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 必须在导入应用之前设置：registry 在 import 时读取 runs_dir 与访问令牌。
    os.environ["DATA_AGENT_RUNS_DIR"] = str(runs_dir)
    os.environ["APP_ACCESS_TOKEN"] = ""
    sys.path.insert(0, str(REPO / "src"))

    from fastapi.testclient import TestClient

    from data_agent import api

    client = TestClient(api.app)

    session_id = args.session_id
    reused = False
    if session_id is None and not args.fresh:
        session_id = _find_demo_session(runs_dir)
    if session_id is not None:
        response = client.get(f"/api/sessions/{session_id}")
        if response.status_code == 200:
            reused = True
        else:
            print(f"指定/发现的会话不可用（HTTP {response.status_code}），改为新建演示会话。")
            session_id = None
    if session_id is None:
        created = _expect(
            client.post(f"/api/sessions/sample?kind={DEMO_KIND}"), 201, "创建演示会话"
        )
        session_id = created.json()["id"]

    print(f"会话 ID：{session_id}（{'复用' if reused else '新建'}）")
    print(f"会话目录：{runs_dir / session_id}")

    audit = _expect(
        client.post(
            f"/api/sessions/{session_id}/sales-audit",
            json={"draft": DEMO_DRAFT, "confirm_full_table": False},
        ),
        200,
        "复算",
    ).json()
    _print_json_summary("复算结果", audit)
    print(f"audit_id：{audit['audit_id']}")
    print(
        "逐行判定："
        + " / ".join(
            f"{claim.get('metric') or claim.get('text') or '?'}={claim['status']}"
            for claim in audit.get("claims", [])
        )
    )

    confirm = _expect(
        client.post(
            f"/api/sessions/{session_id}/sales-audit/confirm",
            json={
                "draft": DEMO_DRAFT,
                "audit_id": audit["audit_id"],
                "confirm_full_table": False,
            },
        ),
        200,
        "服务端确认",
    ).json()
    receipt = confirm["receipt"]
    print(
        f"确认回执：序号 #{receipt['seq']} · {receipt['ts']} · hash "
        f"{receipt['hash'][:16]}… · brief_sha256 {receipt['brief_sha256'][:16]}…"
    )

    bundle = _expect(
        client.post(
            f"/api/sessions/{session_id}/sales-audit/bundle",
            json={
                "draft": DEMO_DRAFT,
                "confirm_full_table": False,
                "draft_trace_sha256": None,
            },
        ),
        200,
        "导出证据包",
    )
    zip_path = out_dir / f"evidence-{session_id}.zip"
    zip_path.write_bytes(bundle.content)
    print(f"账本路径：{runs_dir / session_id / 'audit_ledger.jsonl'}")
    print(f"证据包：{zip_path}（{zip_path.stat().st_size:,} 字节）")

    if args.skip_replay:
        print("已跳过独立复算（--skip-replay）。")
        return 0

    replay = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "independent_replay.py"), str(zip_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
    )
    output = (replay.stdout or "") + (replay.stderr or "")
    if output.strip():
        print("--- independent_replay.py ---")
        print(output.rstrip())
    if replay.returncode != 0:
        print(f"独立复算失败：退出码 {replay.returncode}", file=sys.stderr)
        return replay.returncode
    print("独立复算：通过（仅依赖 Python 标准库，未 import 项目代码）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
