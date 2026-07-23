"""FastAPI 应用工厂：装配中间件、错误处理器、路由与前端静态资源。

本模块从原单体 ``api.py`` 拆分而来，仅保留应用创建与装配逻辑：

- ``app``：FastAPI 实例，由 ``setup_middleware`` / ``register_error_handlers`` /
  ``include_router`` 装配中间件、错误处理器与各路由模块。
- 前端 SPA 静态资源挂载与 catch-all fallback 路由。
- 从拆分模块 re-export 全部共享单例与辅助函数，保持
  ``data_agent.api.<name>`` 访问路径不变，兼容测试的 ``monkeypatch.setattr(api, ...)``
  以及 ``from data_agent.api import SessionRegistry`` 等既有导入。

拆分目标：
- ``data_agent.registry``：``SessionRegistry`` / ``SessionRecord`` / 单例 /
  产物与会话载荷构造 / 请求模型 / 版本常量。
- ``data_agent.middleware``：CORS / GZip / 访问令牌 / 滑动窗口速率限制 / 安全头。
- ``data_agent.errors``：统一错误响应辅助。
- ``data_agent.routers.{sessions,analysis,artifacts,settings}``：按域拆分的路由。
"""

from __future__ import annotations

import asyncio  # noqa: F401 — 测试通过 data_agent.api.asyncio.wait_for 打补丁
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

# Agent 类需经 api.DataAnalysisAgent 访问，以便测试 monkeypatch 注入 FakeAgent。
from data_agent.agent import DataAnalysisAgent  # noqa: F401
from data_agent.errors import register_error_handlers
from data_agent.middleware import (
    MAX_RATE_LIMIT_BUCKETS,  # noqa: F401
    _check_access,  # noqa: F401
    _check_rate_limit,  # noqa: F401
    _client_identifier,  # noqa: F401
    _prune_rate_buckets_locked,  # noqa: F401
    _trusted_proxy_hops,  # noqa: F401
    request_buckets,  # noqa: F401
    request_buckets_lock,  # noqa: F401
    setup_middleware,
)

# 共享单例、请求模型、辅助函数、版本常量 —— 由 registry 持有并在 import 时初始化。
# 所有 re-export 项标注 noqa: F401，避免被 ruff 误判为未使用导入而移除。
from data_agent.registry import (
    _SAMPLE_SALES_CSV,  # noqa: F401
    API_VERSION,  # noqa: F401
    API_VERSION_INT,  # noqa: F401
    MIN_CLIENT_VERSION,  # noqa: F401
    SSE_QUEUE_MAXSIZE,  # noqa: F401
    AnalyzeRequest,  # noqa: F401
    ChartEditRequest,  # noqa: F401
    SessionRecord,  # noqa: F401
    SessionRegistry,  # noqa: F401
    SettingsUpdate,  # noqa: F401
    _artifact_file,  # noqa: F401
    _artifact_payload,  # noqa: F401
    _effective_settings,  # noqa: F401
    _elapsed_seconds,  # noqa: F401
    _history,  # noqa: F401
    _result_payload,  # noqa: F401
    _save_runtime_api_key,  # noqa: F401
    analysis_slots,  # noqa: F401
    bootstrap_settings,  # noqa: F401
    registry,  # noqa: F401
    runtime_settings,  # noqa: F401
    runtime_settings_lock,  # noqa: F401
    session_storage,  # noqa: F401
)
from data_agent.routers import analysis, artifacts, sessions
from data_agent.routers import settings as settings_router

# 图表预览辅助需经 api._inline_echarts_bundle 访问（test_echarts_engine 直接调用）。
from data_agent.routers.artifacts import (
    _harden_preview_document,  # noqa: F401
    _inline_echarts_bundle,  # noqa: F401
    _inline_plotly_bundle,  # noqa: F401
)

# ---------------------------------------------------------------------------
# FastAPI 应用装配。
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Data Analysis Agent API",
    version="2.0.0",
    description="Plan-and-Execute + ReAct data analysis service",
)

setup_middleware(app)
register_error_handlers(app)

app.include_router(sessions.router)
app.include_router(analysis.router)
app.include_router(artifacts.router)
app.include_router(settings_router.router)


# ---------------------------------------------------------------------------
# 前端 SPA 静态资源挂载与 catch-all fallback。
# 静态资源（JS/CSS/图片）文件名含 hash，可长期缓存；index.html 必须禁止缓存，
# 否则浏览器加载旧 HTML 引用的旧 JS 文件名，导致用户看到过期版本。
# catch-all 路由必须注册在所有 /api/... 路由之后，确保 API 端点优先匹配。
# ---------------------------------------------------------------------------
frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    assets_dir = frontend_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_app(full_path: str) -> Response:
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            raise HTTPException(status_code=404, detail="Not found")
        requested = (frontend_dist / full_path).resolve()
        if frontend_dist.resolve() in requested.parents and requested.is_file():
            # 带内容的静态资源（JS/CSS/图片）文件名含 hash，可长期缓存
            return FileResponse(requested, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        # index.html 必须禁止缓存，否则浏览器加载旧 HTML 引用的旧 JS 文件名
        return FileResponse(
            frontend_dist / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
