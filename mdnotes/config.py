import copy
import base64
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import auth

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except Exception:  # noqa: BLE001 缺少 cryptography 时降级为明文（仅影响 WebDAV 密码存储）
    _HAS_CRYPTO = False

# WebDAV 密码保密存储：`wd_enc1:` 前缀标记 AES-256-GCM 密文，
# 密钥由当前用户名派生。防「config.json 被随手翻阅/拷走」场景；
# 运行期 load_config 自动解密回明文使用（本机同用户可透明解密）。
_SECRET_PREFIX = "wd_enc1:"


def _secret_key() -> bytes:
    raw = (getpass.getuser() + "@teyvat-irminsul-webdav-v1").encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", raw, b"mdnotes-webdav-iv1", 50_000, dklen=32)


def _encrypt_secret(plain: str) -> str:
    """加密 WebDAV 密码为带前缀的密文串；无法加密时原样返回。"""
    if not _HAS_CRYPTO or not plain or plain.startswith(_SECRET_PREFIX):
        return plain
    try:
        key = _secret_key()
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), None)
        return _SECRET_PREFIX + base64.b64encode(nonce + ct).decode("ascii")
    except Exception:  # noqa: BLE001
        return plain


def _decrypt_secret(stored: str) -> str:
    """解密存储的 WebDAV 密码；非加密串原样返回，解密失败返回空串。"""
    if not stored.startswith(_SECRET_PREFIX):
        return stored
    try:
        key = _secret_key()
        blob = base64.b64decode(stored[len(_SECRET_PREFIX):])
        nonce, ct = blob[:12], blob[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


DEFAULT_CONFIG = {
    "notes_dir": "notes",
    "server": {"host": "0.0.0.0", "port": 8000, "auth_token": "", "lan_enabled": True},
    "webdav": {
        "enabled": False,
        "url": "",
        "username": "",
        "password": "",
        "remote_dir": "/mdnotes",
        "incremental": True,
        "auto_sync": False,
        "auto_sync_interval_min": 30,
    },
    "image": {
        "max_width": 1920,
        "quality": 85,
        "compress": True,
    },
    "editor": {
        "typewriter_scroll": True,
        "scroll_linked": True,
        "auto_save": True,
        "auto_save_delay_ms": 2000,
        "restore_session": True,
        "keymap": {},
    },
    "app_lock": {
        "enabled": False,
        "password": "",
    },
    "encrypt": {
        "key": "",
        "notes": {},
    },
    "tags": {
        "colors": {},
    },
    "saved_searches": [],
}


def get_project_root() -> Path:
    """返回项目根目录（pyproject.toml 所在目录）。

    PyInstaller 打包后，资源/数据文件与可执行文件放在同一目录，
    因此以 exe 所在目录作为项目根目录。
    但 Electron 混合架构中后端作为 sidecar 位于只读的 resources/backend/，
    主进程会通过环境变量 ``MDNOTES_DATA_DIR`` 指定用户可写的数据目录
    （config.json / 默认笔记目录 / 日志都落在这里）。
    """
    if getattr(sys, "frozen", False):
        data_dir = os.environ.get("MDNOTES_DATA_DIR")
        if data_dir:
            return Path(data_dir)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_config_path() -> Path:
    env_path = os.environ.get("MDNOTES_CONFIG")
    if env_path:
        return Path(env_path)
    return get_project_root() / "config.json"


def upgrade_config_format() -> None:
    """启动时一次性升级旧配置格式：明文 token 哈希化。

    该函数只应在应用启动前显式调用一次，避免在 load_config() 中隐式写盘。
    """
    config_path = get_config_path()
    if not config_path.exists():
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    changed = False

    # 访问令牌哈希化
    token = data.get("server", {}).get("auth_token", "")
    if token and not auth.is_token_hashed(token):
        data.setdefault("server", {})["auth_token"] = auth.hash_token(token)
        data.setdefault("server", {})["_plaintext_token"] = token
        changed = True

    if changed:
        save_config(data)


def load_config() -> dict[str, Any]:
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"配置文件解析失败: {config_path}") from exc
    else:
        data = {}

    # 深度合并默认值
    config = deep_merge(DEFAULT_CONFIG.copy(), data)
    # 解析环境变量占位符
    config["webdav"]["url"] = _expand(config["webdav"].get("url", ""))
    config["webdav"]["username"] = _expand(config["webdav"].get("username", ""))

    # 访问令牌：load_config() 不再隐式写盘升级；由 upgrade_config_format() 在启动时处理
    token = config["server"].get("auth_token", "")
    if auth.is_token_hashed(token):
        config["server"]["_plaintext_token"] = ""
    else:
        # 若配置仍是明文（未升级或新建空配置），运行时保留明文用于兼容，不写盘
        config["server"]["_plaintext_token"] = token

    if os.environ.get("MDNOTES_WEBDAV_PASSWORD"):
        config["webdav"]["password"] = os.environ["MDNOTES_WEBDAV_PASSWORD"]
    else:
        config["webdav"]["password"] = _decrypt_secret(config["webdav"].get("password", ""))

    # 确保笔记目录存在
    notes_dir = Path(config["notes_dir"])
    if not notes_dir.is_absolute():
        notes_dir = get_project_root() / notes_dir
    notes_dir.mkdir(parents=True, exist_ok=True)
    config["notes_dir"] = str(notes_dir.resolve())

    return config


def save_config(config: dict[str, Any]) -> None:
    config_path = get_config_path()
    # 写入时隐藏环境变量里持有的密码，避免明文泄露
    data = deep_merge(DEFAULT_CONFIG.copy(), config)
    # 移除运行时明文标记，不写入磁盘
    if "_plaintext_token" in data.get("server", {}):
        del data["server"]["_plaintext_token"]
    # 访问令牌强制哈希存储
    token = data.get("server", {}).get("auth_token", "")
    if token and not auth.is_token_hashed(token):
        data["server"]["auth_token"] = auth.hash_token(token)
    if os.environ.get("MDNOTES_WEBDAV_PASSWORD"):
        data["webdav"]["password"] = ""
    elif data.get("webdav", {}).get("password"):
        data["webdav"]["password"] = _encrypt_secret(str(data["webdav"]["password"]))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深合并：返回全新 dict，不污染 DEFAULT_CONFIG 等被复用对象。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _expand(value: str) -> str:
    if value is None:
        return ""
    # 支持 ${VAR} 与 $VAR 形式的环境变量
    return os.path.expandvars(str(value))
