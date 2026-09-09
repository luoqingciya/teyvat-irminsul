"""单篇笔记加密（AES-256-GCM）。

加密存储格式：真头 ``MDNOTES_ENC1|`` + 版本 + 随机 salt + nonce + 密文（base64）。
密钥由主密码通过 PBKDF2-HMAC-SHA256 派生；派生后的密钥（base64）由上层
保存在 config.encrypt 中，以便笔记加密后仍能被透明地自动解密（开箱即用，
代价是拿到配置文件 + 笔记目录者可解——适用于防随手翻阅的本地隐私场景）。
"""

from __future__ import annotations

import base64
import hashlib
import os

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except Exception:  # noqa: BLE001
    _HAS_CRYPTO = False


# 派生密钥参数
PBKDF2_ITERATIONS = 200_000
# 加密文件头
MAGIC = b"MDNOTES_ENC1|"


def available() -> bool:
    return _HAS_CRYPTO


def derive_key(password: str) -> str:
    """从主密码派生 32 字节密钥，返回 base64（urlsafe）字符串利于存 JSON。"""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        b"mdnotes-note-encrypt-v1",  # 固定应用级盐
        PBKDF2_ITERATIONS,
        dklen=32,
    )
    return base64.urlsafe_b64encode(dk).decode("ascii")


def _key_bytes(key_b64: str) -> bytes:
    return base64.urlsafe_b64decode(key_b64.encode("ascii"))


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt_bytes(data: bytes, key_b64: str) -> bytes:
    """加密并序列化（返回可写盘的字节）。"""
    if not _HAS_CRYPTO:
        raise RuntimeError("缺少 cryptography 依赖，无法加密")
    key = _key_bytes(key_b64)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return MAGIC + base64.b64encode(nonce + ct)


def decrypt_bytes(data: bytes, key_b64: str) -> bytes:
    """尝试解密；密码/密钥错误或数据损坏返回空字节。"""
    if not data.startswith(MAGIC):
        return data  # 未加密，原样返回
    try:
        key = _key_bytes(key_b64)
        blob = base64.b64decode(data[len(MAGIC):])
        nonce, ct = blob[:12], blob[12:]
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception:  # noqa: BLE001
        return b""


def rekey_bytes(data: bytes, old_key_b64: str, new_key_b64: str) -> bytes:
    """用旧密钥解出明文，再用新密钥重新加密。"""
    plain = decrypt_bytes(data, old_key_b64)
    return encrypt_bytes(plain, new_key_b64)