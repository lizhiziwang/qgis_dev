"""影像切片（栅格瓦片）后台任务。

在普通 :class:`threading.Thread` 里执行 ``native:tilesxyzdirectory``。

.. note::
   不用 :class:`QgsTask`：实测 ``native:tilesxyzdirectory`` 内部用
   :class:`QgsMapRendererParallelJob` 渲染，经 :class:`QgsTaskManager`
   后台线程运行会原生崩溃（退出码 127）；改走普通 ``threading.Thread``
   则稳定——主线程事件循环照常泵事件，进度/日志/完成经信号跨线程投递到
   主线程（与仓库既有线程规则一致）。

进度经 :attr:`progress_updated` 信号回报（``0~100`` int）；算法输出经
:attr:`log_message` 信号回报（``level, message``），同时写入应用日志
（日志面板 / ``logs/app.log``）。

日志捕获经 :class:`ProcessingLogFeedback`（见 :mod:`.feedback`）完成——
QGIS 3.40 的 :class:`QgsProcessingFeedback` 信号面很窄，算法日志走 ``push*``
虚方法回调，由该类覆写并转成信号；与 MBTiles 切片工具共用。
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, pyqtSignal

from app.infra.logging import get_logger

from .feedback import ProcessingLogFeedback
from .service import TILE_FORMAT_LABELS, ImageryTileJob

logger = get_logger(__name__)


class CreateImageryTileTask(QObject):
    """影像切片任务（普通线程执行）。

    提供与旧 :class:`QgsTask` 兼容的 ``taskCompleted`` / ``taskTerminated``
    信号与 ``exception`` 属性，对话框无需改动收尾逻辑。
    """

    progress_updated = pyqtSignal(int)
    #: (level, message)；level ∈ {INFO, CMD, DEBUG, WARNING, ERROR}，
    #: 供对话框实时展示任务日志。
    log_message = pyqtSignal(str, str)
    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()

    def __init__(self, description: str, job: ImageryTileJob) -> None:
        super().__init__()
        self.job = job
        self.exception: object = None
        self.result_output: dict = {}
        self.feedback = ProcessingLogFeedback()
        self.feedback.progressChanged.connect(self._on_progress)
        # feedback.log_line 在渲染线程发出 → 投递到主线程的 _on_log_line
        # （普通 Python 方法，投递安全，见进度信号同款处理）
        self.feedback.log_line.connect(self._on_log_line)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台线程。

        调用前请先连接好 ``taskCompleted`` / ``taskTerminated`` 等信号——
        快速任务可能立即完成，错过连接就会丢完成事件。
        """
        self._thread = threading.Thread(
            target=self._run, name="imagery-tile", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        """请求取消（算法在切片间隙检查 ``feedback.isCanceled()``）。"""
        if self.feedback is not None and not self.feedback.isCanceled():
            self.feedback.cancel()

    # ------------------------------------------------------------ 回调

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

    def _on_log_line(self, level: str, message: str) -> None:
        """渲染线程算法日志经队列投递到这里（主线程），转发给应用日志与 UI。"""
        self._log_line(level, message)

    def _log_line(self, level: str, message: str) -> None:
        """记录一行日志：写入应用日志（线程安全）并转发 :attr:`log_message`。"""
        text = str(message).rstrip()
        if not text:
            return
        _WRITE = {
            "INFO": logger.info,
            "CMD": logger.info,
            "DEBUG": logger.debug,
            "WARNING": logger.warning,
            "ERROR": logger.error,
        }
        _WRITE.get(level, logger.info)(text)
        self.log_message.emit(level, text)

    # ------------------------------------------------------------ 执行

    def _run(self) -> None:
        try:
            # 延迟 import：避免 service 模块顶层依赖 processing
            import processing

            job = self.job
            self._log_line("INFO", f"开始影像切片：{job.layer.name()}")
            self._log_line(
                "INFO",
                f"参数：缩放 {job.min_zoom}-{job.max_zoom}，"
                f"格式 {TILE_FORMAT_LABELS[job.tile_format]}，"
                f"瓦片 {job.tile_size}×{job.tile_size}px，"
                f"抗锯齿 {'开' if job.antialias else '关'}，"
                f"网页查看器 {'生成' if job.html else '不生成'}",
            )
            self._log_line("INFO", f"输出目录：{job.output_dir}")

            self.result_output = processing.run(
                "native:tilesxyzdirectory", job.to_native_params(), feedback=self.feedback
            )
            if self.feedback.failed:
                self.exception = RuntimeError("瓦片生成命令执行失败（详见任务日志）")
                self.taskTerminated.emit()
                return
            self._log_line("INFO", "影像切片完成")
            self.taskCompleted.emit()
        except Exception as e:  # noqa: BLE001
            self.exception = e
            self._log_line("ERROR", f"影像切片异常：{e}")
            self.taskTerminated.emit()
