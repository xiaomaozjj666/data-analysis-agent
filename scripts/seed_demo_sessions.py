"""把五份演示会话写入 runs/，让新环境打开就有历史记录。

用法：
    python scripts/seed_demo_sessions.py
    python scripts/seed_demo_sessions.py --runs-dir runs

脚本通过应用自己的 API 建会话，因此预置的口径、策略、血缘与预分析都会
一起生成；已存在同名会话时跳过，可重复执行。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMOS = (
    ("sales_audit", "经营简报核验"),
    ("regional_policy", "区域权限与 PII"),
    ("quality_injection", "脏数据与提示注入"),
    ("growth", "多维增长月报"),
    ("bigdata", "20 万行密度视图"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写入五份演示会话。")
    parser.add_argument("--runs-dir", type=Path, default=REPO / "runs")
    args = parser.parse_args(argv)
    runs_dir = args.runs_dir.expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_AGENT_RUNS_DIR"] = str(runs_dir)
    os.environ["APP_ACCESS_TOKEN"] = ""
    sys.path.insert(0, str(REPO / "src"))

    from fastapi.testclient import TestClient

    from data_agent import api

    client = TestClient(api.app)
    existing = {
        str(item.get("title") or "")
        for item in client.get("/api/sessions?limit=100").json().get("sessions", [])
    }
    created = 0
    for kind, title in DEMOS:
        if title in existing:
            print(f"跳过：{title} 已存在")
            continue
        response = client.post(f"/api/sessions/sample?kind={kind}")
        if response.status_code != 201:
            print(f"失败：{title} → HTTP {response.status_code} {response.text[:200]}")
            return 1
        session_id = response.json()["id"]
        client.patch(f"/api/sessions/{session_id}", json={"title": title})
        created += 1
        print(f"已写入：{title}（{session_id}）")
    print(f"完成：新增 {created} 条，目录 {runs_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
