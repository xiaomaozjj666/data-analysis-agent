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
    seed = _env_seed()
    if seed is not None:
        return Ed25519PrivateKey.from_private_bytes(seed)
    path = key_dir(root) / PRIVATE_KEY
    if not path.is_file() and public_key_path(root).is_file():
        raise ValueError(
            f"检测到公钥 {public_key_path(root)} 但私钥 {path} 缺失：拒绝重新生成，"
            "否则新条目将无法用旧公钥验证。"
        )
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
    # 0o600 对 NTFS ACL 是 no-op：Windows 权限继承工作区目录，边界已在
    # 文档声明（私钥与数据同目录，工作区访问即密钥访问）。
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return private


def _env_seed() -> bytes | None:
    """环境变量种子（base64, 32 字节）；未设置或非法时返回 None / 抛 ValueError。"""
    configured = os.environ.get(SEED_ENV, "").strip()
    if not configured:
        return None
    seed = base64.b64decode(configured, validate=True)
    if len(seed) != 32:
        raise ValueError(f"{SEED_ENV} 必须是 32 字节的 base64。")
    return seed


def _public_pem(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def public_key_bytes(root: str | Path) -> bytes:
    """返回当前生效身份的公钥 PEM。

    env 种子激活时公钥**必须从种子推导**（I2：此前签名用种子密钥、key_id 却读
    磁盘旧 .pub，会出现"签名与 key_id 来自两把不同钥匙"的自相矛盾条目）。
    磁盘路径下若 .key 缺失而 .pub 还在，说明密钥曾被删除：不能静默重新生成
    （新钥匙 + 旧公钥 = 之后所有条目都验不了），显式报错让用户删除旧 .pub 或
    恢复 .key。base64 解码加 validate=True：坏字符不再被静默丢弃成另一把钥匙。
    """
    seed = _env_seed()
    if seed is not None:
        private = Ed25519PrivateKey.from_private_bytes(seed)
        raw = _public_pem(private)
        path = public_key_path(root)
        if path.is_file() and path.read_bytes() != raw:
            raise ValueError(
                "DATA_AGENT_LEDGER_SEED 与工作区已有公钥不一致："
                "验签会全部失败；请使用导出时的种子或删除旧公钥。"
            )
        return raw
    path = public_key_path(root)
    private_key_path = key_dir(root) / PRIVATE_KEY
    if path.is_file() and not private_key_path.is_file():
        raise ValueError(
            f"检测到公钥 {path} 但私钥 {private_key_path} 缺失：签名身份不完整。"
            "验签请直接读公钥文件；追加需恢复私钥或使用同一 DATA_AGENT_LEDGER_SEED。"
        )
    if path.is_file():
        return path.read_bytes()
    private = _private_key(root)
    raw = _public_pem(private)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def key_id(root: str | Path) -> str:
    raw = public_key_bytes(root)
    return hashlib.sha256(raw).hexdigest()[:16]


def _persist_public_key(root: str | Path, private: Ed25519PrivateKey) -> None:
    """首次签名时把公钥落盘；已有公钥必须与当前签名身份一致。"""
    path = public_key_path(root)
    raw = _public_pem(private)
    if path.is_file():
        if path.read_bytes() != raw:
            raise ValueError(
                "当前签名身份与工作区已有公钥不一致："
                "请删除旧公钥（历史条目将无法验签）或恢复对应私钥/种子。"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def sign_hash(root: str | Path, entry_hash: str) -> dict[str, str]:
    seed = _env_seed()
    private = Ed25519PrivateKey.from_private_bytes(seed) if seed is not None else _private_key(root)
    signature = private.sign(entry_hash.encode("ascii"))
    _persist_public_key(root, private)
    return {
        "signature": base64.b64encode(signature).decode("ascii"),
        # key_id 与签名必须同源（env 种子激活时此前读磁盘 .pub，I2 实测矛盾条目）。
        "signature_key_id": hashlib.sha256(_public_pem(private)).hexdigest()[:16],
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
    """逐条验签；公钥本身解析不了时抛 ValueError（与“签名无效”区分开）。"""
    try:
        public = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"公钥无法解析：{exc}") from exc
    if not isinstance(public, Ed25519PublicKey):
        raise ValueError("公钥不是 Ed25519 类型。")
    errors: list[str] = []
    signed = 0
    for index, entry in enumerate(entries, start=1):
        signature = entry.get("signature")
        if not signature:
            errors.append(f"第 {index} 条记录没有签名。")
            continue
        signed += 1
        try:
            public.verify(base64.b64decode(signature), str(entry.get("hash", "")).encode("ascii"))
        except InvalidSignature:
            errors.append(f"第 {index} 条记录的签名无效。")
        except (ValueError, TypeError):
            errors.append(f"第 {index} 条记录的签名编码无效。")
    return {"valid": not errors, "signed": signed, "errors": errors}
