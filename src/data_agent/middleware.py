"""HTTP 中间件：CORS、GZip 压缩、访问令牌、滑动窗口速率限制与版本头。

``setup_middleware(app)`` 把上述中间件装配到 FastAPI 应用上。速率限制相关
的可变状态（``request_buckets``、``request_buckets_lock``、
``MAX_RATE_LIMIT_BUCKETS``、``trusted_proxy_hops()``）定义在本模块，由
``api.py`` re-export；限流函数内部通过 ``api.<name>`` 访问这些状态，以便
测试用 ``monkeypatch.setattr(api, "request_buckets", ...)`` 等替换时能被
此处感知（与路由层访问 ``api.registry`` 同理）。
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import sys
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _resolve_bind_host() -> str:
    """从 uvicorn 命令行推断监听地址，供「空 token 只允许环回」的启动校验使用。

    Dockerfile、Render Blueprint 与 run.ps1 都通过 uvicorn CLI 显式传入
    ``--host``；命令行里没有 ``--host`` 时取 uvicorn 的默认值 ``127.0.0.1``。
    """
    argv = sys.argv[1:]
    for index, item in enumerate(argv):
        if item == "--host" and index + 1 < len(argv):
            return argv[index + 1].strip()
        if item.startswith("--host="):
            return item.split("=", 1)[1].strip()
    return "127.0.0.1"


def _is_loopback_host(host: str) -> bool:
    """``localhost`` / 127.0.0.0/8 / ::1 视为环回；无法解析的主机名按非环回处理。"""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


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
    # 编码成 bytes 再比较：hmac.compare_digest 对含非 ASCII 字符的 str 会抛
    # TypeError（"comparing strings with non-ASCII characters is not
    # supported"），恶意构造的令牌头会变成 500 而不是 401。按字节比较没有
    # 这个限制，时序安全性也不变。
    if not hmac.compare_digest(provided.strip().encode("utf-8"), expected.encode("utf-8")):
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
_hops_warning_shown = False


def trusted_proxy_hops() -> int:
    """Trusted XFF hops，每次调用时读环境变量。

    不能在导入期求值：``load_dotenv()`` 在 config 导入链里晚于本模块，
    导入期读取会让只写在 ``.env`` 里的该变量被静默忽略——反代后部署若只配
    .env，限流键会退化为代理 IP，全部客户端共享同一个限流桶。
    （与 ``sql_guard.query_time_budget_ms`` 同一套按调用求值的约定。）

    非法值（非数字）回退 0 并告警一次，而不是让每个请求 500：导入期求值
    时代码写错配置是"启动即炸"（好在部署时就能发现），改调用期后同样的
    错误会变成"首个请求 500"——更难排查，且按 0 回退正是安全默认
    （不信任 XFF，退化为直连 socket 键）。
    """
    global _hops_warning_shown
    raw = os.getenv("DATA_AGENT_TRUSTED_PROXY_HOPS", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        if not _hops_warning_shown:
            _hops_warning_shown = True
            logger.warning(
                "DATA_AGENT_TRUSTED_PROXY_HOPS=%r 不是合法整数，按 0 处理（不信任 X-Forwarded-For）",
                raw,
            )
        return 0

request_buckets: dict[str, deque[float]] = defaultdict(deque)
request_buckets_lock = threading.Lock()


def _client_identifier(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Only consults ``X-Forwarded-For`` when ``DATA_AGENT_TRUSTED_PROXY_HOPS`` is
    set, taking the Nth-from-last entry so an attacker cannot forge a header
    to spin up fresh identities. Falls back to the direct socket address so
    the limiter always has a stable key.
    """
    # 通过 ``api.trusted_proxy_hops`` 访问，兼容测试 monkeypatch。
    from data_agent import api

    hops = api.trusted_proxy_hops()
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            parts = [item.strip() for item in forwarded.split(",") if item.strip()]
            if len(parts) >= hops:
                return parts[-hops]
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
        key for key, bucket in api.request_buckets.items() if not bucket or now - bucket[-1] >= 60
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
        for key, _ in ordered[: max(excess, 0)]:
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

    # 未配置访问令牌时只允许绑定环回地址：0.0.0.0 / 公网 IP 上裸奔等于把
    # runs/ 下全部会话公开。Dockerfile、Render 与 run.ps1 都显式传 --host，
    # 因此启动时即可判定（见 _resolve_bind_host）。
    access_token = os.getenv("APP_ACCESS_TOKEN", "").strip()
    if not access_token:
        bind_host = _resolve_bind_host()
        if not _is_loopback_host(bind_host):
            raise RuntimeError(
                f"APP_ACCESS_TOKEN 未设置，但服务监听地址 {bind_host!r} 不是环回地址。"
                "请设置 APP_ACCESS_TOKEN 或把绑定地址改为 127.0.0.1。"
            )
        # 生产类环境即使绑在环回也提示（可能是反向代理背后只绑本机）。
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
