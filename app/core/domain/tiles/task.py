"""矢量瓦片生成任务。

迁自 :class:`component.TreeTool.CreateTileTask`，与 :class:`TileJob` 配合。

任务经 :class:`QgsApplication.taskManager` 在后台线程运行，进度 / 日志经
Qt 信号投递回主线程。取消时把取消标志同步给算法反馈对象，算法会在当前
缩放级别之间退出。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsTask

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask

from app.core.domain.imagery_tiles.feedback import ProcessingLogFeedback
from app.infra.logging import get_logger

from .service import TileJob

logger = get_logger(__name__)


class CreateTileTask(QgsTask):
    """后台运行的矢量瓦片生成任务。

    进度通过 :attr:`progress_updated` 信号回报（``0~100`` int）；算法日志经
    :attr:`log_message` 信号回报（``level, message``），同时写入应用日志。
    """

    progress_updated = pyqtSignal(int)
    #: 算法 ``setProgressText`` 给出的文字（当前矢量瓦片算法只给百分比，
    #: 保留通用通道，供以后算法升级使用）
    progress_text = pyqtSignal(str)
    log_message = pyqtSignal(str, str)

    def __init__(self, description: str, job: TileJob) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self.job = job
        self.exception: object = None
        self.result_output: dict = {}
        self.feedback = ProcessingLogFeedback()
        self.feedback.progressChanged.connect(self._on_progress)
        self.feedback.progress_text.connect(self._on_progress_text)
        # feedback 在工作线程发信号；先转到本对象的普通 Python 方法，再投递 UI
        self.feedback.log_line.connect(self._on_log_line)

    # ------------------------------------------------------------ 回调

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

    def _on_progress_text(self, text: str) -> None:
        self.progress_text.emit(str(text))

    def _on_log_line(self, level: str, message: str) -> None:
        """工作线程算法日志 → 应用日志 + UI 信号。"""
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

    # ------------------------------------------------------------ 取消

    def cancel(self) -> None:  # noqa: N802 —— QgsTask 命名
        """请求取消：同时把取消标志传给算法反馈对象。

        QgsTask 自己的取消标志只有任务管理器看得到，``processing`` 算法检查的是
        :class:`QgsProcessingFeedback`；不转发的话点「取消」后要等当前任务跑完。
        """
        try:
            self.feedback.cancel()
        finally:
            super().cancel()

    # ------------------------------------------------------------ 执行

    def run(self) -> bool:
        try:
            # 延迟 import：避免 service 模块顶层依赖 processing
            import processing

            params = self.job.to_native_params()
            logger.info(
                "执行算法 native:writevectortiles_xyz：输出=%s 缩放=%s-%s 图层=%s",
                self.job.output_dir,
                self.job.min_zoom,
                self.job.max_zoom,
                list(self.job.layer_sources),
            )
            params["OUTPUT_DIRECTORY"] = self.job.output_dir
            self.result_output = processing.run(
                "native:writevectortiles_xyz", params, feedback=self.feedback
            )
            if self.feedback.failed:
                self.exception = RuntimeError("矢量瓦片生成命令执行失败（详见应用日志）")
                return False
            if self.isCanceled():
                logger.info("矢量瓦片任务已取消：%s", self.job.output_dir)
                return False
            logger.info("native:writevectortiles_xyz 完成 → %s", self.job.output_dir)
            return True
        except Exception as e:  # noqa: BLE001
            if self.isCanceled():
                logger.info("矢量瓦片任务已取消：%s", self.job.output_dir)
                return False
            self.exception = e
            logger.error("native:writevectortiles_xyz 执行失败：%s", e, exc_info=True)
            return False

    def finished(self, result: bool) -> None:  # noqa: D401
        """QgsTask 在主线程中回调；收尾由对话框订阅生命周期信号完成。"""
        return None
