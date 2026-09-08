"""点绘制工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt

from app.infra.logging import get_logger
from .base import BaseDrawTool

logger = get_logger(__name__)


class PointDrawTool(BaseDrawTool):
    """点击画布直接创建点几何。"""

    def __init__(self, canvas: "QgsMapCanvas", draw_manager=None, *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.draw_manager = draw_manager
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self.rubber_band.setColor(Qt.GlobalColor.red)
        self.rubber_band.setWidth(2)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        """移动即预览：让磁吸指示器实时反馈吸到顶点还是边。"""
        self.snapped_point(event, wait_index=False)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        point = self.snapped_point(event)
        geometry = QgsGeometry.fromPointXY(QgsPointXY(point.x(), point.y()))
        self.geom = geometry
        self.rubber_band.addPoint(point)
        self.rubber_band.show()
        self.commit_geometry(geometry)

    def cleanup(self) -> None:
        super().cleanup()
        try:
            self.rubber_band.hide()
        except Exception:  # noqa: BLE001
            logger.exception("点绘制工具 cleanup 隐藏橡皮筋失败")
