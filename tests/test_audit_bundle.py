"""零信任证据包：独立脚本只依赖标准库也能复算、验签并识别篡改。"""

from __future__ import annotations

import hashlib
import io
import json
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
DRAFT = "销售额合计：20.00 元\n销量合计：3 件\n订单数：2 笔"


def _session(tmp_path, monkeypatch) -> TestClient:
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    upload = client.post(
        "/api/sessions", files={"file": ("sales.csv", SALES, "text/csv")}
    )
    return client, upload.json()["id"]


def _confirm(client: TestClient, session_id: str, draft: str = DRAFT, **extra):
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": draft, **extra},
    )
    assert audit.status_code == 200, audit.text
    body = audit.json()
    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": body["audit_id"], **extra},
    )
    assert confirm.status_code == 200, confirm.text
    return body, confirm.json()


def _bundle(client: TestClient, session_id: str, draft: str = DRAFT, **extra) -> bytes:
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle",
        json={"draft": draft, **extra},
    )
    assert response.status_code == 200, response.text
    return response.content


def _run(zip_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(zip_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _repack(
    content: bytes,
    path: Path,
    *,
    edits: dict[str, bytes] | None = None,
    drop: tuple[str, ...] = (),
) -> Path:
    """按“能改包的攻击者”方式重建 ZIP：替换/删除文件并同步 manifest 哈希。"""
    source = zipfile.ZipFile(io.BytesIO(content))
    manifest = json.loads(source.read("manifest.json"))
    files = {name: source.read(name) for name in source.namelist()}
    for name in drop:
        files.pop(name, None)
        manifest["files"].pop(name, None)
    for name, raw in (edits or {}).items():
        files[name] = raw
        manifest["files"][name] = hashlib.sha256(raw).hexdigest()
    files["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return path


def _strip_signatures(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        lines = archive.read("ledger.jsonl").decode("utf-8").splitlines()
    rewritten = []
    for line in lines:
        entry = json.loads(line)
        entry.pop("signature", None)
        entry.pop("signature_key_id", None)
        rewritten.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    return ("\n".join(rewritten) + "\n").encode("utf-8")


def test_unconfirmed_bundle_is_rejected(tmp_path, monkeypatch):
    """只 audit、未 confirm 的包不再判通过：确认门必须留下 confirm 记录。"""
    client, session_id = _session(tmp_path, monkeypatch)
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit", json={"draft": DRAFT}
    )
    assert audit.status_code == 200
    bundle = tmp_path / "audit-only.zip"
    bundle.write_bytes(_bundle(client, session_id))

    result = _run(bundle)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "confirm" in result.stdout


def test_confirmed_bundle_replays_from_stdlib_only(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    bundle = tmp_path / "evidence.zip"
    bundle.write_bytes(_bundle(client, session_id))

    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {
        "source.csv",
        "draft.txt",
        "schema.json",
        "report.json",
        "manifest.json",
        "ledger.jsonl",
    } <= names
    assert set(manifest["files"]) <= names

    result = _run(bundle)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "独立复算一致" in result.stdout
    assert "confirm     1 条" in result.stdout
    assert "signature   2/2 条，Ed25519 有效" in result.stdout
    assert "未 import 项目代码" in result.stdout


def test_tampered_source_fails(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    tampered = _repack(
        _bundle(client, session_id),
        tmp_path / "tampered.zip",
        edits={"source.csv": SALES.replace(b"10.00", b"99.00", 1)},
    )
    result = _run(tampered)
    assert result.returncode == 1
    assert "SHA-256" in result.stdout


def test_stripped_signatures_fail_instead_of_passing(tmp_path, monkeypatch):
    """blocking 回归：删光签名后链仍自洽，但必须报缺少签名并退出 1。"""
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    stripped = _repack(
        _bundle(client, session_id),
        tmp_path / "stripped.zip",
        edits={"ledger.jsonl": _strip_signatures(_bundle(client, session_id))},
    )
    result = _run(stripped)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "缺少 Ed25519 签名" in result.stdout
    assert "Ed25519 有效" not in result.stdout


def test_partial_signature_strip_fails(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    with zipfile.ZipFile(io.BytesIO(_bundle(client, session_id))) as archive:
        original = json.loads(archive.read("ledger.jsonl").decode("utf-8").splitlines()[0])
    stripped_all = _strip_signatures(_bundle(client, session_id)).decode("utf-8").splitlines()
    stripped_all[0] = json.dumps(original, ensure_ascii=False, sort_keys=True)
    ledger = ("\n".join(stripped_all) + "\n").encode("utf-8")

    result = _run(
        _repack(
            _bundle(client, session_id),
            tmp_path / "partial.zip",
            edits={"ledger.jsonl": ledger},
        )
    )
    assert result.returncode == 1
    assert "缺少 Ed25519 签名" in result.stdout


def test_missing_or_empty_ledger_fails(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    content = _bundle(client, session_id)

    empty = _repack(content, tmp_path / "empty.zip", edits={"ledger.jsonl": b""})
    result = _run(empty)
    assert result.returncode == 1
    assert "账本为空" in result.stdout

    missing = _repack(content, tmp_path / "missing.zip", drop=("ledger.jsonl",))
    result = _run(missing)
    assert result.returncode == 1
    assert "缺少 ledger.jsonl" in result.stdout


def test_missing_public_key_fails(tmp_path, monkeypatch):
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    result = _run(
        _repack(
            _bundle(client, session_id),
            tmp_path / "no-pubkey.zip",
            drop=("public_key.pem",),
        )
    )
    assert result.returncode == 1
    assert "缺少 public_key.pem" in result.stdout


def test_replaced_schema_metadata_is_rejected(tmp_path, monkeypatch):
    """只改口径展示元数据（unit/aliases）也必须被抓到。"""
    client, session_id = _session(tmp_path, monkeypatch)
    _confirm(client, session_id)
    with zipfile.ZipFile(io.BytesIO(_bundle(client, session_id))) as archive:
        schema = json.loads(archive.read("schema.json"))
    schema["metrics"][0]["unit"] = "RMB"
    schema["metrics"][0]["aliases"] = ["revenue"]
    result = _run(
        _repack(
            _bundle(client, session_id),
            tmp_path / "schema-tampered.zip",
            edits={"schema.json": json.dumps(schema, ensure_ascii=False).encode("utf-8")},
        )
    )
    assert result.returncode == 1
    assert "口径文件被替换" in result.stdout


def test_replaced_lineage_is_rejected(tmp_path, monkeypatch):
    from data_agent.audit_lineage import save_lineage

    client, session_id = _session(tmp_path, monkeypatch)
    root = api.registry.get(session_id).workspace.root
    save_lineage(
        root,
        {
            "upstream_table": "dwd.orders_daily",
            "extracted_at": "2026-11-21T08:00:00+08:00",
            "owner": "data-platform",
        },
    )
    _confirm(client, session_id)
    content = _bundle(client, session_id)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        lineage = json.loads(archive.read("lineage.json"))
    lineage["owner"] = "someone-else"
    result = _run(
        _repack(
            content,
            tmp_path / "lineage-tampered.zip",
            edits={"lineage.json": json.dumps(lineage, ensure_ascii=False).encode("utf-8")},
        )
    )
    assert result.returncode == 1
    assert "来源血缘的指纹不在账本中" in result.stdout


def test_replaced_draft_trace_is_rejected(tmp_path, monkeypatch):
    import hashlib as _hashlib

    from data_agent.audit_draft import write_trace

    client, session_id = _session(tmp_path, monkeypatch)
    root = api.registry.get(session_id).workspace.root
    trace_sha, _ = write_trace(
        root / "draft_traces",
        {
            "schema_version": 1,
            "model": "test-model",
            "parsed_draft": DRAFT,
            "parsed_draft_sha256": _hashlib.sha256(DRAFT.encode("utf-8")).hexdigest(),
        },
    )
    _confirm(client, session_id, draft_trace_sha256=trace_sha)
    content = _bundle(client, session_id, draft_trace_sha256=trace_sha)
    result = _run(
        _repack(
            content,
            tmp_path / "trace-tampered.zip",
            edits={
                "draft_trace.json": json.dumps(
                    {"parsed_draft_sha256": "0" * 64}, ensure_ascii=False
                ).encode("utf-8")
            },
        )
    )
    assert result.returncode == 1
    assert "轨迹被替换" in result.stdout or "草稿哈希" in result.stdout


def test_bundle_tolerates_unreadable_optional_files_and_audit_oserror(
    tmp_path, monkeypatch
):
    """可选文件读失败要退回内置口径；原始 CSV 读失败要返回可读 422。"""
    client, session_id = _session(tmp_path, monkeypatch)

    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.name == "audit_schema.json":
            raise OSError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle", json={"draft": DRAFT}
    )
    assert response.status_code == 200
    assert response.content

    import data_agent.sales_audit as sales_audit

    monkeypatch.setattr(
        sales_audit,
        "audit_sales_brief",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle", json={"draft": DRAFT}
    )
    assert response.status_code == 422
    assert "原始 CSV" in response.json()["detail"]
