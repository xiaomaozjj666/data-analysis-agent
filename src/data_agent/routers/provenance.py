"""分析溯源查询与导出路由。

- GET /api/sessions/{id}/provenance：摘要 + 当场复核结果。
- GET /api/sessions/{id}/provenance/export：自包含 JSON（摘要、全部条目、复核结果）。

溯源写在会话根目录（``provenance.jsonl`` / ``provenance.json``），由分析完成
时自动生成，见 ``data_agent.provenance``。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from data_agent import provenance

router = APIRouter()


def _root(session_id: str):
    from data_agent import api

    record = api.registry.get(session_id)
    return record.workspace.root


@router.get("/api/sessions/{session_id}/provenance")
def get_provenance(session_id: str) -> dict:
    root = _root(session_id)
    summary = provenance.load_summary(root)
    if summary is None:
        raise HTTPException(status_code=404, detail="当前会话还没有溯源记录，请先完成一次分析。")
    return {"summary": summary, "verification": provenance.verify(root)}


@router.get("/api/sessions/{session_id}/provenance/export")
def export_provenance(session_id: str) -> Response:
    root = _root(session_id)
    payload = provenance.export_payload(root)
    if payload["summary"] is None:
        raise HTTPException(status_code=404, detail="当前会话还没有溯源记录，请先完成一次分析。")
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="provenance.json"'},
    )
