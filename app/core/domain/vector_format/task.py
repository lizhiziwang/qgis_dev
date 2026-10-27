"""矢量格式转换后台任务。

GDAL 写出放在 :class:`QThread` 中执行，避免大图层阻塞 UI；进度通过
``QgsFeedback.progressChanged`` 转发到普通 Python 方法后再发 Qt 信号，
不直接连控件槽（与 ``GenericAlgRunnerTask`` 的踩坑处理一致）。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import QThread, pyqtSignal

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from .service import VectorConvertJob, VectorConvertService

logger = get_logger(__name__)


class VectorConvertTask(QThread):
    """在后台线程执行 :meth:`VectorConvertService.write`。"""

    #: 当前阶段：``preparing``（字段长度修正）/ ``exporting``（GDAL 写出）
    phase_changed = pyqtSignal(str)
    #: 写出进度 0–100（worker 线程发出 → 队列投递到主线程）
    progress_updated = pyqtSignal(int)
    #: 转换成功：输出路径、实际写入要素数、预期要素数（-1 表示未统计）
    converted = pyqtSignal(str, int, int)
    #: 转换失败，参数为错误信息
    failed = pyqtSignal(str)

    def __init__(
        self,
        service: VectorConvertService,
        job: VectorConvertJob,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._job = job
        self._feedback = None

    def run(self) -> None:
        from qgis.core import QgsFeedback

        feedback = QgsFeedback()
        feedback.progressChanged.connect(self._on_progress)
        self._feedback = feedback
        try:
            self.phase_changed.emit("preparing")
            prepared = self._service.prepare(self._job)
            expected = (
                prepared.layer.selectedFeatureCount() if prepared.only_selected else prepared.layer.featureCount()
            )
            self.phase_changed.emit("exporting")
            path = self._service.write(prepared, feedback)
            # 追加到已有库时文件里可能有多层：按写入的图层名统计
            layer_name = prepared.layer_name or os.path.splitext(os.path.basename(path))[0]
            written = self._service.count_output_features(path, prepared.fmt, layer_name)
        except Exception as exc:
            logger.error("矢量格式转换后台任务失败: %s", exc, exc_info=True)
            self.failed.emit(str(exc))
            return
        finally:
            self._feedback = None
        self.converted.emit(path, int(written), int(expected))

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))

    def cancel(self) -> None:
        """请求 GDAL 停止写出（QgsFeedback 被取消后 writer 会尽快退出）。"""
        if self._feedback is not None:
            self._feedback.cancel()
