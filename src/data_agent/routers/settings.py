"""健康检查、版本、认证状态与运行时设置路由。

- GET  /api/health：服务健康检查。
- GET  /api/version：API 版本与最低兼容客户端版本。
- GET  /api/auth：访问令牌认证状态。
- GET  /api/storage/health：对象存储后端健康检查。
- GET  /api/settings：当前运行时配置。
- PUT  /api/settings：更新 API Key / thinking / reasoning_effort。
- DELETE /api/settings/key：清除已保存的 API Key。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from langsmith import utils as langsmith_utils

from data_agent._runtime_config import has_runtime_scope, update_runtime_settings
from data_agent.config import DEEPSEEK_BASE_URL
from data_agent.credentials import delete_saved_api_key
from data_agent.middleware import _check_access, _is_loopback_host
from data_agent.observability import render_metrics
from data_agent.registry import SettingsUpdate

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request) -> Response:
    """默认仅环回客户端可抓取；可用环境变量完全关闭。"""
    if os.getenv("DATA_AGENT_METRICS_ENABLED", "true").strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        raise HTTPException(status_code=404, detail="Not found")
    host = request.client.host if request.client else ""
    # Starlette 的本地测试传输使用 testclient 伪主机；真实网络请求不会
    # 出现该值。生产路径仍严格要求 IP/localhost 环回。
    if host != "testclient" and not _is_loopback_host(host):
        raise HTTPException(status_code=404, detail="Not found")
    payload, media_type = render_metrics()
    return Response(content=payload, media_type=media_type)


@router.get("/api/health")
def health() -> dict[str, str]:
    from data_agent import api

    return {
        "status": "ok",
        "architecture": "plan-and-execute-react",
        "storage_backend": api.session_storage.backend,
        "persistent_storage": str(api.session_storage.persistent).lower(),
    }


@router.get("/api/version")
def api_version() -> dict[str, str]:
    from data_agent import api

    return {"version": api.API_VERSION, "min_client": api.MIN_CLIENT_VERSION}


@router.get("/api/auth")
def auth_status(request: Request) -> dict[str, bool]:
    full = bool(os.getenv("APP_ACCESS_TOKEN", "").strip())
    viewer = bool(os.getenv("APP_VIEWER_TOKEN", "").strip())
    if not full and not viewer:
        return {"required": False, "authenticated": True}
    try:
        _check_access(request)
    except HTTPException:
        return {"required": True, "authenticated": False}
    return {"required": True, "authenticated": True}


@router.get("/api/storage/health")
def storage_health() -> dict[str, str | bool]:
    from data_agent import api

    return api.session_storage.healthcheck()


@router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    from data_agent import api

    settings = api._effective_settings()
    storage_status = api.session_storage.healthcheck()
    tracing = langsmith_utils.tracing_is_enabled()
    if tracing is True:
        key = langsmith_utils.get_api_key(None)
        if key is None:
            # SDK profiles and self-hosted endpoints can work without an environment key.
            langsmith_status = "key_unconfirmed"
        else:
            langsmith_status = "missing_key" if key == "your-langsmith-api-key" else "configured"
    else:
        langsmith_status = "local" if tracing == "local" else "disabled"
    return {
        "provider": "deepseek",
        "model": settings.model,
        "base_url": settings.base_url,
        "configured": bool(settings.api_key),
        "thinking_enabled": settings.thinking_enabled,
        "reasoning_effort": settings.reasoning_effort,
        "langsmith_tracing": tracing is True,
        "langsmith_status": langsmith_status,
        "langsmith_project": langsmith_utils.get_tracer_project(),
        "storage_backend": api.session_storage.backend,
        "persistent_storage": api.session_storage.persistent,
        "storage_status": storage_status.get("status", "unknown"),
        "storage_message": storage_status.get("message", ""),
        # 上传大小上限：前端据此做客户端预校验，避免与服务端配置脱节
        # （服务端可通过 DATA_AGENT_MAX_UPLOAD_BYTES 调整）。
        "max_upload_bytes": api.bootstrap_settings.max_upload_bytes,
        # 执行预算：前端 Settings 类型早就声明了 max_iterations / max_plan_steps，
        # 但接口从未下发（类型与运行时不一致，读到就是 undefined）。这里补齐，
        # 并带上决定"一轮分析跑多久"的两个上限——用户抱怨卡顿时能看到实际配置。
        "max_iterations": settings.max_iterations,
        "max_plan_steps": settings.max_plan_steps,
        "max_tool_calls_per_step": settings.max_tool_calls_per_step,
    }


@router.post("/api/settings/test")
async def test_model_connection(request: Request) -> dict[str, str]:
    from data_agent import api

    # Avoid validation responses containing the submitted plaintext credential.
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="请求格式错误。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="请求格式错误。")
    submitted = payload.get("api_key")
    if submitted is not None and not isinstance(submitted, str):
        raise HTTPException(status_code=422, detail="API Key 格式不正确。")
    settings = api._effective_settings()
    key = submitted.strip() if submitted is not None else settings.api_key
    if not key:
        raise HTTPException(status_code=422, detail="请先输入 DeepSeek API Key。")
    if len(key) > 2048 or not all(33 <= ord(char) <= 126 for char in key):
        raise HTTPException(status_code=422, detail="API Key 格式不正确。")
    base_url = (settings.base_url or DEEPSEEK_BASE_URL).rstrip("/")
    try:
        async with asyncio.timeout(8), httpx.AsyncClient(timeout=7) as client:
            response = await client.get(
                f"{base_url}/models", headers={"Authorization": f"Bearer {key}"}
            )
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=504, detail="连接模型服务超时，请检查网络后重试。") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="无法访问模型服务，请检查网络。") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="模型服务拒绝了此 API Key，请核对后重试。")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="模型服务限流，请稍后重试。")
    if response.is_success:
        try:
            result = response.json()
        except ValueError:
            result = None
        if isinstance(result, dict) and isinstance(result.get("data"), list):
            return {"message": "模型服务连接成功。"}
    raise HTTPException(status_code=502, detail="模型服务未返回有效的模型列表。")


@router.put("/api/settings")
def update_settings(update: SettingsUpdate) -> dict[str, Any]:
    from data_agent import api

    keyring_warning = ""
    if update.api_key is not None:
        value = update.api_key.strip()
        if not value:
            raise HTTPException(status_code=422, detail="API Key 不能为空。")
        persisted = api._save_runtime_api_key(value, update.persist_key)
        if update.persist_key and not persisted:
            keyring_warning = (
                "系统凭据存储不可用，本次 Key 仅保留在服务进程内存中，重启后需重新填写。"
            )
    update_runtime_settings(
        thinking_enabled=update.thinking_enabled,
        reasoning_effort=update.reasoning_effort,
    )
    payload = get_settings()
    if keyring_warning:
        payload["warning"] = keyring_warning
    return payload


@router.delete("/api/settings/key")
def delete_key() -> dict[str, bool]:
    from data_agent import api

    update_runtime_settings(api_key="")
    # 多用户 Web 作用域不能删除其他客户端或桌面旧流程使用的全局凭据。
    if not has_runtime_scope():
        delete_saved_api_key()
    configured = bool(api._effective_settings().api_key)
    return {"configured": configured}
