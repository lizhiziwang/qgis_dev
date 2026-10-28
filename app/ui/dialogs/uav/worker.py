"""无人机向导的后台任务。

沿用仓库既有模式：普通 :class:`threading.Thread` + Qt 信号回主线程；
**不要**在后台线程里碰任何 QGIS / Qt 控件。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from qgis.PyQt.QtCore import QObject, pyqtSignal

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 后台函数签名：(progress(percent, message), cancelled() -> bool) -> object
TaskFunction = Callable[[Callable[[int, str], None], Callable[[], bool]], object]


class UavWorker(QObject):
    """在后台线程执行一个可调用对象，进度/结果经信号回主线程。"""

    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, function: TaskFunction, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._function = function
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="uav-wizard-worker", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _emit_progress(self, percent: int, message: str = "") -> None:
        self.progress.emit(int(percent), str(message))

    def _run(self) -> None:
        try:
            result = self._function(self._emit_progress, self._cancel.is_set)
        except Exception as exc:
            logger.exception("无人机向导任务失败")
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        if self._cancel.is_set():
            self.failed.emit("任务已取消")
            return
        self.finished.emit(result)


__all__ = ["UavWorker"]
