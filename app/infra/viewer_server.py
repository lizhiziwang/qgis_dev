"""本机临时 HTTP 服务：用于打开瓦片网页查看器。

矢量瓦片查看器用 XHR 读取 ``{z}/{x}/{y}.pbf``。浏览器对 ``file://`` 页面发起
的跨源请求一律拦截（控制台报 CORS / ``Access to XMLHttpRequest ... has been
blocked``），页面因此空白。应用点「打开网页查看器」时改为：

1. 在 ``127.0.0.1`` 上按输出目录启动一个只读的临时 HTTP 服务（端口随机）；
2. 用默认浏览器打开 ``http://127.0.0.1:<端口>/index.html``。

服务仅监听回环地址、只暴露指定目录，进程退出即结束（守护线程）。
同一目录重复打开会复用已有服务，不会每次新开端口。
"""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from app.infra.logging import get_logger

logger = get_logger(__name__)

_servers: dict[str, ThreadingHTTPServer] = {}
_lock = threading.Lock()


class _ViewerRequestHandler(SimpleHTTPRequestHandler):
    """静默日志、禁用缓存，并补上瓦片格式的 MIME 类型。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".pbf": "application/x-protobuf",
        ".mvt": "application/x-protobuf",
        ".mbtiles": "application/octet-stream",
    }

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 签名固定
        logger.debug("查看器 HTTP: " + format, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _start_server(root: str) -> ThreadingHTTPServer | None:
    try:
        handler = functools.partial(_ViewerRequestHandler, directory=root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError:
        logger.error("启动查看器 HTTP 服务失败：%s", root, exc_info=True)
        return None
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever,
        name="viewer-http",
        daemon=True,
    ).start()
    return server


def serve_directory(directory: str) -> str | None:
    """确保 ``directory`` 可通过本机 HTTP 访问，返回以 ``/`` 结尾的 URL。"""
    root = str(Path(directory).resolve())
    with _lock:
        server = _servers.get(root)
        if server is None:
            server = _start_server(root)
            if server is None:
                return None
            _servers[root] = server
        host, port = server.server_address[:2]
    return f"http://{host}:{port}/"


def viewer_url(html_path: str) -> str | None:
    """把本地 ``index.html`` 路径转换成可访问的 HTTP URL。"""
    path = Path(html_path).resolve()
    if not path.is_file():
        return None
    base = serve_directory(str(path.parent))
    if not base:
        return None
    return base + quote(path.name)


def open_viewer(html_path: str) -> bool:
    """用默认浏览器打开查看器；HTTP 服务不可用时退回 ``file://``。"""
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtGui import QDesktopServices

    path = Path(html_path).resolve()
    if not path.is_file():
        return False
    url = viewer_url(str(path))
    target = QUrl(url) if url else QUrl.fromLocalFile(str(path))
    try:
        return bool(QDesktopServices.openUrl(target))
    except Exception:  # noqa: BLE001 - 打开失败由调用方提示
        logger.error("打开查看器失败：%s", target.toString(), exc_info=True)
        return False


def shutdown_viewer_servers() -> None:
    """关闭所有查看器服务（测试收尾用；正常退出时守护线程自动结束）。"""
    with _lock:
        servers = list(_servers.items())
        _servers.clear()
    for root, server in servers:
        try:
            server.shutdown()
            server.server_close()
        except OSError:
            logger.debug("关闭查看器服务失败：%s", root, exc_info=True)


__all__ = ["open_viewer", "serve_directory", "shutdown_viewer_servers", "viewer_url"]
