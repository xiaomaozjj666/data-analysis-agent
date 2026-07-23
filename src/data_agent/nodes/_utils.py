"""节点共享的消息文本提取辅助函数。

从 ``agent.py`` 拆分，供 execute / finalize 节点以及 ``DataAnalysisAgent.chat``
共享，避免跨模块循环导入。
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage


def _message_text(message: BaseMessage | None) -> str:
    """从 LangChain 消息对象中提取纯文本内容。

    兼容 str 和 list[dict] 两种 content 格式（后者出现在多模态/
    thinking-mode 响应中）。返回空字符串表示无有效文本。
    """
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            parts.append(str(item.get("text", "")))
    return "\n".join(part for part in parts if part)
