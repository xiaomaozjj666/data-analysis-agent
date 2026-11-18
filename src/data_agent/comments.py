"""会话评论：轻量协作层的存储与校验。

设计边界（对标 P2「共享空间、评论」的最小可用版）：
- 评论跟着会话目录走，导出/导入 ZIP 时自然带上；不引入数据库与用户体系。
- 单文件 ``comments.json`` + 原子写：评论数量有上限，读改写成本可接受；
  多进程并发写入仍可能丢一条（无跨进程锁），这是刻意的轻量取舍，文档写明。
- 文本只做长度与空白校验，不做 Markdown/HTML 渲染；前端按纯文本显示，
  避免把评论变成 XSS 注入面。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from data_agent._workspace_bundles import _atomic_write_text

COMMENTS_FILENAME = "comments.json"
MAX_COMMENTS = 200
MAX_TEXT_CHARS = 2000
MAX_AUTHOR_CHARS = 40

_log = logging.getLogger(__name__)
_lock = threading.Lock()


def _comments_path(root: str | Path) -> Path:
    return Path(root).expanduser() / COMMENTS_FILENAME


def list_comments(root: str | Path) -> list[dict[str, Any]]:
    """读取评论；文件损坏时记录告警并返回空列表（不阻断会话其他功能）。"""
    path = _comments_path(root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("cannot read comments file %s: %s", path, exc)
        return []
    if not isinstance(payload, list):
        _log.warning("comments file %s is not a JSON array; ignoring", path)
        return []
    return [item for item in payload if isinstance(item, dict)]


def add_comment(root: str | Path, text: str, author: str = "") -> dict[str, Any]:
    """追加一条评论并原子落盘；空文本/超长/超过上限时抛 ValueError。"""
    content = " ".join(str(text or "").split())
    if not content:
        raise ValueError("评论内容不能为空。")
    if len(content) > MAX_TEXT_CHARS:
        raise ValueError(f"评论不能超过 {MAX_TEXT_CHARS} 个字符。")
    name = " ".join(str(author or "").split())[:MAX_AUTHOR_CHARS] or "匿名"
    with _lock:
        items = list_comments(root)
        if len(items) >= MAX_COMMENTS:
            raise ValueError(f"单个会话最多保留 {MAX_COMMENTS} 条评论，请先删除旧评论。")
        comment = {
            "id": uuid4().hex[:12],
            "text": content,
            "author": name,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        items.append(comment)
        path = _comments_path(root)
        _atomic_write_text(path, json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    return comment


def delete_comment(root: str | Path, comment_id: str) -> bool:
    """删除指定评论；不存在返回 False。"""
    with _lock:
        items = list_comments(root)
        remaining = [item for item in items if item.get("id") != comment_id]
        if len(remaining) == len(items):
            return False
        _atomic_write_text(
            _comments_path(root),
            json.dumps(remaining, ensure_ascii=False, indent=2) + "\n",
        )
        return True
