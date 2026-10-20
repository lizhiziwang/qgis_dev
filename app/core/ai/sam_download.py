"""SAM 权重下载任务（后台线程 + 断点续传）。

线程模型与离线下载一致：``QObject`` 存活在 GUI 线程，worker 是 daemon 线程，
进度/日志/完成全部经信号回到主线程；调用方**先连信号再 start()**。

权重文件较大（最大约 2.5 GB），下载写 ``<文件名>.part``，支持 HTTP Range 续传；
取消只停止当前循环，``.part`` 保留，下次可接着下。
"""

from __future__ import annotations

import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from qgis.PyQt.QtCore import QObject, pyqtSignal

from app.infra.logging import get_logger

logger = get_logger(__name__)

_CHUNK = 64 * 1024
_TIMEOUT = 60.0
_RETRIES = 3
_PROGRESS_INTERVAL = 0.25
_USER_AGENT = "qgis-dev/1.0 (SAM model downloader)"


class DownloadCanceledError(Exception):
    """用户取消下载。"""


class DownloadAuthError(OSError):
    """权重仓库需要授权（HuggingFace 401/403），重试无意义。"""


def resolve_download_headers(url: str) -> dict[str, str]:
    """HuggingFace 受限仓库自动带上环境变量里的访问令牌。"""
    host = (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    if host != "huggingface.co" and not host.endswith(".huggingface.co"):
        return {}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
    return {"Authorization": f"Bearer {token}"} if token.strip() else {}


class ModelDownloadTask(QObject):
    """下载一个权重文件到 ``target``（可断点续传）。"""

    progress = pyqtSignal(int, int)  # 已下载字节 / 总字节（0 = 未知）
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)  # ok, message

    def __init__(
        self,
        url: str,
        target: str | Path,
        parent: QObject | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._url = str(url)
        self._target = Path(target)
        self._headers = {str(key): str(value) for key, value in dict(headers or {}).items()}
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = False

    @property
    def target(self) -> Path:
        return self._target

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"sam-download-{self._target.name}", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float = 1.0) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        try:
            self._target.parent.mkdir(parents=True, exist_ok=True)
            part = self._target.with_name(self._target.name + ".part")
            self.log.emit(f"开始下载 {self._target.name}")
            for attempt in range(_RETRIES):
                self._check_cancel()
                try:
                    self._download_once(part)
                    os.replace(part, self._target)
                    size = self._target.stat().st_size
                    self.log.emit(f"下载完成：{self._target.name}（{_format_size(size)}）")
                    self._emit_finished(True, str(self._target))
                    return
                except DownloadCanceledError:
                    raise
                except DownloadAuthError as exc:
                    # 授权失败重试没有意义，直接给出可操作的提示
                    self.log.emit(str(exc))
                    self._emit_finished(False, str(exc))
                    return
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    if attempt + 1 >= _RETRIES:
                        raise
                    self.log.emit(f"下载失败，重试 {attempt + 1}/{_RETRIES}：{exc}")
                    self._wait(0.8 * (attempt + 1))
            raise OSError("下载多次失败")
        except DownloadCanceledError:
            self.log.emit("下载已取消（.part 文件保留，可续传）")
            self._emit_finished(False, "已取消")
        except Exception as exc:  # noqa: BLE001 —— 后台线程必须把错误回传
            logger.warning("SAM 权重下载失败 %s：%s", self._url, exc)
            self.log.emit(f"下载失败：{exc}")
            self._emit_finished(False, str(exc))

    def _download_once(self, part: Path) -> None:
        offset = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(self._url, headers={"User-Agent": _USER_AGENT, **self._headers})
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        context = ssl.create_default_context()
        try:
            response = urllib.request.urlopen(request, timeout=_TIMEOUT, context=context)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DownloadAuthError(
                    f"权重仓库需要授权（HTTP {exc.code}）。SAM3 权重需先在 HuggingFace 申请访问，"
                    "并在环境变量 HF_TOKEN 中配置令牌后重试；也可手动下载后点「导入本地权重…」。"
                ) from exc
            raise
        with response as response:
            status = int(getattr(response, "status", 200))
            if status == 416:
                if offset:
                    return
                raise OSError("服务器返回 416 且没有已下载内容")
            if status == 200 and offset:
                offset = 0
                part.unlink(missing_ok=True)
            length = int(response.headers.get("Content-Length") or 0)
            total = offset + length if length > 0 else 0
            done = offset
            last_emit = 0.0
            mode = "ab" if status == 206 and offset else "wb"
            with open(part, mode) as handle:
                while True:
                    self._check_cancel()
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= _PROGRESS_INTERVAL:
                        self.progress.emit(done, total)
                        last_emit = now
                handle.flush()
                os.fsync(handle.fileno())
            self.progress.emit(done, total)
            if total and done < total:
                raise OSError(f"下载不完整（{done}/{total} 字节）")

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise DownloadCanceledError

    def _wait(self, seconds: float) -> None:
        if self._cancel.wait(seconds):
            raise DownloadCanceledError

    def _emit_finished(self, ok: bool, message: str) -> None:
        if self._finished:
            return
        self._finished = True
        self.finished.emit(bool(ok), str(message))


def _format_size(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


__all__ = [
    "DownloadAuthError",
    "DownloadCanceledError",
    "ModelDownloadTask",
    "resolve_download_headers",
]
