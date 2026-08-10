"""矢量瓦片生成任务。

迁自 :class:`component.TreeTool.CreateTileTask`，与 :class:`TileJob` 配合。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsTask

from PyQt6.QtCore import pyqtSignal
from qgis.core import QgsProcessingFeedback, QgsTask

from .service import TileJob


class CreateTileTask(QgsTask):
    """后台运行的矢量瓦片生成任务。

    进度通过 :attr:`progress_updated` 信号回报（``0~100`` int）。
    """

    progress_updated = pyqtSignal(int)

    def __init__(self, description: str, job: TileJob) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self.job = job
        self.exception: object = None
        self.result_output: dict = {}
        self.feedback = QgsProcessingFeedback()
        self.feedback.progressChanged.connect(self._on_progress)

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

    def run(self) -> bool:
        try:
            # 延迟 import：避免 service 模块顶层依赖 processing
            import processing

            params = self.job.to_native_params()
            self.result_output = processing.run(
                "native:writevectortiles_xyz", params, feedback=self.feedback
            )
            return True
        except Exception as e:  # noqa: BLE001
            self.exception = e
            return False

    def finished(self, result: bool) -> None:  # noqa: D401
        """QgsTask 在主线程中回调。这里只占位，UI 进度条订阅 progress_updated。"""
        return None
