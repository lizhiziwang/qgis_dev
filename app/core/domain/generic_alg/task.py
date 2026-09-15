"""通用算法后台执行任务（:class:`QgsProcessingAlgRunnerTask`）。

与 QGIS processing 对话框同一运行机制：算法在 worker 线程执行，主线程事件
循环不阻塞；``executed(successful, results)`` 信号把结果送回主线程，图层装配
交给 :meth:`GenericAlgService.wrap_results` 在**主线程**完成（worker 线程内不
创建 ``QgsMapLayer``，避免跨线程 QObject 亲缘问题）。

进度转发：``feedback.progressChanged`` 只接一个普通 Python 方法重发
:attr:`progress_updated` 信号（直连 widget 槽在本构建会原生崩溃
``0xC0000409``，见 memory / ``BufferDialog`` 注释），由对话框订阅该信号后再
驱动进度条。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsProcessingAlgRunnerTask, QgsProcessingFeedback

if TYPE_CHECKING:
    from qgis.core import QgsProcessingAlgorithm, QgsProcessingContext


class GenericAlgRunnerTask(QgsProcessingAlgRunnerTask):
    """:class:`GenericAlgJob` 的后台执行任务。

    相比裸 :class:`QgsProcessingAlgRunnerTask`，只多一个
    :attr:`progress_updated` 信号：把 ``feedback.progressChanged``（数值
    ``float``）转成 ``int`` 百分比，绕开「信号直连控件槽 → 原生崩溃」的坑。
    """

    #: 算法进度 0–100（worker 线程发出 → 队列投递到主线程普通方法）
    progress_updated = pyqtSignal(int)

    def __init__(
        self,
        algorithm: "QgsProcessingAlgorithm",
        parameters: dict,
        context: "QgsProcessingContext",
        feedback: QgsProcessingFeedback,
        prepared_params: dict | None = None,
    ) -> None:
        super().__init__(algorithm, parameters, context, feedback)
        #: 保留 ``prepare`` 清洗后的参数（含临时 memory 输入图层）；
        #: 这些图层未被任何 project 持有，若 Python 引用被 GC，C++ 任务仍在使用
        #: → 悬垂。调用方需在任务结束前持有本任务（对话框的 ``self._task``）。
        self.prepared_params = prepared_params or {}
        # 只接普通方法重发信号：直连控件槽本构建会原生崩溃（0xC0000409）
        feedback.progressChanged.connect(self._on_progress)

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))


__all__ = ["GenericAlgRunnerTask"]
