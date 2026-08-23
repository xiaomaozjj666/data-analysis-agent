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
import logging
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
# LangSmith tracing 后台上报碰到免费额度限流时会刷 429 warning，
# 不影响任何分析功能，降为 ERROR 级别避免淹没业务日志。
logging.getLogger("langsmith").setLevel(logging.ERROR)
logging.getLogger("langsmith.client").setLevel(logging.ERROR)

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


def _mount_frontend_assets() -> None:
    """挂载前端构建产物中的 /assets 静态目录（仅当真实 dist 存在时）。

    与 catch-all 路由解耦：catch-all 在 import 时**无条件**注册（保证测试与
    运行期行为一致），而 /assets 仅在磁盘上确有产物时才挂载，避免对不存在的
    目录调用 StaticFiles（会抛异常）。
    """
    assets_dir = frontend_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")


# catch-all 路由在所有 /api/... 路由之后注册（模块加载时即完成），保证 API 端点优先匹配。
# 注意：dist 是否存在**不**应影响路由注册——handler 在请求时读取模块级 ``frontend_dist``
# 全局，因此测试可通过 ``monkeypatch.setattr(api, "frontend_dist", tmp)`` 注入临时 dist，
# 运行期 dist 缺失时也只会导致 SPA 回退 404，而不会让整条路由消失（回归保护）。
@app.get("/{full_path:path}", include_in_schema=False)
def frontend_app(full_path: str) -> Response:
    if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
        raise HTTPException(status_code=404, detail="Not found")
    dist = frontend_dist  # 请求时取值，支持测试 monkeypatch 覆盖
    if not dist.is_dir():
        # 前端未构建（如仅运行后端、dist 未生成）：不提供 SPA 回退，避免对缺失文件
        # 调用 FileResponse 触发 500。API 路由不受影响（已在 catch-all 之前匹配）。
        raise HTTPException(status_code=404, detail="Not found")
    raw_path = dist / full_path
    if raw_path.is_symlink():
        raise HTTPException(status_code=404, detail="Not found")
    requested = raw_path.resolve()
    if dist.resolve() in requested.parents and requested.is_file():
        # 带内容的静态资源（JS/CSS/图片）文件名含 hash，可长期缓存
        return FileResponse(requested, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    # index.html 必须禁止缓存，否则浏览器加载旧 HTML 引用的旧 JS 文件名
    return FileResponse(
        dist / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


_mount_frontend_assets()
