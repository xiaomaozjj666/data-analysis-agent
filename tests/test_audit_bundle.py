"""零信任证据包：独立脚本只依赖标准库也能复算并重现 audit_id。"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "independent_replay.py"
SALES = b"order_id,sales,quantity\nA,10.00,1\nA,2.50,1\nB,7.50,1\n"


def _session(tmp_path, monkeypatch) -> TestClient:
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions", files={"file": ("sales.csv", SALES, "text/csv")}
    )
    return client, upload.json()["id"]


def _run(zip_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(zip_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_bundle_replays_from_stdlib_only(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    draft = "销售额合计：20.00 元\n销量合计：3 件\n订单数：2 笔"
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle",
        json={"draft": draft},
    )
    assert response.status_code == 200
    bundle = tmp_path / "evidence.zip"
    bundle.write_bytes(response.content)

    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        manifest = __import__("json").loads(archive.read("manifest.json"))
    assert {"source.csv", "draft.txt", "schema.json", "report.json", "manifest.json"} <= names
    assert "ledger.jsonl" in names
    assert set(manifest["files"]) <= names

    result = _run(bundle)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "独立复算一致" in result.stdout
    assert "未 import 项目代码" in result.stdout


def test_tampered_bundle_fails(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle",
        json={"draft": "订单数：2 笔"},
    )
    source = zipfile.ZipFile(io.BytesIO(response.content))
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name in source.namelist():
            raw = source.read(name)
            if name == "source.csv":
                raw = raw.replace(b"10.00", b"99.00", 1)
            archive.writestr(name, raw)
    result = _run(tampered)
    assert result.returncode == 1
    assert "SHA-256" in result.stdout
