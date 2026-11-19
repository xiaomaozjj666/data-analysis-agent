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
    draft = "订单数：1 笔" + chr(10) + "销售额：10.00 元"
    client.post(f"/api/sessions/{session_id}/sales-audit", json={"draft": draft})
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
    entries = [{"seq": 1, "hash": "a" * 64, "prev": "0" * 64, "signature_key_id": "k"}]
    result = verify_entries_signatures(entries, b"not-a-key")
    assert result["valid"] is False
    assert "没有签名" in result["errors"][0]
