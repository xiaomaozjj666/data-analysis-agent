"""下载响应：把 ZIP 写到临时文件再流式发送，避免整包驻留内存。

会话导出（含 input/、artifacts/、workspace_state.parquet）与零信任证据包
（原始 CSV + 报告 + 账本）都可能达到数百 MB；此前用 ``io.BytesIO`` 在内存里
拼完整包，峰值内存约等于「源数据 + 压缩包」两份。改为：
1. 以 ``mkstemp`` 在系统临时目录创建文件（同目录 rename 语义无关，仅读一次）；
2. ``zipfile`` 逐文件写入磁盘（压缩流式进行，不额外占内存）；
3. ``FileResponse`` 以块读取发送，响应结束后由 ``BackgroundTask`` 删除临时文件。

失败路径（打包中途异常）也会立即删除临时文件，不把半截 ZIP 留在磁盘。
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

__all__ = ["zip_file_response"]


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
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )
