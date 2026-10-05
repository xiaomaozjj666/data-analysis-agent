"""工作区 bundle 管理、原子写入与核心数据结构。

从 workspace.py 提取的模块级辅助代码：CDN bundle 下载/共享缓存、原子写入、
dtype 降级、Artifact/WorkspaceSnapshot 数据类。DataWorkspace 类本身因内部
高度耦合而保留在 workspace.py，本模块仅承载无状态的纯函数与数据定义。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pandas as pd

# ---------------------------------------------------------------------------
# Bundle 命名常量
# ---------------------------------------------------------------------------

#: 共享 Plotly.js 束缚文件名，每个工作区只写一次，所有图表复用。
PLOTLY_BUNDLE_NAME = "plotly.min.js"
#: ECharts 引擎所需的前端 bundle 文件名，与 plotly.min.js 同目录共存。
#: 双引擎互不冲突：HTML 通过相对路径引用各自的 bundle。
ECHARTS_BUNDLE_NAME = "echarts.min.js"
#: ECharts 官方稳定版 CDN，首次生成 echarts 图表时下载到 artifacts_dir，
#: 后续复用。下载失败时 fallback 到 CDN URL 直接引用（在线场景）。
ECHARTS_CDN_URL = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"
#: echarts-gl 扩展 bundle（3D 散点等 gl 系列需要），与主 bundle 同目录按需下载。
ECHARTS_GL_BUNDLE_NAME = "echarts-gl.min.js"
ECHARTS_GL_CDN_URL = "https://cdn.jsdelivr.net/npm/echarts-gl@2.0.9/dist/echarts-gl.min.js"

#: CDN bundle 的机器级共享缓存目录名（位于 runs 根目录下）。
#: 这些文件对所有会话完全相同，没必要每个会话各下一次——实测每个新会话的
#: 首张 ECharts 图要多等约 6 秒，纯粹是重复下载同一个 1MB 文件。
_BUNDLE_CACHE_DIRNAME = "_bundles"
#: 小于该字节数视为损坏/半截下载（CDN 出错页），需要重新拉取。
MIN_BUNDLE_BYTES = 1024

_bundle_lock = threading.Lock()


def shared_bundle_path(root: Path, name: str) -> Path:
    """共享缓存里某个 bundle 的路径（不保证存在）。"""
    directory = Path(
        os.environ.get("DATA_AGENT_BUNDLE_CACHE_DIR") or (root / _BUNDLE_CACHE_DIRNAME)
    )
    return directory / name


def warm_bundles(root: Path, names: tuple[tuple[str, str], ...] | None = None) -> dict[str, bool]:
    """预下载 CDN bundle 到共享缓存（供服务启动时后台调用）。

    返回 ``{文件名: 是否可用}``。任何失败都只记为 False——离线时图表会按原
    逻辑 fallback 到 CDN 直引，不能因为预热失败影响启动。
    """
    targets = names or (
        (ECHARTS_BUNDLE_NAME, ECHARTS_CDN_URL),
        (ECHARTS_GL_BUNDLE_NAME, ECHARTS_GL_CDN_URL),
    )
    result: dict[str, bool] = {}
    for name, url in targets:
        path = shared_bundle_path(root, name)
        if not (path.exists() and path.stat().st_size > MIN_BUNDLE_BYTES):
            _download_bundle(url, path)
        result[name] = path.exists() and path.stat().st_size > MIN_BUNDLE_BYTES
    return result


def _download_bundle(url: str, target: Path) -> bool:
    """下载 bundle 到共享缓存（先写临时文件再原子替换，避免半截文件被复用）。"""
    import urllib.request

    with _bundle_lock:
        if target.exists() and target.stat().st_size > MIN_BUNDLE_BYTES:
            return True
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
                content = response.read()
            if not content or len(content) < MIN_BUNDLE_BYTES:
                return False
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(content)
            temporary.replace(target)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 原子写入与 dtype 降级
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text atomically: write to a sibling .tmp file then rename.

    A direct ``path.write_text`` truncates the destination before writing; if
    the process is killed mid-write (OOM, deploy restart, disk full) we leave
    a corrupt partial file that subsequent reads will fail on. The tmp + rename
    pattern guarantees readers either see the old file or the new file, never
    a half-written one. ``os.replace`` is atomic on POSIX and Windows for
    same-filesystem renames.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding=encoding)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _downcast_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """降级 DataFrame 数值列的 dtype 以节省内存。

    pandas 默认用 int64/float64，对大多数业务数据来说 int32/float32 已足够。
    安全降级：仅在不丢失精度时转换（如 0-255 的整数列 → uint8）。
    典型场景：100 万行 × 10 列的 int64 数据，降级后内存从 ~80MB 降到 ~20MB。
    """
    for col in df.columns:
        col_data = df[col]
        if col_data.dtype == "int64":
            # 尝试降级到最小可容纳的整数类型
            df[col] = pd.to_numeric(col_data, downcast="integer")
        elif col_data.dtype == "float64":
            # float32 精度足够大多数统计场景（6-7 位有效数字）
            df[col] = pd.to_numeric(col_data, downcast="float")
    return df


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Artifact:
    """工作区中已注册的产物文件元数据。

    Attributes:
        name: 文件名（不含目录）。
        kind: 产物类型（visualization / dataset / image / chart_data）。
        path: 绝对路径。
        description: 面向用户的简短描述。
    """

    name: str
    kind: str
    path: Path
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": str(self.path.resolve()),
            "description": self.description,
        }


class WorkspaceSnapshot(NamedTuple):
    """单步执行前的回滚点。

    ``source_row_count`` 一并纳入快照：跨源合并会重置行数基线，若回滚只恢复
    DataFrame 而不恢复基线，后续 clean_data 的 20% 安全下限就会以"已被回滚掉的
    那张表"的规模计算，护栏在同一会话内失效。

    Attributes:
        dataframe: 快照时的活动数据集（浅拷贝，依赖 pandas 写时复制语义）。
        files: 快照时 artifacts 目录中已存在的文件路径集合。
        version: 快照时的 ``_df_version``，用于跳过多余的 DataFrame 还原。
        source_row_count: 快照时的行数基线。
    """

    dataframe: pd.DataFrame
    files: set[Path]
    version: int
    source_row_count: int
