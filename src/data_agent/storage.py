from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Protocol


class SessionStorage(Protocol):
    backend: str
    persistent: bool

    def sync_session(self, session_id: str, source: Path) -> None: ...

    def restore_session(self, session_id: str, destination: Path) -> bool: ...

    def delete_session(self, session_id: str) -> None: ...

    def healthcheck(self) -> dict[str, str | bool]: ...


class LocalSessionStorage:
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
    """Persist an isolated session directory as one archive in an S3-compatible bucket."""

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

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}/{session_id}.zip"

    def sync_session(self, session_id: str, source: Path) -> None:
        source = source.resolve()
        archive = source.parent / f".{session_id}.upload.zip"
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                for path in sorted(source.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(source))
            self.client.upload_file(str(archive), self.bucket, self._key(session_id))
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
