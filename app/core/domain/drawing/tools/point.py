"""点绘制工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsRubberBand
from PyQt6.QtCore import Qt

from .base import BaseDrawTool


class PointDrawTool(BaseDrawTool):
    """点击画布直接创建点几何。"""

    def __init__(self, canvas: "QgsMapCanvas", draw_manager=None, *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.draw_manager = draw_manager
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self.rubber_band.setColor(Qt.GlobalColor.red)
        self.rubber_band.setWidth(2)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())
        geometry = QgsGeometry.fromPointXY(QgsPointXY(point.x(), point.y()))
        self.geom = geometry
        self.rubber_band.addPoint(point)
        self.rubber_band.show()
        if self.draw_manager is not None:
            self.draw_manager.finish_draw(geometry)
        else:
            self.finish_draw()

    def cleanup(self) -> None:
        super().cleanup()
        try:
            self.rubber_band.hide()
        except Exception:  # noqa: BLE001
            pass
