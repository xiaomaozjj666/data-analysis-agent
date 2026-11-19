"""账本条目的 Ed25519 签名：让“谁写的”也能离线验证。

每个工作区首次写账本时生成一对 Ed25519 密钥，私钥只留在工作区
``audit_keys/ledger_ed25519.key``，公钥随证据包导出。签名对象是账本条目的
SHA-256（链哈希），签名放在条目外层，不参与哈希复算，因此：

- 改任何正文 → 链哈希对不上；
- 改哈希 → 签名对不上；
- 删签名 → 验签失败（只对声明已签名的账本强制）。

生产环境可用 ``DATA_AGENT_LEDGER_SEED``（32 字节 base64）固定密钥，便于
多实例共享同一身份；未设置时为每个工作区生成独立密钥。
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_DIR = "audit_keys"
PRIVATE_KEY = "ledger_ed25519.key"
PUBLIC_KEY = "ledger_ed25519.pub"
SEED_ENV = "DATA_AGENT_LEDGER_SEED"


def key_dir(root: str | Path) -> Path:
    return Path(root) / KEY_DIR


def public_key_path(root: str | Path) -> Path:
    return key_dir(root) / PUBLIC_KEY


def _private_key(root: str | Path) -> Ed25519PrivateKey:
    configured = os.environ.get(SEED_ENV, "").strip()
    if configured:
        seed = base64.b64decode(configured)
        if len(seed) != 32:
            raise ValueError(f"{SEED_ENV} 必须是 32 字节的 base64。")
        return Ed25519PrivateKey.from_private_bytes(seed)
    path = key_dir(root) / PRIVATE_KEY
    if path.is_file():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]
    path.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return private


def public_key_bytes(root: str | Path) -> bytes:
    path = public_key_path(root)
    if path.is_file():
        return path.read_bytes()
    private = _private_key(root)
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def key_id(root: str | Path) -> str:
    raw = public_key_bytes(root)
    return hashlib.sha256(raw).hexdigest()[:16]


def sign_hash(root: str | Path, entry_hash: str) -> dict[str, str]:
    signature = _private_key(root).sign(entry_hash.encode("ascii"))
    return {
        "signature": base64.b64encode(signature).decode("ascii"),
        "signature_key_id": key_id(root),
    }


def verify_hash(public_key_pem: bytes, entry_hash: str, signature_b64: str) -> bool:
    try:
        public = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public, Ed25519PublicKey):
            return False
        public.verify(base64.b64decode(signature_b64), entry_hash.encode("ascii"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_entries_signatures(
    entries: list[dict[str, Any]], public_key_pem: bytes
) -> dict[str, Any]:
    errors: list[str] = []
    signed = 0
    for index, entry in enumerate(entries, start=1):
        signature = entry.get("signature")
        if not signature:
            errors.append(f"第 {index} 条记录没有签名。")
            continue
        signed += 1
        if not verify_hash(public_key_pem, str(entry.get("hash", "")), str(signature)):
            errors.append(f"第 {index} 条记录的签名无效。")
    return {"valid": not errors, "signed": signed, "errors": errors}
