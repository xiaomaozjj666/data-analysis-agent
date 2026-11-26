"""会话级分析内核快照。

分析过程中的 objective / plan / 完成步骤 / 当前节点 / 工具调用时间线原先只
存在于 worker 线程的局部变量与 SSE 帧里：切换会话或进程重启后，只能靠
``last_result`` 恢复"已完成的整份结果"，正在执行或中断的分析会丢失计划面板
与工具时间线。

本模块提供一个纯数据结构的快照对象：

- 由 ``SessionRecord`` 持有，随 ``session.json`` 原子落盘并进入
  ``GET /api/sessions/{id}`` 载荷；
- 分析 worker 在每个节点/工具事件处增量更新（内存态，零额外 I/O）；
- 恢复时对缺失字段与坏类型一律降级到默认值——内核是展示用辅助数据，
  任何一条脏数据都不应让整个会话恢复失败。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: 工具时间线只保留尾部若干条。一次分析可能有几十次工具调用，完整保存会
#: 让 session.json 随会话轮次线性膨胀；尾部足够还原"最近在做什么"。
KERNEL_TOOL_TRACE_LIMIT = 30

#: 允许通过 :meth:`SessionKernel.update` 修改的字段白名单。
_UPDATE_FIELDS = frozenset(
    {
        "objective",
        "plan",
        "plan_source",
        "completed_steps",
        "current_node",
        "status",
        "tool_trace",
    }
)


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """把任意值降级为 ``list[dict]``；非列表/非字典元素直接丢弃。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


@dataclass
class SessionKernel:
    """一次分析的可恢复内核快照。"""

    objective: str = ""
    plan: list[dict[str, Any]] = field(default_factory=list)
    plan_source: str = ""
    completed_steps: list[dict[str, Any]] = field(default_factory=list)
    current_node: str = ""
    #: idle / running / awaiting_approval / completed / cancelled / failed
    status: str = "idle"
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好载荷（工具时间线裁剪到尾部上限）。"""
        return {
            "objective": self.objective,
            "plan": list(self.plan),
            "plan_source": self.plan_source,
            "completed_steps": list(self.completed_steps),
            "current_node": self.current_node,
            "status": self.status,
            "tool_trace": list(self.tool_trace[-KERNEL_TOOL_TRACE_LIMIT:]),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> SessionKernel:
        """从 session.json 恢复；任何非法字段都回退默认值，绝不抛异常。"""
        if not isinstance(payload, dict):
            return cls()
        updated_at = payload.get("updated_at")
        return cls(
            objective=str(payload.get("objective") or ""),
            plan=_as_dict_list(payload.get("plan")),
            plan_source=str(payload.get("plan_source") or ""),
            completed_steps=_as_dict_list(payload.get("completed_steps")),
            current_node=str(payload.get("current_node") or ""),
            status=str(payload.get("status") or "idle"),
            tool_trace=_as_dict_list(payload.get("tool_trace"))[-KERNEL_TOOL_TRACE_LIMIT:],
            updated_at=float(updated_at) if isinstance(updated_at, (int, float)) else None,
        )

    def update(self, **changes: Any) -> None:
        """批量更新白名单字段并刷新 ``updated_at``（未知字段忽略）。"""
        for key, value in changes.items():
            if key in _UPDATE_FIELDS:
                setattr(self, key, value)
        self.updated_at = time.time()

    def append_tool(self, entry: dict[str, Any]) -> None:
        """追加一条工具调用；同一 call_id 重复上报时原位覆盖。"""
        call_id = str(entry.get("call_id") or "")
        if call_id:
            for index, item in enumerate(self.tool_trace):
                if str(item.get("call_id") or "") == call_id:
                    self.tool_trace[index] = {**item, **entry}
                    self._trim_trace()
                    self.updated_at = time.time()
                    return
        self.tool_trace.append(dict(entry))
        self._trim_trace()
        self.updated_at = time.time()

    def update_tool(self, call_id: str, **fields: Any) -> None:
        """按 call_id 合并工具调用结果；找不到时静默忽略。"""
        if not call_id:
            return
        for item in self.tool_trace:
            if str(item.get("call_id") or "") == call_id:
                item.update(fields)
                self.updated_at = time.time()
                return

    def _trim_trace(self) -> None:
        if len(self.tool_trace) > KERNEL_TOOL_TRACE_LIMIT:
            del self.tool_trace[:-KERNEL_TOOL_TRACE_LIMIT]
