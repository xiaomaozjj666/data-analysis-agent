"""「直接问模型」快速起草路由。

- POST /api/sessions/{id}/sales-audit/quick-draft：读会话原始 CSV，让模型不调用
  任何工具、关闭思考，直接写出三个指标行，作为核验器的对照输入。

业务逻辑与诚实边界见 ``data_agent.audit_draft``；本模块只做 HTTP 映射。
全局速率限制由中间件统一处理，同一会话 10 秒内的重复调用在业务层返回 429。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/api/sessions/{session_id}/sales-audit/quick-draft")
def quick_draft(session_id: str) -> dict[str, Any]:
    """让模型直接读表写出三个指标行（不调工具、不思考），返回过滤后的草稿。"""
    from data_agent import api
    from data_agent.audit_draft import (
        TRACE_DIR_NAME,
        QuickDraftError,
        resolve_source,
        run_quick_draft,
    )

    workspace = api.registry.get(session_id).workspace
    try:
        source = resolve_source(workspace)
        return run_quick_draft(
            session_id,
            source,
            api._effective_settings(),
            trace_dir=workspace.root / TRACE_DIR_NAME,
        )
    except QuickDraftError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=headers
        ) from exc
