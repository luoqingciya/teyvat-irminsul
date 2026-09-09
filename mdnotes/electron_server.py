"""Electron 混合架构下的本地 HTTP 后端服务。

把 ``webapi.WebAPI`` 封装为仅监听 127.0.0.1 随机端口的本地 HTTP 服务，
供 Electron 渲染进程通过 ``fetch`` 调用（见前端 app.js 的 ``call()``）。

约定：
- ``GET /health``        返回存活状态
- ``POST /api/<method>`` body 为 ``{"args": [...]}``，逐项透传给 WebAPI 方法
- 启动后向 stdout 打印一行 ``PORT=<port>``，Electron 主进程据此建立连接

启动方式：
    python run_backend.py            # PyInstaller sidecar 入口
    python -m mdnotes.electron_server
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import config as cfg
from .logger import log
from .webapi import WebAPI


class _BoundedHTTPServer(ThreadingHTTPServer):
    """线程化 HTTP 服务，限制最大并发请求数并启用守护线程。

    - ``daemon_threads``：应用退出时不等待长连接线程，避免卡住退出。
    - 信号量：防止批量操作（重建索引等）引发线程数失控。
    """

    daemon_threads = True

    def __init__(self, *args: Any, max_workers: int = 16, **kwargs: Any) -> None:
        self._semaphore = threading.Semaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self._semaphore:
            super().process_request_thread(request, client_address)


class _ApiHandler(BaseHTTPRequestHandler):
    """单请求处理器：解析 /api/<method>，转发到共享的 WebAPI 实例。"""

    api: WebAPI = None  # type: ignore[assignment]  # 由 start_server 注入
    api_token: str = ""  # 由 start_server 注入；非空时校验 X-Auth-Token 请求头

    # 禁用默认访问日志（stdout 需保持只输出 PORT=，供主进程解析）
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
        pass

    # ── 基础响应 ──────────────────────────────────────────

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"success": False, "error": "not found"})

    # ── API 分发 ──────────────────────────────────────────

    def _authorized(self) -> bool:
        """token 校验：配置了 api_token 时，请求必须携带匹配的 X-Auth-Token。"""
        return not self.api_token or self.headers.get("X-Auth-Token") == self.api_token

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send_json(404, {"success": False, "error": "not found"})
            return
        method = path[len("/api/"):]
        if not method or not method.isidentifier() or method.startswith("_"):
            self._send_json(404, {"success": False, "error": f"未知方法: {method}"})
            return

        # 安全：拒绝无 token 的跨源请求（防本地恶意网页读写笔记）
        if not self._authorized():
            self._send_json(401, {"success": False, "error": "未授权"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"success": False, "error": f"无效请求: {exc}"})
            return

        args = payload.get("args") if isinstance(payload.get("args"), list) else []
        fn = getattr(self.api, method, None)
        if fn is None:
            self._send_json(404, {"success": False, "error": f"未知方法: {method}"})
            return

        try:
            result = fn(*args)
        except TypeError as exc:
            self._send_json(400, {"success": False, "error": f"参数错误: {exc}"})
            return
        except Exception:  # noqa: BLE001
            # 详细堆栈仅记录到日志，避免向客户端泄露内部信息
            log.exception("API 调用失败: %s", method)
            self._send_json(500, {"success": False, "error": "服务器内部错误"})
            return

        self._send_json(200, result)


def start_server(api: WebAPI | None = None, api_token: str = "") -> tuple[_BoundedHTTPServer, int]:
    """启动线程化 HTTP 服务（随机端口）。返回 (server, port)。"""
    api = api or WebAPI()
    server = _BoundedHTTPServer(("127.0.0.1", 0), _ApiHandler)
    _ApiHandler.api = api
    _ApiHandler.api_token = api_token
    return server, int(server.server_address[1])


def main() -> None:
    """后端 sidecar 入口：初始化 + 启动 HTTP 服务 + 打印 PORT。"""
    # 加载 .env（WebDAV 密码）
    try:
        from dotenv import load_dotenv
        load_dotenv(cfg.get_project_root() / ".env")
    except ImportError:
        pass

    cfg.upgrade_config_format()

    api = WebAPI()
    api.start_webdav_auto_sync()

    # 访问令牌：由 Electron 主进程生成并通过环境变量注入，前端携带 X-Auth-Token
    api_token = os.environ.get("MDNOTES_API_TOKEN") or ""
    server, port = start_server(api, api_token)
    log.info("后端 HTTP 服务启动: http://127.0.0.1:%d", port)
    # 主进程 stdout 只输出这一行，供 Electron 解析（保持只含 PORT= 前缀）
    print(f"PORT={port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("后端服务退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
