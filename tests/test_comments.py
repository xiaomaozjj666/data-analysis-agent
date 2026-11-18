"""会话评论：校验、原子落盘与 API 协作流。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_agent import api, comments


def test_comments_roundtrip_and_validation(tmp_path):
    added = comments.add_comment(tmp_path, "  收入\n口径  需要确认 ", author="  小王  ")
    assert added["text"] == "收入 口径 需要确认"
    assert added["author"] == "小王"
    assert comments.list_comments(tmp_path)[0]["id"] == added["id"]

    with pytest.raises(ValueError, match="不能为空"):
        comments.add_comment(tmp_path, "   ")
    with pytest.raises(ValueError, match="不能超过"):
        comments.add_comment(tmp_path, "x" * (comments.MAX_TEXT_CHARS + 1))

    assert comments.delete_comment(tmp_path, added["id"]) is True
    assert comments.delete_comment(tmp_path, added["id"]) is False
    assert comments.list_comments(tmp_path) == []


def test_comments_limit_and_corrupt_file(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(comments, "MAX_COMMENTS", 1)
    comments.add_comment(tmp_path, "first")
    with pytest.raises(ValueError, match="最多保留"):
        comments.add_comment(tmp_path, "second")

    (tmp_path / comments.COMMENTS_FILENAME).write_text("{broken", encoding="utf-8")
    caplog.set_level("WARNING", logger=comments._log.name)
    assert comments.list_comments(tmp_path) == []
    assert any("cannot read comments" in record.message for record in caplog.records)


def test_comments_api_flow(monkeypatch):
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("APP_VIEWER_TOKEN", raising=False)
    client = TestClient(api.app)
    created = client.post("/api/sessions/sample")
    assert created.status_code == 201
    session_id = created.json()["id"]

    assert client.get(f"/api/sessions/{session_id}/comments").json()["comments"] == []
    posted = client.post(
        f"/api/sessions/{session_id}/comments",
        json={"text": "这条结论的统计口径需要复核。"},
    )
    assert posted.status_code == 201
    comment = posted.json()
    assert comment["text"].startswith("这条结论")
    listed = client.get(f"/api/sessions/{session_id}/comments").json()["comments"]
    assert [item["id"] for item in listed] == [comment["id"]]

    assert client.delete(f"/api/sessions/{session_id}/comments/{comment['id']}").status_code == 200
    assert client.delete(f"/api/sessions/{session_id}/comments/{comment['id']}").status_code == 404


def test_comments_survive_export_zip(monkeypatch):
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    client = TestClient(api.app)
    session_id = client.post("/api/sessions/sample").json()["id"]
    client.post(f"/api/sessions/{session_id}/comments", json={"text": "导出也要带上我"})
    response = client.get(f"/api/sessions/{session_id}/export")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert comments.COMMENTS_FILENAME.encode() in response.content


def test_comments_non_array_file_is_ignored(tmp_path, caplog):
    (tmp_path / comments.COMMENTS_FILENAME).write_text('{"not": "array"}', encoding="utf-8")
    caplog.set_level("WARNING", logger=comments._log.name)
    assert comments.list_comments(tmp_path) == []
    assert any("not a JSON array" in record.message for record in caplog.records)
