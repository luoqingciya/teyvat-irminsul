"""访问令牌哈希化工具。

- token 使用 PBKDF2-HMAC-SHA256 哈希后存储，盐值随机生成。
- 桌面版仅用于配置中的 server.auth_token 字段哈希存储（Web 版残留配置）。
"""

from __future__ import annotations

import hashlib
import secrets


TOKEN_HASH_PREFIX = "pbkdf2_sha256$"
ITERATIONS = 100_000


def hash_token(token: str) -> str:
    """对明文 token 做 PBKDF2 哈希，返回 `pbkdf2_sha256$iter$salt$hash`。"""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt.encode("ascii"), ITERATIONS)
    return f"{TOKEN_HASH_PREFIX}{ITERATIONS}${salt}${key.hex()}"


def is_token_hashed(stored: str | None) -> bool:
    return bool(stored and stored.startswith(TOKEN_HASH_PREFIX))
