"""哈希链账本与服务端确认门的测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import _isolate_runtime

from data_agent import api
from data_agent.audit_draft import verify_trace
from data_agent.audit_ledger import (
    GENESIS_HASH,
    LedgerError,
    _lock_key,
    append_entry,
    read_entries,
    verify_entries,
    verify_ledger,
)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import verify_audit_ledger  # noqa: E402


def _audit_payload(**overrides):
    payload = {
        "audit_id": "aud-abc123",
        "source_sha256": "a" * 64,
        "draft_sha256": "b" * 64,
        "summary": {"verified": 1, "conflict": 0, "unsupported": 0},
    }
    payload.update(overrides)
    return payload


def _append(root, **overrides):
    payload = _audit_payload(**overrides)
    return append_entry(
        root,
        kind=payload.pop("kind", "audit"),
        session_id="s1",
        payload=payload,
    )


def test_ledger_chain_links_entries(tmp_path):
    first = _append(tmp_path)
    second = _append(tmp_path, kind="confirm", brief_sha256="c" * 64)
    assert first["seq"] == 1
    assert first["prev"] == GENESIS_HASH
    assert second["seq"] == 2
    assert second["prev"] == first["hash"]
    result = verify_ledger(tmp_path / "audit_ledger.jsonl")
    assert result["valid"] and result["entries"] == 2
    assert result["tip"] == second["hash"]


def test_tampering_breaks_the_chain(tmp_path):
    _append(tmp_path)
    _append(tmp_path, kind="confirm")
    path = tmp_path / "audit_ledger.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["source_sha256"] = "f" * 64
    lines[0] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_ledger(path)
    assert not result["valid"]
    assert any("无法复算" in error or "prev" in error for error in result["errors"])


def test_append_refuses_a_broken_chain(tmp_path):
    _append(tmp_path)
    path = tmp_path / "audit_ledger.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        _append(tmp_path)


def _session_with_sales(tmp_path, monkeypatch) -> tuple[TestClient, str]:
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    content = b"order_id,sales,quantity\nA,10.00,1\nA,2.50,1\nB,7.50,1\n"
    upload = client.post("/api/sessions", files={"file": ("sales.csv", content, "text/csv")})
    assert upload.status_code == 201
    return client, upload.json()["id"]


def test_audit_and_confirm_are_recorded(tmp_path, monkeypatch):
    client, session_id = _session_with_sales(tmp_path, monkeypatch)
    draft = "销售额合计：20.00 元\n销量合计：3 件\n订单数：2 笔"
    audit = client.post(f"/api/sessions/{session_id}/sales-audit", json={"draft": draft}).json()
    assert audit["ledger"]["seq"] == 1

    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": audit["audit_id"]},
    )
    assert confirm.status_code == 200
    receipt = confirm.json()["receipt"]
    assert receipt["seq"] == 2
    assert receipt["prev"] == audit["ledger"]["hash"]
    assert receipt["brief_sha256"]

    ledger = client.get(f"/api/sessions/{session_id}/sales-audit/ledger").json()
    assert ledger["verification"]["valid"] is True
    assert [entry["kind"] for entry in ledger["entries"]] == ["audit", "confirm"]


def test_confirm_rejects_stale_or_foreign_audit_id(tmp_path, monkeypatch):
    client, session_id = _session_with_sales(tmp_path, monkeypatch)
    draft = "订单数：2 笔"
    audit = client.post(f"/api/sessions/{session_id}/sales-audit", json={"draft": draft}).json()

    tampered = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft + "（华东区）", "audit_id": audit["audit_id"]},
    )
    assert tampered.status_code == 409

    forged = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": "aud-" + "0" * 32},
    )
    assert forged.status_code == 409


def test_offline_script_detects_one_byte_change(tmp_path, monkeypatch):
    client, session_id = _session_with_sales(tmp_path, monkeypatch)
    draft = "销售额合计：20.00 元\n销量合计：3 件\n订单数：2 笔"
    audit = client.post(f"/api/sessions/{session_id}/sales-audit", json={"draft": draft}).json()
    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": audit["audit_id"]},
    ).json()

    brief = tmp_path / "brief.md"
    # 必须按字节写入：write_text 在 Windows 上会把 \n 转成 \r\n，哈希就对不上了。
    brief.write_bytes(confirm["brief"].encode("utf-8"))
    ledger = tmp_path / "audit_ledger.jsonl"
    workspace = api.registry.get(session_id).workspace
    ledger.write_bytes((workspace.root / "audit_ledger.jsonl").read_bytes())

    assert verify_audit_ledger.main([str(ledger), "--brief", str(brief)]) == 0

    original = brief.read_bytes()
    brief.write_bytes(original.replace(b"20.00", b"20.01", 1))
    assert verify_audit_ledger.main([str(ledger), "--brief", str(brief)]) == 1


def test_ledger_survives_reopen(tmp_path):
    _append(tmp_path)
    assert len(read_entries(tmp_path / "audit_ledger.jsonl")) == 1
    assert read_entries(tmp_path / "missing.jsonl") == []


# --- 起草轨迹绑定：模型原话 → 草稿 → 复算 → 确认 ---


def test_trace_verification_rejects_edits_and_tampering(tmp_path):
    from data_agent.audit_draft import QuickDraftError, verify_trace, write_trace

    draft = "订单数：2 笔"
    trace = {
        "schema_version": 1,
        "parsed_draft": draft,
        "parsed_draft_sha256": __import__("hashlib").sha256(draft.encode()).hexdigest(),
    }
    digest, path = write_trace(tmp_path / "draft_traces", trace)
    assert path.name == f"{digest}.json"
    assert verify_trace(tmp_path / "draft_traces", digest, draft)["parsed_draft"] == draft

    with pytest.raises(QuickDraftError):
        verify_trace(tmp_path / "draft_traces", digest, draft + "（改过）")

    path.write_text(path.read_text(encoding="utf-8").replace("2 笔", "3 笔"), encoding="utf-8")
    with pytest.raises(QuickDraftError):
        verify_trace(tmp_path / "draft_traces", digest, draft)


def test_audit_confirm_and_offline_script_verify_trace(tmp_path, monkeypatch):
    import hashlib

    from data_agent.audit_draft import write_trace

    client, session_id = _session_with_sales(tmp_path, monkeypatch)
    workspace = api.registry.get(session_id).workspace
    draft = "销售额合计：20.00 元\n销量合计：3 件\n订单数：2 笔"
    trace = {
        "schema_version": 1,
        "model": "test-model",
        "parsed_draft": draft,
        "parsed_draft_sha256": hashlib.sha256(draft.encode()).hexdigest(),
    }
    trace_sha, trace_path = write_trace(workspace.root / "draft_traces", trace)

    audit = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": draft, "draft_trace_sha256": trace_sha},
    ).json()
    assert audit["draft_trace_sha256"] == trace_sha
    assert audit["ledger"]["seq"] == 1

    confirm = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": draft, "audit_id": audit["audit_id"], "draft_trace_sha256": trace_sha},
    )
    assert confirm.status_code == 200
    assert confirm.json()["receipt"]["draft_trace_sha256"] == trace_sha

    ledger = tmp_path / "ledger-copy.jsonl"
    ledger.write_bytes((workspace.root / "audit_ledger.jsonl").read_bytes())
    assert (
        verify_audit_ledger.main(
            [
                str(ledger),
                "--brief",
                str(_write_brief(tmp_path, confirm.json()["brief"])),
                "--trace",
                str(trace_path),
            ]
        )
        == 0
    )

    trace_path.write_text(
        trace_path.read_text(encoding="utf-8").replace("test-model", "other"), encoding="utf-8"
    )
    assert verify_audit_ledger.main([str(ledger), "--trace", str(trace_path)]) == 1


def test_audit_rejects_trace_that_does_not_match_draft(tmp_path, monkeypatch):
    import hashlib

    from data_agent.audit_draft import write_trace

    client, session_id = _session_with_sales(tmp_path, monkeypatch)
    workspace = api.registry.get(session_id).workspace
    draft = "订单数：2 笔"
    original = {
        "parsed_draft": draft,
        "parsed_draft_sha256": hashlib.sha256(draft.encode()).hexdigest(),
    }
    trace_sha, _ = write_trace(workspace.root / "draft_traces", original)

    response = client.post(
        f"/api/sessions/{session_id}/sales-audit",
        json={"draft": "订单数：2 笔\n市场份额：37%", "draft_trace_sha256": trace_sha},
    )
    assert response.status_code == 422
    assert "轨迹" in response.json()["detail"]


def _write_brief(tmp_path, text: str):
    path = tmp_path / "brief-trace.md"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_ledger_reading_edge_cases(tmp_path):
    path = tmp_path / "audit_ledger.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    assert read_entries(path) == []

    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        read_entries(path)
    assert verify_ledger(path)["valid"] is False

    path.write_text("{not json\n", encoding="utf-8")
    assert verify_ledger(path)["valid"] is False

    # 账本路径是目录：读取失败要转成 LedgerError，而不是裸 OSError。
    directory = tmp_path / "as_dir.jsonl"
    directory.mkdir()
    with pytest.raises(LedgerError):
        read_entries(directory)


def test_sequence_and_prev_tampering_are_reported(tmp_path):
    _append(tmp_path)
    _append(tmp_path)
    path = tmp_path / "audit_ledger.jsonl"
    entries = read_entries(path)
    entries[1]["seq"] = 9
    entries[1]["prev"] = "f" * 64
    report = verify_entries(entries)
    assert not report["valid"]
    assert any("序号" in error for error in report["errors"])
    assert any("prev" in error for error in report["errors"])


def test_append_refuses_chain_with_wrong_hash(tmp_path):
    _append(tmp_path)
    path = tmp_path / "audit_ledger.jsonl"
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["hash"] = "e" * 64
    path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="链校验失败"):
        _append(tmp_path)


def test_append_reports_unwritable_ledger(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(LedgerError, match="无法(写入|读取)账本|签名密钥不可用"):
        _append(blocker)


def test_verify_trace_rejects_malformed_inputs(tmp_path):
    from data_agent.audit_draft import QuickDraftError

    trace_dir = tmp_path / "draft_traces"
    with pytest.raises(QuickDraftError, match="格式"):
        verify_trace(trace_dir, "not-a-hash", "订单数：1 笔")
    with pytest.raises(QuickDraftError, match="找不到"):
        verify_trace(trace_dir, "a" * 64, "订单数：1 笔")

    trace_dir.mkdir(parents=True, exist_ok=True)
    target = trace_dir / f"{'b' * 64}.json"
    target.write_text("{broken", encoding="utf-8")
    with pytest.raises(QuickDraftError, match="不一致"):
        verify_trace(trace_dir, "b" * 64, "订单数：1 笔")

    import hashlib

    payload = json.dumps([]).encode("utf-8")
    target = trace_dir / f"{hashlib.sha256(payload).hexdigest()}.json"
    target.write_bytes(payload)
    with pytest.raises(QuickDraftError, match="格式"):
        verify_trace(trace_dir, hashlib.sha256(payload).hexdigest(), "订单数：1 笔")

    broken = b"\xff\xfe\x00"
    target = trace_dir / f"{hashlib.sha256(broken).hexdigest()}.json"
    target.write_bytes(broken)
    with pytest.raises(QuickDraftError, match="无法解析"):
        verify_trace(trace_dir, hashlib.sha256(broken).hexdigest(), "订单数：1 笔")


def test_confirm_and_ledger_error_paths(tmp_path, monkeypatch):
    client, session_id = _session_with_sales(tmp_path, monkeypatch)
    draft = "订单数：2 笔"
    confirm_url = f"/api/sessions/{session_id}/sales-audit/confirm"

    blank = client.post(confirm_url, json={"draft": " ", "audit_id": "aud-" + "0" * 8})
    assert blank.status_code == 422

    bad_trace = client.post(
        confirm_url,
        json={"draft": draft, "audit_id": "aud-" + "0" * 8, "draft_trace_sha256": "0" * 64},
    )
    assert bad_trace.status_code == 422

    import data_agent.sales_audit as sales_audit

    monkeypatch.setattr(
        sales_audit, "audit_sales_brief", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))
    )
    assert (
        client.post(confirm_url, json={"draft": draft, "audit_id": "aud-" + "0" * 8}).status_code
        == 422
    )

    monkeypatch.setattr(
        sales_audit, "audit_sales_brief", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    assert (
        client.post(confirm_url, json={"draft": draft, "audit_id": "aud-" + "0" * 8}).status_code
        == 422
    )

    audit_id = "aud-" + "1" * 8
    monkeypatch.setattr(
        sales_audit,
        "audit_sales_brief",
        lambda *a, **k: {"audit_id": audit_id, "corrected_brief": None},
    )
    no_brief = client.post(confirm_url, json={"draft": draft, "audit_id": audit_id})
    assert no_brief.status_code == 422

    import data_agent.routers.audit_ledger as ledger_router

    monkeypatch.setattr(
        ledger_router, "append_entry", lambda *a, **k: (_ for _ in ()).throw(LedgerError("ledger"))
    )
    monkeypatch.setattr(
        sales_audit,
        "audit_sales_brief",
        lambda *a, **k: {
            "audit_id": audit_id,
            "corrected_brief": "# ok\n",
            "source": {"sha256": "a" * 64, "filename": "sales.csv"},
            "summary": {"verified": 1},
        },
    )
    assert client.post(confirm_url, json={"draft": draft, "audit_id": audit_id}).status_code == 503

    monkeypatch.setattr(
        ledger_router, "recent_entries", lambda *a, **k: (_ for _ in ()).throw(LedgerError("read"))
    )
    assert client.get(f"/api/sessions/{session_id}/sales-audit/ledger").status_code == 503


def test_confirm_requires_uploaded_csv(tmp_path, monkeypatch):
    client, _session_id = _session_with_sales(tmp_path, monkeypatch)
    imported = client.post(
        "/api/sessions",
        files={"file": ("notes.json", b'{"a": 1}', "application/json")},
    )
    session_id = imported.json()["id"]
    response = client.post(
        f"/api/sessions/{session_id}/sales-audit/confirm",
        json={"draft": "订单数：1 笔", "audit_id": "aud-" + "0" * 8},
    )
    assert response.status_code == 422


def test_oversized_sales_is_rejected_instead_of_500(tmp_path):
    """10^26 元曾在 Decimal 除法里抛 InvalidOperation（500），必须变成可读 422。"""
    from data_agent.sales_audit import audit_sales_brief

    path = tmp_path / "sales.csv"
    path.write_text(
        "order_id,sales,quantity\nX,1000000000000000000000000000,1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="金额过大"):
        audit_sales_brief(path, "订单数：1 笔")


def test_lock_key_is_stable_before_and_after_file_exists(tmp_path):
    """Windows 的 resolve() 在文件出现后会加 \\?\ 前缀，锁键必须保持不变。"""
    path = tmp_path / "audit_ledger.jsonl"
    before = _lock_key(path)
    path.write_text("", encoding="utf-8")
    assert _lock_key(path) == before


def test_concurrent_first_appends_keep_the_chain_valid(tmp_path):
    """多个线程同时向空账本首写，不能出现重复 seq 或断链。"""
    from concurrent.futures import ThreadPoolExecutor

    for round_number in range(5):
        root = tmp_path / f"round-{round_number}"
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(
                pool.map(
                    lambda index, root=root: append_entry(
                        root,
                        kind="audit",
                        session_id="s",
                        payload={"index": index},
                    ),
                    range(20),
                )
            )
        entries = read_entries(root / "audit_ledger.jsonl")
        assert sorted(entry["seq"] for entry in entries) == list(range(1, 21))
        assert verify_entries(entries)["valid"] is True


def test_ledger_roundtrips_unicode_line_separators(tmp_path):
    """字段里的 U+2028 是合法 JSON 字符，不能被 splitlines 当成换行。"""
    _append(tmp_path, source_filename="a b.csv", note="x y\u0085z")
    entries = read_entries(tmp_path / "audit_ledger.jsonl")
    assert entries[0]["source_filename"] == "a b.csv"
    assert verify_entries(entries)["valid"] is True
