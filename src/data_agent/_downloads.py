"""下载响应：把 ZIP 写到临时文件再流式发送，避免整包驻留内存。

会话导出（含 input/、artifacts/、workspace_state.parquet）与零信任证据包
（原始 CSV + 报告 + 账本）都可能达到数百 MB；此前用 ``io.BytesIO`` 在内存里
拼完整包，峰值内存约等于「源数据 + 压缩包」两份。改为：
1. 以 ``mkstemp`` 在系统临时目录创建文件（同目录 rename 语义无关，仅读一次）；
2. ``zipfile`` 逐文件写入磁盘（压缩流式进行，不额外占内存）；
3. ``FileResponse`` 以块读取发送，响应结束后删除临时文件。

清理放在响应 ``__call__`` 的 ``finally`` 里：BackgroundTask 在 Starlette
1.10 中只在正常发送结束后执行，客户端断开或 send 抛异常时不会运行，
临时文件会残留。``finally`` 覆盖发送异常与客户端断开两条路径。

失败路径（打包中途异常）也会立即删除临时文件，不把半截 ZIP 留在磁盘。
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

__all__ = ["zip_file_response"]


class _CleanupFileResponse(FileResponse):
    """发送完成或发送异常后都删除底层临时文件的 FileResponse。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


def zip_file_response(
    filename: str,
    write: Callable[[zipfile.ZipFile], None],
    *,
    media_type: str = "application/zip",
) -> FileResponse:
    """生成 ZIP 下载响应；``write`` 负责往打开的 ZipFile 里写条目。"""
    descriptor, name = tempfile.mkstemp(prefix="data-agent-download-", suffix=".zip")
    os.close(descriptor)
    path = Path(name)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            write(bundle)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return _CleanupFileResponse(
        path,
        media_type=media_type,
        filename=filename,
    )
