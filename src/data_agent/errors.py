"""统一的错误响应辅助。

提供：

- ``api_error(status_code, detail)``：构造 ``HTTPException`` 的薄封装，供路由
  层用一致的入口抛业务错误（响应体仍为 FastAPI 默认的 ``{"detail": ...}``，
  不改变既有状态码与文案）。
- ``register_error_handlers(app)``：注册兜底的 ``Exception`` 处理器，把未捕获
  的异常统一成 500 ``{"detail": "内部错误", "status": "error"}``，避免向客户端
  泄露内部栈信息。``HTTPException`` 由 Starlette 默认处理器响应，不受影响。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def api_error(status_code: int, detail: str) -> HTTPException:
    """Build an ``HTTPException`` with the given status code and detail."""
    return HTTPException(status_code=status_code, detail=detail)


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on ``app``.

    Only the catch-all ``Exception`` handler is registered here so that truly
    unhandled errors return a stable 500 envelope. ``HTTPException`` keeps using
    Starlette's default handler (``{"detail": ...}`` with the exception's
    status code), preserving existing error contracts.
    """

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:  # type: ignore[no-untyped-def]
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "内部错误", "status": "error"},
        )
