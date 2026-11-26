"""定时分析任务的只读/触发路由。

任务定义仍由 ``automation/automations.json``（或 ``data-agent-automation`` CLI）
管理，本路由只负责前端面板需要的最小闭环：

- ``GET  /api/automation/tasks``：任务概览（下次到期、上次状态、告警数）。
- ``GET  /api/automation/tasks/{id}/history``：运行历史尾部。
- ``PATCH /api/automation/tasks/{id}``：启停任务（只改 ``enabled``）。
- ``POST /api/automation/tasks/{id}/run``：立即运行；``dry_run=true`` 时只
  计算不写历史/状态、也不推送。

错误口径：配置损坏（``load_specs`` 抛 ``ValueError``）返回 422 而不是 500，
让用户知道是任务定义文件的问题；任务被其他进程锁住时返回 409，前端提示
"正在运行"而不是当成失败。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from data_agent import automation

router = APIRouter()


def _runs_dir() -> Any:
    from data_agent import api

    return api.bootstrap_settings.runs_dir


def _specs() -> list[automation.AutomationSpec]:
    try:
        return automation.load_specs(_runs_dir())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"任务配置读取失败：{exc}") from exc


def _overview() -> dict[str, Any]:
    try:
        return automation.status_overview(_runs_dir())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"任务配置读取失败：{exc}") from exc


class TaskToggle(BaseModel):
    enabled: bool


@router.get("/api/automation/tasks")
def list_tasks() -> dict[str, Any]:
    """全部任务的当前状态汇总（不触发任何执行）。"""
    return _overview()


@router.get("/api/automation/tasks/{task_id}/history")
def task_history(task_id: str, limit: int = 20) -> dict[str, Any]:
    """最近若干次运行记录（按时间正序，尾部 ``limit`` 条）。"""
    if not any(spec.id == task_id for spec in _specs()):
        raise HTTPException(status_code=404, detail=f"定时任务不存在：{task_id}")
    capped = max(1, min(int(limit), 200))
    try:
        rows = automation.read_history(_runs_dir(), task_id, capped)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"运行历史读取失败：{exc}") from exc
    return {"task_id": task_id, "history": rows}


@router.patch("/api/automation/tasks/{task_id}")
def toggle_task(task_id: str, body: TaskToggle) -> dict[str, Any]:
    """启停任务；返回更新后的完整概览，前端一次请求即可刷新列表。"""
    specs = _specs()
    if not any(spec.id == task_id for spec in specs):
        raise HTTPException(status_code=404, detail=f"定时任务不存在：{task_id}")
    updated = [replace(spec, enabled=body.enabled) if spec.id == task_id else spec for spec in specs]
    try:
        automation.save_specs(_runs_dir(), updated)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"任务配置写入失败：{exc}") from exc
    return _overview()


@router.post("/api/automation/tasks/{task_id}/run")
def run_task(task_id: str, dry_run: bool = False) -> dict[str, Any]:
    """立即执行一次任务；``dry_run`` 用于验证配置而不落历史。"""
    spec = next((item for item in _specs() if item.id == task_id), None)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"定时任务不存在：{task_id}")
    try:
        result = automation.run_automation(spec, _runs_dir(), dry_run=dry_run)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.get("skipped") == "locked":
        raise HTTPException(status_code=409, detail="任务正在运行，请稍后再试。")
    return result
