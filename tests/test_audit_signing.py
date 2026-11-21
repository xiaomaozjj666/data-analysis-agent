"""账本 Ed25519 签名：生成、验签、篡改失败与证据包复验。"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent.audit_ledger import append_entry, read_entries
from data_agent.audit_signing import (
    public_key_bytes,
    sign_hash,
    verify_entries_signatures,
    verify_hash,
)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "independent_replay.py"


def test_sign_and_verify_and_tamper(tmp_path):
    signed = sign_hash(tmp_path, "a" * 64)
    pem = public_key_bytes(tmp_path)
    assert verify_hash(pem, "a" * 64, signed["signature"])
    assert not verify_hash(pem, "b" * 64, signed["signature"])
    raw = bytearray(base64.b64decode(signed["signature"]))
    raw[0] ^= 1
    assert not verify_hash(pem, "a" * 64, base64.b64encode(raw).decode())


def test_ledger_entries_are_signed(tmp_path):
    append_entry(tmp_path, kind="audit", session_id="s", payload={"x": 1})
    entries = read_entries(tmp_path / "audit_ledger.jsonl")
    assert entries[0]["signature"]
    assert entries[0]["signature_key_id"]
    result = verify_entries_signatures(entries, public_key_bytes(tmp_path))
    assert result == {"valid": True, "signed": 1, "errors": []}


def test_bundle_replay_verifies_signature(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"order_id,sales,quantity\nA,10,1\n", "text/csv")},
    ).json()["id"]
    draft = "订单数：1 笔"
    audit = client.post(f"/api/sessions/{session_id}/sales-audit", json={"draft": draft}).json()
    confirmed = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": audit["audit_id"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle",
        json={"draft": draft},
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        ledger = archive.read("ledger.jsonl").decode("utf-8")
    assert "public_key.pem" in names
    assert '"signature"' in ledger

    bundle = tmp_path / "signed.zip"
    bundle.write_bytes(response.content)
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), str(bundle)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Ed25519 有效" in ok.stdout

    tampered = tmp_path / "tampered.zip"
    source = zipfile.ZipFile(io.BytesIO(response.content))
    with zipfile.ZipFile(tampered, "w") as archive:
        for name in source.namelist():
            raw = source.read(name)
            if name == "ledger.jsonl":
                entry = json.loads(raw.decode("utf-8").splitlines()[0])
                signature = bytearray(base64.b64decode(entry["signature"]))
                signature[0] ^= 1
                entry["signature"] = base64.b64encode(signature).decode()
                raw = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode()
            archive.writestr(name, raw)
    failed = subprocess.run(
        [sys.executable, str(SCRIPT), str(tampered)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert failed.returncode == 1
    assert "Ed25519 签名无效" in failed.stdout


def test_seed_env_is_reproducible(tmp_path, monkeypatch):
    seed = base64.b64encode(b"1" * 32).decode()
    monkeypatch.setenv("DATA_AGENT_LEDGER_SEED", seed)
    from data_agent.audit_signing import key_id

    first, second = tmp_path / "a", tmp_path / "b"
    assert key_id(first) == key_id(second)


def test_cli_verifies_signature_and_rejects_tamper(tmp_path):
    ledger = tmp_path / "audit_ledger.jsonl"
    append_entry(tmp_path, kind="audit", session_id="s", payload={"x": 1})
    pubkey = tmp_path / "public.pem"
    pubkey.write_bytes(public_key_bytes(tmp_path))
    cli = REPO / "scripts" / "verify_audit_ledger.py"

    ok = subprocess.run(
        [sys.executable, str(cli), str(ledger), "--pubkey", str(pubkey)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Ed25519 签名" in ok.stdout

    entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    signature = bytearray(base64.b64decode(entry["signature"]))
    signature[0] ^= 1
    entry["signature"] = base64.b64encode(signature).decode()
    ledger.write_text(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) + chr(10), encoding="utf-8"
    )
    bad = subprocess.run(
        [sys.executable, str(cli), str(ledger), "--pubkey", str(pubkey)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert bad.returncode == 1
    assert "签名无效" in bad.stdout


def test_unsigned_entry_is_rejected(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    entries = [{"seq": 1, "hash": "a" * 64, "prev": "0" * 64, "signature_key_id": "k"}]
    result = verify_entries_signatures(entries, pem)
    assert result["valid"] is False
    assert "没有签名" in result["errors"][0]


def test_unparsable_pubkey_is_its_own_error(tmp_path):
    import pytest

    """坏公钥 ≠ 逐条签名无效：错误文案必须区分（2026-11-19 评审 N7）。"""
    entries = [{"seq": 1, "hash": "a" * 64, "signature": "c2ln"}]
    with pytest.raises(ValueError, match="公钥无法解析"):
        verify_entries_signatures(entries, b"not-a-key")


def test_invalid_seed_and_non_ed25519_key(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("DATA_AGENT_LEDGER_SEED", base64.b64encode(b"short").decode())
    from data_agent.audit_signing import key_id

    with pytest.raises(ValueError, match="32 字节"):
        key_id(tmp_path)

    assert verify_hash(b"not a pem", "a" * 64, "c2ln") is False


def test_seed_signs_and_bad_signature_encodings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_LEDGER_SEED", base64.b64encode(b"2" * 32).decode())
    signed = sign_hash(tmp_path, "c" * 64)
    pem = public_key_bytes(tmp_path)
    assert verify_hash(pem, "c" * 64, signed["signature"])
    assert verify_hash(pem, "c" * 64, "!!!not-base64!!!") is False
    result = verify_entries_signatures([{"seq": 1, "hash": "c" * 64, "signature": "!!!"}], pem)
    assert result["valid"] is False
    assert "签名无效" in result["errors"][0]


def test_signature_check_and_tamper_endpoints(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"order_id,sales,quantity\nA,10,1\n", "text/csv")},
    ).json()["id"]
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "订单数：1 笔" + chr(10) + "销售额：10.00 元"},
    )
    # 中间步骤显式断言：否则后续 KeyError 只会掩盖真实失败原因（CI 2026-11-19）
    assert audit.status_code == 200, audit.text
    audit_body = audit.json()
    assert "audit_id" in audit_body and audit_body.get("ledger_error") is None, audit.text
    checked_response = client.get(f"/api/sessions/{session_id}/sales-audit/signature-check")
    assert checked_response.status_code == 200, checked_response.text
    checked = checked_response.json()
    assert checked["valid"] is True
    assert checked["signed"] >= 1

    tampered = client.get(f"/api/sessions/{session_id}/sales-audit/signature-tamper-demo").json()
    assert tampered["valid"] is False
    assert "签名无效" in tampered["errors"][0]


def test_verify_script_warns_when_signatures_unchecked(tmp_path, monkeypatch):
    """I1：条目带签名但未给 --pubkey 时必须显式“签名未校验”，不得静默通过。"""
    import base64 as b64

    monkeypatch.setenv("DATA_AGENT_LEDGER_SEED", b64.b64encode(b"3" * 32).decode())
    import pytest

    from data_agent.audit_ledger import LedgerError, append_entry

    root = tmp_path / "ws"
    append_entry(root, kind="audit", session_id="s", payload={"note": "x"})
    pub_path = root / "audit_keys" / "ledger_ed25519.pub"
    assert pub_path.is_file()

    from scripts.verify_audit_ledger import main as verify_main

    code = verify_main([str(root / "audit_ledger.jsonl"), "--json"])
    assert code == 0  # 链校验仍通过；退出码语义不因“未验签”改变
    # main 的 JSON 输出走 stdout，重新捕获一次以断言字段
    import contextlib
    import io
    import json as _json

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        verify_main([str(root / "audit_ledger.jsonl"), "--json"])
    payload = _json.loads(buffer.getvalue())
    assert payload["signatures_checked"] is False
    assert any("签名未校验" in item for item in payload["verification"]["errors"])

    # I2 fail-closed：同一工作区切换种子 = 换身份，必须拒绝续写；若新种子
    # 与旧公钥不一致却继续追加，新旧条目会分属两把钥匙、验签必然一半失败。
    monkeypatch.setenv("DATA_AGENT_LEDGER_SEED", b64.b64encode(b"4" * 32).decode())
    with pytest.raises((LedgerError, ValueError), match="不一致|公钥"):
        append_entry(root, kind="audit", session_id="s", payload={"note": "y"})

    pub_args = [str(root / "audit_ledger.jsonl"), "--json", "--pubkey", str(pub_path)]
    buffer2 = io.StringIO()
    with contextlib.redirect_stdout(buffer2):
        assert verify_main(pub_args) in {0, 1}
    payload2 = _json.loads(buffer2.getvalue())
    assert payload2["signatures_checked"] is True
    # 单一身份（种子 3）签署的账本，用对应公钥验签必须全部有效
    assert payload2["checks"]["signature"]["valid"] is True


def test_signature_endpoints_without_ledger(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions", files={"file": ("sales.csv", b"a,b\n1,2\n", "text/csv")}
    ).json()["id"]
    assert client.get(f"/api/sessions/{session_id}/sales-audit/signature-check").status_code == 404
    assert (
        client.get(f"/api/sessions/{session_id}/sales-audit/signature-tamper-demo").status_code
        == 404
    )


def test_public_key_without_private_is_an_error(tmp_path):
    import pytest

    public_key_bytes(tmp_path)
    (tmp_path / "audit_keys" / "ledger_ed25519.key").unlink()
    from data_agent.audit_signing import key_id

    with pytest.raises(ValueError, match="私钥"):
        key_id(tmp_path)


def test_tamper_demo_requires_a_signature(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions", files={"file": ("sales.csv", b"a,b\n1,2\n", "text/csv")}
    ).json()["id"]
    workspace = api.registry.get(session_id).workspace
    (workspace.root / "audit_ledger.jsonl").write_text(
        '{"seq": 1, "prev": "' + "0" * 64 + '", "hash": "' + "a" * 64 + '"}' + chr(10),
        encoding="utf-8",
    )
    response = client.get(f"/api/sessions/{session_id}/sales-audit/signature-tamper-demo")
    assert response.status_code == 422


def test_session_export_never_includes_private_key(tmp_path, monkeypatch):
    """会话 ZIP 会被转发/归档：私钥必须留在本机；导入后旧账本仍可验签，
    但新增追加必须显式失败而不是静默换钥匙。"""
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"order_id,sales,quantity\nA,10,1\n", "text/csv")},
    ).json()["id"]
    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "订单数：1 笔" + chr(10) + "销售额：10.00 元"},
    )
    assert audit.status_code == 200 and audit.json().get("ledger_error") is None, audit.text

    exported = client.get(f"/api/sessions/{session_id}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = archive.namelist()
    assert "audit_ledger.jsonl" in names
    assert any(name.endswith("ledger_ed25519.pub") for name in names)
    assert not any(name.endswith(".key") for name in names), names

    imported = client.post(
        "/api/sessions/import",
        files={"file": ("session.zip", exported.content, "application/zip")},
    )
    assert imported.status_code == 201, imported.text
    imported_id = imported.json()["id"]
    check = client.get(f"/api/sessions/{imported_id}/sales-audit/signature-check")
    assert check.status_code == 200, check.text
    assert check.json()["valid"] is True

    # 私钥没随包走：追加必须失败并给出可操作原因（不再生成新钥匙冒充旧身份）
    again = client.post(
        f"/api/sessions/{imported_id}/sales-audit",
        json={"draft": "订单数：1 笔" + chr(10) + "销售额：10.00 元"},
    )
    assert again.status_code == 200
    ledger_error = again.json().get("ledger_error") or ""
    assert "私钥" in ledger_error and "公钥" in ledger_error, again.text


def test_bundle_download_uses_temp_file_and_cleans_up(tmp_path, monkeypatch):
    import tempfile as tempfile_module

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    monkeypatch.setattr(tempfile_module, "tempdir", str(download_dir))
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    session_id = client.post("/api/sessions/sample?kind=sales_audit").json()["id"]
    draft = "订单数：20 笔"
    client.post(f"/api/sessions/{session_id}/sales-audit", json={"draft": draft})
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/bundle", json={"draft": draft}
    )
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert "evidence-" in response.headers["content-disposition"]
    assert not list(download_dir.glob("data-agent-download-*.zip"))
