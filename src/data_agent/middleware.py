"""HTTP 中间件：CORS、GZip 压缩、访问令牌、滑动窗口速率限制与版本头。

``setup_middleware(app)`` 把上述中间件装配到 FastAPI 应用上。速率限制相关
的可变状态（``request_buckets``、``request_buckets_lock``、
``MAX_RATE_LIMIT_BUCKETS``、``_trusted_proxy_hops``）定义在本模块，由
``api.py`` re-export；限流函数内部通过 ``api.<name>`` 访问这些状态，以便
测试用 ``monkeypatch.setattr(api, "request_buckets", ...)`` 等替换时能被
此处感知（与路由层访问 ``api.registry`` 同理）。
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _check_access(request: Request) -> None:
    expected = os.getenv("APP_ACCESS_TOKEN", "").strip()
    if not expected:
        return
    provided = request.headers.get("x-app-token", "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:]
    if not provided:
        provided = request.headers.get("authorization", "")
        if provided.lower().startswith("bearer "):
            provided = provided[7:]
    if not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=401, detail="需要有效的应用访问令牌。")


# Rate-limit bucket dictionary hard cap. An attacker spoofing X-Forwarded-For
# can otherwise manufacture unlimited unique keys and OOM the process. 10K
# entries × ~60 floats each stays under 5MB and is plenty for legitimate load.
MAX_RATE_LIMIT_BUCKETS = 10_000

# Number of trusted reverse-proxy hops in front of this process. Each trusted
# proxy appends the IP it received the request from to X-Forwarded-For. We
# therefore trust the Nth-from-last entry; taking the leftmost entry when no
# proxy is declared would let attackers forge the header and bypass limits.
# Default 0 means "direct exposure, ignore XFF"; set 1 for Render/Nginx.
_trusted_proxy_hops = max(0, int(os.getenv("DATA_AGENT_TRUSTED_PROXY_HOPS", "0")))

request_buckets: dict[str, deque[float]] = defaultdict(deque)
request_buckets_lock = threading.Lock()


def _client_identifier(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Only consults ``X-Forwarded-For`` when ``DATA_AGENT_TRUSTED_PROXY_HOPS`` is
    set, taking the Nth-from-last entry so an attacker cannot forge a header
    to spin up fresh identities. Falls back to the direct socket address so
    the limiter always has a stable key.
    """
    # 通过 ``api._trusted_proxy_hops`` 访问，兼容测试 monkeypatch。
    from data_agent import api

    if api._trusted_proxy_hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            parts = [item.strip() for item in forwarded.split(",") if item.strip()]
            if len(parts) >= api._trusted_proxy_hops:
                return parts[-api._trusted_proxy_hops]
            # XFF has fewer entries than declared hops — likely an internal
            # health check that bypassed the proxy. Fall through to direct host.
    if request.client:
        return request.client.host
    return "unknown"


def _prune_rate_buckets_locked(now: float) -> None:
    """Bound the rate-limit dict size. Caller must hold request_buckets_lock."""
    # 通过 ``api.*`` 访问，兼容测试 monkeypatch（request_buckets / MAX_RATE_LIMIT_BUCKETS）。
    from data_agent import api

    if len(api.request_buckets) < api.MAX_RATE_LIMIT_BUCKETS:
        return
    # Drop buckets with no activity in the last 60s (the common case).
    stale = [
        key for key, bucket in api.request_buckets.items()
        if not bucket or now - bucket[-1] >= 60
    ]
    for key in stale:
        del api.request_buckets[key]
    # If still over the cap, evict the oldest 25% by last-seen timestamp to
    # amortise the cost across many writes instead of scanning every request.
    if len(api.request_buckets) >= api.MAX_RATE_LIMIT_BUCKETS:
        ordered = sorted(
            api.request_buckets.items(),
            key=lambda kv: kv[1][-1] if kv[1] else 0.0,
        )
        excess = len(api.request_buckets) - api.MAX_RATE_LIMIT_BUCKETS // 2
        for key, _ in ordered[:max(excess, 0)]:
            del api.request_buckets[key]


def _check_rate_limit(request: Request) -> None:
    if request.method not in {"POST", "PUT", "DELETE"} or not request.url.path.startswith("/api/"):
        return
    # 通过 ``api.*`` 访问，兼容测试 monkeypatch（bootstrap_settings / request_buckets）。
    from data_agent import api

    limit = api.bootstrap_settings.rate_limit_per_minute
    now = time.monotonic()
    key = _client_identifier(request)
    with api.request_buckets_lock:
        _prune_rate_buckets_locked(now)
        bucket = api.request_buckets[key]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试。")
        bucket.append(now)


def setup_middleware(app) -> None:  # type: ignore[no-untyped-def]
    """Attach CORS、GZip、安全头与速率限制中间件到 ``app``。"""
    from data_agent import api

    # Warn on empty access token in non-local deployments (Render, Docker, etc.).
    # Local dev (uvicorn with no APP_ACCESS_TOKEN) is intentionally unauthenticated.
    if not os.getenv("APP_ACCESS_TOKEN", "").strip():
        if os.getenv("RENDER") or os.getenv("DATA_AGENT_STORAGE_BACKEND") == "s3":
            logger.warning(
                "APP_ACCESS_TOKEN is empty in a production-like environment. "
                "Set APP_ACCESS_TOKEN to enable API authentication."
            )

    allowed_origins = [
        item.strip()
        for item in os.getenv(
            "DATA_AGENT_CORS_ORIGINS",
            "http://127.0.0.1:5173,http://localhost:5173",
        ).split(",")
        if item.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def protect_api(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = None
        # CORS preflight requests do not carry the application token. Let
        # CORSMiddleware answer OPTIONS before enforcing auth on the real request.
        if (
            request.method != "OPTIONS"
            and request.url.path.startswith("/api/")
            and request.url.path not in {"/api/health", "/api/auth"}
        ):
            try:
                _check_access(request)
                _check_rate_limit(request)
            except HTTPException as exc:
                response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        if response is None:
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        response.headers.setdefault("X-API-Version", api.API_VERSION)
        return response
