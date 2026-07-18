from __future__ import annotations

import keyring

SERVICE_NAME = "DataAnalysisAgent"
DEEPSEEK_ACCOUNT = "deepseek-api-key"


def get_saved_api_key() -> str:
    """Read the DeepSeek key from the operating system credential store."""
    try:
        return keyring.get_password(SERVICE_NAME, DEEPSEEK_ACCOUNT) or ""
    except Exception:
        return ""


def save_api_key(api_key: str) -> None:
    """Save the DeepSeek key in Windows Credential Manager."""
    value = api_key.strip()
    if not value:
        raise ValueError("API Key 不能为空。")
    try:
        keyring.set_password(SERVICE_NAME, DEEPSEEK_ACCOUNT, value)
    except Exception as exc:
        raise RuntimeError(f"无法写入 Windows 凭据管理器：{exc}") from exc


def delete_saved_api_key() -> None:
    """Remove the persisted DeepSeek key if it exists."""
    try:
        keyring.delete_password(SERVICE_NAME, DEEPSEEK_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        return
    except Exception as exc:
        raise RuntimeError(f"无法从 Windows 凭据管理器删除 Key：{exc}") from exc
