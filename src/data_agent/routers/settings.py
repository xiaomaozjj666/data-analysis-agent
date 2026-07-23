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

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from data_agent.credentials import delete_saved_api_key
from data_agent.middleware import _check_access
from data_agent.registry import SettingsUpdate

router = APIRouter()


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
    required = bool(os.getenv("APP_ACCESS_TOKEN", "").strip())
    if not required:
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
    return {
        "provider": "deepseek",
        "model": settings.model,
        "base_url": settings.base_url,
        "configured": bool(settings.api_key),
        "thinking_enabled": settings.thinking_enabled,
        "reasoning_effort": settings.reasoning_effort,
        "langsmith_tracing": os.getenv("LANGSMITH_TRACING", "false").lower() == "true",
        "langsmith_project": os.getenv("LANGSMITH_PROJECT", "data-analysis-agent"),
        "storage_backend": api.session_storage.backend,
        "persistent_storage": api.session_storage.persistent,
        "storage_status": storage_status.get("status", "unknown"),
        "storage_message": storage_status.get("message", ""),
    }


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
            keyring_warning = "系统凭据存储不可用，本次 Key 仅保留在服务进程内存中，重启后需重新填写。"
    with api.runtime_settings_lock:
        api.runtime_settings["thinking_enabled"] = update.thinking_enabled
        api.runtime_settings["reasoning_effort"] = update.reasoning_effort
    payload = get_settings()
    if keyring_warning:
        payload["warning"] = keyring_warning
    return payload


@router.delete("/api/settings/key")
def delete_key() -> dict[str, bool]:
    from data_agent import api

    with api.runtime_settings_lock:
        api.runtime_settings["api_key"] = ""
    delete_saved_api_key()
    configured = bool(api._effective_settings().api_key)
    return {"configured": configured}
