"""最短路径后台执行任务（与 QGIS processing 对话框同一运行机制）。

基类 :class:`QgsProcessingAlgRunnerTask` 正是 QGIS processing 对话框执行算法用的
任务类：算法在 worker 线程运行，主线程事件循环不阻塞 → 窗口保持响应；调用
``task.cancel()`` 会转发到 ``feedback.cancel()``，算法随之尽早停止。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import pyqtSignal
from qgis.core import QgsProcessingAlgRunnerTask, QgsProcessingFeedback

if TYPE_CHECKING:
    from qgis.core import QgsProcessingAlgorithm, QgsProcessingContext


class ShortestPathFeedback(QgsProcessingFeedback):
    """捕获算法错误/信息消息的反馈对象。

    本构建的 ``QgsProcessingFeedback`` 没有 ``errorOccurred`` 信号（已探针验证），
    算法通过虚方法 ``reportError`` / ``pushInfo`` 上报消息；这里覆写收集，供对话框
    在失败时展示（如 "There is no route from start point to end point."）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []
        self.infos: list[str] = []

    def reportError(self, error: str, fatalError: bool = False) -> None:
        self.errors.append(str(error))
        return super().reportError(error, fatalError)

    def pushInfo(self, info: str) -> None:
        self.infos.append(str(info))
        return super().pushInfo(info)


class ShortestPathRunnerTask(QgsProcessingAlgRunnerTask):
    """后台执行 ``native:shortestpathpointtopoint`` 的任务。

    进度转发：``feedback.progressChanged`` 直接接到 QObject/widget 槽在本构建会
    原生崩溃（0xC0000409，见 memory），这里只接一个普通方法重发
    :attr:`progress_updated` 信号，由对话框订阅该信号再驱动进度条。
    """

    progress_updated = pyqtSignal(int)

    def __init__(
        self,
        algorithm: "QgsProcessingAlgorithm",
        parameters: dict,
        context: "QgsProcessingContext",
        feedback: ShortestPathFeedback,
    ) -> None:
        super().__init__(algorithm, parameters, context, feedback)
        feedback.progressChanged.connect(self._on_progress)

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))


__all__ = ["ShortestPathFeedback", "ShortestPathRunnerTask"]
