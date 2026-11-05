"""低基数、无敏感载荷的 HTTP Prometheus 指标。"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# 独立 registry：不导出进程环境、Python GC 或平台信息，/metrics 只包含
# 下面三个明确审查过的 HTTP 指标。
METRICS_REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter(
    "data_agent_http_requests_total",
    "HTTP 请求总数（按路由模板、方法与状态码）。",
    ("method", "route", "status"),
    registry=METRICS_REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "data_agent_http_request_duration_seconds",
    "HTTP 请求端到端耗时。",
    ("method", "route"),
    registry=METRICS_REGISTRY,
    buckets=(
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        float("inf"),
    ),
)
HTTP_REQUEST_ERRORS = Counter(
    "data_agent_http_request_errors_total",
    "HTTP 4xx/5xx 响应总数；与 requests_total 配比即错误率。",
    ("method", "route", "status"),
    registry=METRICS_REGISTRY,
)


def observe_http_request(method: str, route: str, status: int, duration_seconds: float) -> None:
    """记录一次请求；route 必须是路由模板，禁止传入原始 URL。"""
    status_label = str(status)
    HTTP_REQUESTS.labels(method=method, route=route, status=status_label).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(max(0.0, duration_seconds))
    if status >= 400:
        HTTP_REQUEST_ERRORS.labels(method=method, route=route, status=status_label).inc()


def render_metrics() -> tuple[bytes, str]:
    """生成 Prometheus 文本 exposition 载荷及 Content-Type。"""
    return generate_latest(METRICS_REGISTRY), CONTENT_TYPE_LATEST
