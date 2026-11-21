"""经营简报核验结果的会话级持久化。

核验复算的结果此前只活在前端组件 state 里：重开会话、刷新页面、切换
标签都会丢，用户只能重新复算。本模块把每次复算的完整结果（含草稿正文、
时间戳）原子写入会话工作区 ``audit/last_audit.json``，确认发布的回执
（receipt）与权威简报也追加进去，前端打开核验页时按需拉取即可恢复
"上次核验到哪一步"。

设计约束：
- 写失败绝不影响核验本身（核验/确认的主链路照常返回，只附加提示）。
- 文件属于会话工作区，随会话删除而清理，不进产物注册表（避免把内部
  状态文件混进用户产物列表）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

AUDIT_STATE_DIRNAME = "audit"
AUDIT_STATE_FILENAME = "last_audit.json"


def last_audit_path(root: str | Path) -> Path:
    return Path(root) / AUDIT_STATE_DIRNAME / AUDIT_STATE_FILENAME


def save_last_audit(root: str | Path, result: dict[str, Any], draft: str) -> None:
    """保存（覆盖）最近一次核验结果；调用方负责吞掉 OSError。"""
    path = last_audit_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": time.time(),
        "draft": draft,
        "result": result,
    }
    temporary = path.with_name(f"{path.name}.{uuid4().hex[:8]}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def attach_receipt(root: str | Path, receipt: dict[str, Any], brief: str) -> None:
    """确认发布成功后，把回执与权威简报并入最近一次核验结果。"""
    path = last_audit_path(root)
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.exception("Failed to read audit state for receipt attach")
        return
    if not isinstance(payload, dict):
        return
    payload["receipt"] = receipt
    payload["confirmed_brief"] = brief
    payload["confirmed_at"] = time.time()
    temporary = path.with_name(f"{path.name}.{uuid4().hex[:8]}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError:
        logger.exception("Failed to persist confirm receipt into audit state")
    finally:
        temporary.unlink(missing_ok=True)


def load_last_audit(root: str | Path) -> dict[str, Any] | None:
    """读取最近一次核验结果；没有记录或文件损坏时返回 None。"""
    path = last_audit_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.exception("Failed to load audit state")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        return None
    return payload
