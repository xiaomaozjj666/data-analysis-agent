"""Capture the README screenshots (docs/screenshots/*.png) from the running app.

Dev-time tool, not part of the test suite. Can either connect to an existing
local server (default http://127.0.0.1:8000, or ``--app-url``) or spin up an
isolated local server automatically with ``--auto-start`` (using a temporary
runs directory seeded with the built-in demo datasets so it never touches your
real runs/ or requires external LLM/network calls).

Usage:
    python scripts/capture_docs_screenshots.py --auto-start
    python scripts/capture_docs_screenshots.py [--app-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 960}


def read_token() -> str:
    env_token = os.environ.get("APP_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    env_file = REPO / ".env"
    if not env_file.is_file():
        return ""
    text = env_file.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^APP_ACCESS_TOKEN\s*=\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, timeout: float = 35.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.35)
    raise RuntimeError(f"Server did not become healthy in time: {base_url}")


def _post_json(url: str, body: bytes = b"{}", token: str = "") -> dict:
    import json

    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Access-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _patch_json(url: str, body: bytes, token: str = "") -> dict:
    import json

    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Access-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="PATCH")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _seed_sample_sessions(base_url: str, token: str) -> tuple[str, str]:
    """Create built-in bigdata & sales_audit demo sessions via live API."""
    import json

    big = _post_json(f"{base_url}/api/sessions/sample?kind=bigdata", token=token)
    sales = _post_json(f"{base_url}/api/sessions/sample?kind=sales_audit", token=token)
    big_id = str(big["id"])
    sales_id = str(sales["id"])
    _patch_json(
        f"{base_url}/api/sessions/{big_id}",
        body=json.dumps({"title": "20万行密度视图"}).encode("utf-8"),
        token=token,
    )
    _patch_json(
        f"{base_url}/api/sessions/{sales_id}",
        body=json.dumps({"title": "经营简报核验"}).encode("utf-8"),
        token=token,
    )

    # Pre-run sales audit + server confirmation so the Sales Audit tab shows a live verified receipt
    draft = "销售额合计：17224.00 元\n销量合计：462 件\n订单数：462 笔\n市场份额：37%"
    audit = _post_json(
        f"{base_url}/api/sessions/{sales_id}/sales-audit",
        body=json.dumps({"draft": draft, "confirm_full_table": False}).encode("utf-8"),
        token=token,
    )
    if audit.get("audit_id"):
        fixed_draft = "销售额合计：17224.00 元\n销量合计：462 件\n订单数：20 笔"
        audit2 = _post_json(
            f"{base_url}/api/sessions/{sales_id}/sales-audit",
            body=json.dumps({"draft": fixed_draft, "confirm_full_table": False}).encode("utf-8"),
            token=token,
        )
        _post_json(
            f"{base_url}/api/sessions/{sales_id}/sales-audit/confirm",
            body=json.dumps(
                {
                    "draft": fixed_draft,
                    "audit_id": audit2["audit_id"],
                    "confirm_full_table": False,
                }
            ).encode("utf-8"),
            token=token,
        )
    return big_id, sales_id


def _launch_playwright(playwright, **kwargs):
    """优先使用系统 Edge / Playwright 内置 Chromium，缺失时回退本地缓存。"""
    for attempt in ({"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch(**attempt, **kwargs)
        except Exception:  # noqa: BLE001
            continue
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "",
        str(Path.home() / "AppData" / "Local" / "ms-playwright"),
        str(Path.home() / ".cache" / "ms-playwright"),
    ]
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for pattern in ("chromium-*/chrome-win64/chrome.exe", "chromium-*/chrome-linux/chrome"):
            for candidate in sorted(glob.glob(str(Path(root) / pattern)), reverse=True):
                try:
                    return playwright.chromium.launch(executable_path=candidate, **kwargs)
                except Exception:  # noqa: BLE001
                    continue
    raise RuntimeError("No usable Chromium/Edge browser found for Playwright")


def _capture_via_node(base_url: str, token: str, big_id: str, sales_id: str) -> None:
    """Fallback driver using Node @playwright/test when Python playwright is not in venv."""
    script = f"""
const path = require("node:path");
const pwMod = process.env.PW_MODULE || "@playwright/test";
const {{ chromium }} = require(pwMod);

const APP = {base_url!r};
const OUT = {str(OUT)!r};
const TOKEN = {token!r};
const BIG_ID = {big_id!r};
const SALES_ID = {sales_id!r};

async function openSessionByTitle(page, title) {{
  const item = page.locator("aside").getByText(title, {{ exact: false }}).first();
  if (!(await item.isVisible().catch(() => false))) {{
    await page.locator("aside").getByText("历史会话").first().click();
    await page.waitForTimeout(700);
  }}
  await item.click();
  await page.waitForTimeout(2800);
}}

async function clickTab(page, label) {{
  await page.evaluate((text) => {{
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    const tab = tabs.find((n) => (n.textContent || "").trim().includes(text));
    if (tab) {{ tab.click(); return; }}
    const el = Array.from(document.querySelectorAll("button, a"))
      .find((n) => (n.textContent || "").trim() === text);
    if (el) el.click();
  }}, label);
  await page.waitForTimeout(2200);
}}

(async () => {{
  let browser;
  try {{
    browser = await chromium.launch({{ channel: "msedge" }});
  }} catch {{
    browser = await chromium.launch();
  }}
  const ctx = await browser.newContext({{
    viewport: {{ width: 1440, height: 960 }},
    reducedMotion: "reduce",
  }});
  const page = await ctx.newPage();
  await page.goto(APP, {{ waitUntil: "domcontentloaded" }});
  await page.evaluate((t) => {{
    if (t) localStorage.setItem("data-desk-access-token", t);
    localStorage.removeItem("data-desk-current-session");
    localStorage.setItem("data-desk-theme", "light");
  }}, TOKEN);
  await page.reload({{ waitUntil: "networkidle" }});
  await page.waitForTimeout(1800);

  // 1) 起始页（浅色）
  await page.screenshot({{ path: path.join(OUT, "start-light.png") }});
  console.log("wrote start-light.png");

  // 2) 大数据样例会话 → 分析总览（浅色）
  await openSessionByTitle(page, "20万行密度视图");
  await clickTab(page, "分析");
  await page.screenshot({{ path: path.join(OUT, "overview-light.png") }});
  console.log("wrote overview-light.png");

  // 3) 数据预览表格
  await clickTab(page, "数据");
  await page.waitForTimeout(1200);
  await page.screenshot({{ path: path.join(OUT, "data-preview.png") }});
  console.log("wrote data-preview.png");

  // 4) 产物中心（高视口保证全部懒加载图表缩略图渲染完成）
  const tall = await browser.newPage({{ viewport: {{ width: 1440, height: 1560 }}, reducedMotion: "reduce" }});
  await tall.goto(APP, {{ waitUntil: "networkidle" }});
  await tall.waitForTimeout(1500);
  await openSessionByTitle(tall, "20万行密度视图");
  await clickTab(tall, "产物");
  await tall.waitForTimeout(4500);
  await tall.screenshot({{ path: path.join(OUT, "artifacts-light.png") }});
  console.log("wrote artifacts-light.png");

  // 5) 图表预览模态（在产物页点击第一张图表预览）
  await tall.evaluate(() => {{
    const cards = Array.from(document.querySelectorAll(".chart-card, [class*='artifact-card']"));
    const target = cards.find((c) => (c.textContent || "").includes("密度") || (c.textContent || "").includes("整体")) || cards[0];
    const btn = target && (target.querySelector(".preview-button, button[title*='预览'], button") || target);
    if (btn) btn.click();
  }});
  await tall.waitForTimeout(4800);
  await tall.setViewportSize({{ width: 1440, height: 960 }});
  await tall.waitForTimeout(800);
  await tall.screenshot({{ path: path.join(OUT, "preview-light.png") }});
  console.log("wrote preview-light.png");
  await tall.close();

  // 6) 经营简报核验视图（点击「按原表复算」展示 462 -> 20 差额核验结果）
  await openSessionByTitle(page, "经营简报核验");
  await clickTab(page, "简报核验");
  await page.waitForTimeout(1200);
  const runAuditBtn = page.getByRole("button", {{ name: /按原表复算/ }}).first();
  if (await runAuditBtn.isVisible().catch(() => false)) {{
    await runAuditBtn.click();
    await page.waitForTimeout(2200);
  }}
  await page.screenshot({{ path: path.join(OUT, "sales-audit-light.png") }});
  console.log("wrote sales-audit-light.png");

  // 7) 暗色主题主工作台
  await openSessionByTitle(page, "20万行密度视图");
  await page.evaluate(() => {{
    const b = document.querySelector('button[title*="主题"], .theme-toggle, button[aria-label*="主题"], button[title*="暗"], button[title*="深色"]');
    if (b) b.click();
  }});
  await page.waitForTimeout(1400);
  await clickTab(page, "分析");
  await page.screenshot({{ path: path.join(OUT, "overview-dark.png") }});
  console.log("wrote overview-dark.png");

  await browser.close();
}})().catch((e) => {{
  console.error(e);
  process.exit(1);
}});
"""
    subprocess.run(["node", "-e", script], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-url", default="http://127.0.0.1:8000")
    parser.add_argument("--auto-start", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    if args.auto_start:
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory(prefix="daa-shots-") as tmp:
            env = os.environ.copy()
            env["DATA_AGENT_RUNS_DIR"] = tmp
            env["DATA_AGENT_STATIC_DIR"] = str(REPO / "frontend" / "dist")
            env["APP_ACCESS_TOKEN"] = ""
            env["APP_VIEWER_TOKEN"] = ""
            env["PYTHONUTF8"] = "1"
            srv = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "data_agent.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=str(REPO),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _wait_health(base_url)
                big_id, sales_id = _seed_sample_sessions(base_url, "")
                _capture_via_node(base_url, "", big_id, sales_id)
            finally:
                srv.terminate()
                srv.wait(timeout=8)
        return 0

    token = read_token()
    big_id, sales_id = _seed_sample_sessions(args.app_url.rstrip("/"), token)
    _capture_via_node(args.app_url.rstrip("/"), token, big_id, sales_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
