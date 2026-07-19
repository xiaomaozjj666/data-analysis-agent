from __future__ import annotations

import logging

import keyring

logger = logging.getLogger(__name__)

SERVICE_NAME = "DataAnalysisAgent"
DEEPSEEK_ACCOUNT = "deepseek-api-key"


def get_saved_api_key() -> str:
    """Read the DeepSeek key from the operating system credential store.

    Returns an empty string when the backend is unavailable (for example on
    headless Linux servers without a Secret Service daemon) so that the API
    keeps working with the in-memory key configured via the settings panel.
    """
    try:
        return keyring.get_password(SERVICE_NAME, DEEPSEEK_ACCOUNT) or ""
    except Exception:
        return ""


def save_api_key(api_key: str) -> bool:
    """Persist the DeepSeek key in the OS credential store.

    Returns True on success, False when the credential backend is unavailable
    (Linux without a Secret Service daemon, sandboxes, etc.). The caller is
    expected to keep the in-memory copy regardless of this result.
    """
    value = api_key.strip()
    if not value:
        raise ValueError("API Key 不能为空。")
    try:
        keyring.set_password(SERVICE_NAME, DEEPSEEK_ACCOUNT, value)
        return True
    except Exception as exc:
        logger.warning("无法写入系统凭据存储，本次仅保留在内存中：%s", exc)
        return False


def delete_saved_api_key() -> bool:
    """Remove the persisted DeepSeek key if it exists.

    Returns True when deletion succeeded or the key was already absent.
    Returns False when the credential backend is unavailable; in that case
    the caller should still clear the in-memory copy.
    """
    try:
        keyring.delete_password(SERVICE_NAME, DEEPSEEK_ACCOUNT)
        return True
    except keyring.errors.PasswordDeleteError:
        return True
    except Exception as exc:
        logger.warning("无法从系统凭据存储删除 Key，仅清除内存副本：%s", exc)
        return False
