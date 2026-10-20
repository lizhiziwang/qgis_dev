"""训练子进程客户端（QProcess + 单行 JSON 事件）。

训练可能持续数小时，因此**不设**请求超时；只有在用户主动停止、进程崩溃或
退出时结束。stdout 只解析协议，stderr 保留尾部用于异常提示。
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from app.core.ai.sam_environment import clean_process_environment
from app.core.training.trainlib.common import STOP_REQUEST_NAME
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

_STDERR_TAIL = 16384
_MAX_LINE_BYTES = 8 * 1024 * 1024
#: 优雅停止的兜底时间：子进程在保存完当前轮后仍未退出就强杀
STOP_GRACE_MS = 15000


def default_sidecar_path() -> Path:
    """开发态取工程目录，冻结态取 PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Paths.default().project_root
    return base / "app" / "core" / "training" / "train_sidecar.py"


class TrainingClient(QObject):
    """训练子进程的 GUI 线程侧封装。"""

    hello = pyqtSignal()
    log = pyqtSignal(str)
    started = pyqtSignal(object)
    progress = pyqtSignal(object)
    epoch_finished = pyqtSignal(object)
    finished = pyqtSignal(object)
    stopped = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    running_changed = pyqtSignal(bool)
    status_changed = pyqtSignal(str)

    def __init__(
        self,
        *,
        python: str = "",
        sidecar_path: str | Path | None = None,
        label: str = "训练",
        stop_hint: str = "（等待当前轮次保存）",
        stop_grace_ms: int = STOP_GRACE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = python
        self._sidecar_path = Path(sidecar_path) if sidecar_path else default_sidecar_path()
        self._label = str(label or "训练")
        self._stop_hint = str(stop_hint)
        self._stop_grace_ms = max(1000, int(stop_grace_ms))
        self._process: QProcess | None = None
        self._manifest_path: Path | None = None
        self._buffer = b""
        self._stderr_tail = ""
        self._running = False
        self._status = ""
        self._terminal = False
        self._killing = False
        self._stop_timer: QTimer | None = None

    # ------------------------------------------------------------------ 状态
    @property
    def python(self) -> str:
        return self._python

    @python.setter
    def python(self, value: str) -> None:
        self._python = value

    @property
    def sidecar_path(self) -> Path:
        return self._sidecar_path

    @property
    def label(self) -> str:
        return self._label

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, text: str) -> None:
        self._status = text
        self.status_changed.emit(text)

    def _set_running(self, running: bool) -> None:
        if self._running != running:
            self._running = running
            self.running_changed.emit(running)

    # ------------------------------------------------------------------ 校验
    def check_environment(self) -> str | None:
        if not self._python or not Path(self._python).is_file():
            return f"{self._label} Python 不存在：{self._python or '（未配置）'}"
        if not self._sidecar_path.is_file():
            return f"缺少{self._label}子进程脚本：{self._sidecar_path}"
        return None

    # ------------------------------------------------------------------ 进程
    def start(self, manifest_path: str | Path) -> bool:
        """启动子进程；返回是否成功发起进程。"""
        if self._running:
            return False
        problem = self.check_environment()
        if problem:
            self.failed.emit(problem, "environment")
            return False
        manifest = Path(manifest_path)
        if not manifest.is_file():
            self.failed.emit(f"{self._label}清单不存在：{manifest}", "environment")
            return False

        self._buffer = b""
        self._stderr_tail = ""
        self._terminal = False
        self._killing = False
        self._manifest_path = manifest
        # 清掉上次运行残留，避免刚启动就被判为“已请求停止”
        with contextlib.suppress(OSError):
            (manifest.parent / STOP_REQUEST_NAME).unlink()

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.setProgram(self._python)
        process.setArguments(["-u", str(self._sidecar_path), "--manifest", str(manifest)])
        process.setProcessEnvironment(
            clean_process_environment(QProcessEnvironment.systemEnvironment())
        )
        process.setWorkingDirectory(str(manifest.parent))
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_process_error)
        self._process = process
        self._set_running(True)
        self._set_status(f"正在启动{self._label}进程…")
        process.start()
        return True

    def stop(self, *, graceful: bool = True) -> None:
        """请求停止。``graceful`` 会等当前批次保存完；否则立即 kill。"""
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return
        if not graceful:
            self._kill(f"{self._label}已强制停止")
            return
        self._set_status(f"正在停止{self._label}{self._stop_hint}…")
        manifest = self._manifest_path
        if manifest is None:
            self._kill(f"{self._label}进程通信中断，已强制停止")
            return
        try:
            (manifest.parent / STOP_REQUEST_NAME).write_text("stop\n", encoding="utf-8")
        except OSError:
            self._kill("无法写入停止请求，已强制停止")
            return
        if self._stop_timer is None:
            self._stop_timer = QTimer(self)
            self._stop_timer.setSingleShot(True)
            self._stop_timer.timeout.connect(self._on_stop_timeout)
        self._stop_timer.start(self._stop_grace_ms)

    def shutdown(self) -> None:
        """窗口关闭时调用：直接终止，不等当前批次。"""
        self._kill(f"{self._label}进程已关闭")
        process = self._process
        if process is not None:
            process.waitForFinished(2000)
            process.deleteLater()
        self._process = None
        self._set_running(False)

    def _kill(self, message: str) -> None:
        process = self._process
        if process is not None:
            self._killing = True
            process.kill()
            process.waitForFinished(1000)
        self._set_running(False)
        self._set_status(message)

    def _on_stop_timeout(self) -> None:
        if self._running:
            self._kill(f"{self._label}进程未在限时内退出，已强制停止")

    # ------------------------------------------------------------------ 输出
    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput())
        if not chunk:
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            if len(raw) > _MAX_LINE_BYTES:
                self.failed.emit(f"{self._label}进程输出超长，已终止", "protocol")
                self._terminal = True
                self._kill(f"{self._label}进程输出异常")
                return
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.warning("忽略无法解析的%s输出：%r", self._label, raw[:200])
                continue
            self._dispatch(message)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None:
            return
        text = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if not text:
            return
        self._stderr_tail = (self._stderr_tail + text)[-_STDERR_TAIL:]
        logger.debug("%s stderr: %s", self._label, text.rstrip())

    def _dispatch(self, message: dict) -> None:
        event = str(message.get("event") or "")
        if event == "hello":
            self._set_status(f"{self._label}进程已就绪")
            self.hello.emit()
            return
        if event == "log":
            self.log.emit(str(message.get("message") or ""))
            return
        if event == "started":
            self._set_status(f"{self._label}进行中…")
            self.started.emit(message)
            return
        if event == "progress":
            self.progress.emit(message)
            return
        if event == "epoch":
            self.epoch_finished.emit(message)
            return
        if event == "done":
            self._terminal = True
            self._set_status(f"{self._label}完成")
            self.finished.emit(message)
            return
        if event == "stopped":
            self._terminal = True
            self._set_status(f"{self._label}已停止")
            self.stopped.emit(message)
            return
        if event == "error":
            self._terminal = True
            text = str(message.get("message") or f"{self._label}失败")
            error_type = str(message.get("error_type") or "internal")
            self._set_status(text)
            self.failed.emit(text, error_type)
            return
        logger.debug("未处理的%s事件：%s", self._label, event)

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if self._killing:
            return
        process = self._process
        message = (
            f"{self._label}进程启动失败：{process.errorString() if process else error}"
        )
        self._set_running(False)
        self._set_status(message)
        if not self._terminal:
            self._terminal = True
            self.failed.emit(message, "process")
        self._cleanup_process()

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._set_running(False)
        if self._stop_timer is not None:
            self._stop_timer.stop()
        if self._killing:
            self._cleanup_process()
            return
        if self._terminal:
            self._cleanup_process()
            return
        tail = self._stderr_tail.strip()
        message = f"{self._label}进程意外退出（code={exit_code}）"
        if tail:
            message += f"\n{tail[-3000:]}"
        self._terminal = True
        self._set_status(message.splitlines()[0])
        logger.warning("%s子进程退出：%s", self._label, message)
        self._cleanup_process()
        self.failed.emit(message, "process")

    def _cleanup_process(self) -> None:
        process = self._process
        if process is not None:
            process.deleteLater()
        self._process = None

    def wait_for_finished(self, timeout_ms: int = 2000) -> bool:
        process = self._process
        if process is None:
            return True
        with contextlib.suppress(Exception):
            return process.waitForFinished(timeout_ms)
        return True


__all__ = ["STOP_GRACE_MS", "TrainingClient", "default_sidecar_path"]
