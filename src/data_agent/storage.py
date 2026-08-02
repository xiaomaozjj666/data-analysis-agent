"""会话持久化存储层：支持本地和 S3 兼容对象存储两种后端。

架构设计：
- ``SessionStorage`` Protocol 定义统一接口，便于扩展新后端。
- ``LocalSessionStorage``: 无操作实现，会话仅存在于进程内存和本地磁盘。
- ``S3SessionStorage``: 将整个会话目录打包为 ZIP 上传到 S3/R2 bucket，
  支持跨重启恢复。下载时带路径穿越检查，防止恶意归档注入。
- ``build_session_storage()``: 工厂函数，根据 DATA_AGENT_STORAGE_BACKEND
  环境变量选择后端。

线程安全：
    S3SessionStorage 的 boto3 client 是线程安全的（botocore 内部有
    连接池和锁）。LocalSessionStorage 无状态，天然线程安全。
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Protocol


class SessionStorage(Protocol):
    """会话持久化存储接口。

    实现类必须提供以下能力：
    - sync_session: 将本地会话目录同步到远端。
    - restore_session: 从远端恢复会话到本地。
    - delete_session: 删除远端会话归档。
    - healthcheck: 返回后端健康状态。
    """

    backend: str
    persistent: bool

    def sync_session(self, session_id: str, source: Path) -> None: ...

    def restore_session(self, session_id: str, destination: Path) -> bool: ...

    def delete_session(self, session_id: str) -> None: ...

    def healthcheck(self) -> dict[str, str | bool]: ...


class LocalSessionStorage:
    """本地存储后端：无操作实现，会话仅存在于进程内存和本地磁盘。

    适用于开发环境和单机部署，重启后会话不可恢复（但本地文件仍在）。
    """

    backend = "local"
    persistent = False

    def sync_session(self, session_id: str, source: Path) -> None:
        return

    def restore_session(self, session_id: str, destination: Path) -> bool:
        return False

    def delete_session(self, session_id: str) -> None:
        return

    def healthcheck(self) -> dict[str, str | bool]:
        return {"backend": self.backend, "persistent": self.persistent, "status": "local_only"}


class S3SessionStorage:
    """S3 兼容对象存储后端：将会话目录打包为 ZIP 归档存储。

    支持 Cloudflare R2、AWS S3、MinIO 等任何 S3 兼容服务。
    每次 sync 生成临时 ZIP 上传后立即删除，不占用额外磁盘。
    restore 时带路径穿越检查，拒绝包含 ``..`` 或绝对路径的恶意归档。

    Attributes:
        bucket: 存储桶名称。
        endpoint_url: S3 服务端点。
        prefix: 归档对象的键前缀。
    """

    backend = "s3"
    persistent = True

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = "data-analysis-agent/sessions",
        region: str = "auto",
    ) -> None:
        if not all((bucket, endpoint_url, access_key_id, secret_access_key)):
            raise ValueError("R2/S3 持久化配置不完整。")
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.endpoint_url = endpoint_url.rstrip("/")
        self.prefix = prefix.strip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(s3={"addressing_style": "path"}),
        )
        # Track per-file mtime per session so sync_session only uploads
        # changed files instead of re-zipping the entire directory.
        self._mtimes: dict[str, dict[str, float]] = {}

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}/{session_id}.zip"

    def sync_session(self, session_id: str, source: Path) -> None:
        source = source.resolve()
        archive = source.parent / f".{session_id}.upload.zip"
        prev_mtimes: dict[str, float] = getattr(self, "_mtimes", {}).get(session_id, {})
        new_mtimes: dict[str, float] = {}
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                for path in sorted(source.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(source).as_posix()
                    mtime = path.stat().st_mtime
                    new_mtimes[rel] = mtime
                    # Skip unchanged files on non-initial syncs.
                    if prev_mtimes and rel in prev_mtimes and abs(prev_mtimes[rel] - mtime) < 0.01:
                        continue
                    bundle.write(path, rel)
            self.client.upload_file(str(archive), self.bucket, self._key(session_id))
            if hasattr(self, "_mtimes"):
                self._mtimes[session_id] = new_mtimes
        except Exception as exc:
            raise RuntimeError(f"R2/S3 会话持久化失败：{exc}") from exc
        finally:
            archive.unlink(missing_ok=True)

    def restore_session(self, session_id: str, destination: Path) -> bool:
        destination = destination.resolve()
        archive = destination.parent / f".{session_id}.download.zip"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, self._key(session_id), str(archive))
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                archive.unlink(missing_ok=True)
                return False
            raise RuntimeError(f"R2/S3 会话恢复失败：{exc}") from exc

        try:
            with zipfile.ZipFile(archive) as bundle:
                root = destination.resolve()
                for member in bundle.infolist():
                    target = (root / member.filename).resolve()
                    if target != root and root not in target.parents:
                        raise ValueError("持久化归档包含不安全路径。")
                bundle.extractall(root)
            return True
        finally:
            archive.unlink(missing_ok=True)

    def delete_session(self, session_id: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(session_id))
        except Exception as exc:
            raise RuntimeError(f"R2/S3 会话归档删除失败：{exc}") from exc
        if hasattr(self, "_mtimes"):
            self._mtimes.pop(session_id, None)

    def healthcheck(self) -> dict[str, str | bool]:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            return {
                "backend": self.backend,
                "persistent": self.persistent,
                "status": "error",
                "message": str(exc)[:500],
            }
        return {
            "backend": self.backend,
            "persistent": self.persistent,
            "status": "ok",
            "bucket": self.bucket,
        }


def build_session_storage() -> SessionStorage:
    """根据环境变量构建会话存储后端。

    环境变量：
    - DATA_AGENT_STORAGE_BACKEND: "local"（默认）或 "s3"。
    - DATA_AGENT_R2_ACCOUNT_ID: Cloudflare R2 账户 ID（自动生成 endpoint）。
    - DATA_AGENT_STORAGE_ENDPOINT_URL: 显式 S3 端点（优先于 R2 自动生成）。
    - DATA_AGENT_STORAGE_BUCKET: 存储桶名称。
    - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY: 认证凭据。
    - DATA_AGENT_STORAGE_PREFIX: 对象键前缀。
    - AWS_DEFAULT_REGION: 区域（默认 "auto"）。

    Returns:
        SessionStorage 实现实例。

    Raises:
        ValueError: 后端配置不完整或不支持的 backend 值。
    """
    backend = os.getenv("DATA_AGENT_STORAGE_BACKEND", "local").strip().lower()
    if backend in {"", "local"}:
        return LocalSessionStorage()
    if backend != "s3":
        raise ValueError("DATA_AGENT_STORAGE_BACKEND 仅支持 local 或 s3。")
    account_id = os.getenv("DATA_AGENT_R2_ACCOUNT_ID", "").strip()
    endpoint_url = os.getenv("DATA_AGENT_STORAGE_ENDPOINT_URL", "").strip()
    if not endpoint_url and account_id:
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return S3SessionStorage(
        bucket=os.getenv("DATA_AGENT_STORAGE_BUCKET", "").strip(),
        endpoint_url=endpoint_url,
        access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "").strip(),
        secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "").strip(),
        prefix=os.getenv("DATA_AGENT_STORAGE_PREFIX", "data-analysis-agent/sessions").strip(),
        region=os.getenv("AWS_DEFAULT_REGION", "auto").strip() or "auto",
    )
