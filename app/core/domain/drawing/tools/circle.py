"""圆形绘制工具。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.core import (
    QgsCircle,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from .base import BaseDrawTool


class CircleDrawTool(BaseDrawTool):
    """圆心 → 半径方式绘制圆。"""

    def __init__(self, canvas: "QgsMapCanvas", draw_manager=None, *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.draw_manager = draw_manager
        self.temp_points: List[QgsPointXY] = draw_manager.temp_points if draw_manager is not None else []
        self.radius = 0.0

        self.rubber_band = draw_manager.rubber_band if draw_manager is not None else None
        if self.rubber_band is None:
            self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self.rubber_band.setColor(Qt.GlobalColor.red)
            self.rubber_band.setWidth(2)
            self.rubber_band.setFillColor(QColor(255, 0, 0, 50))

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        try:
            if not self.temp_points:
                return
            point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
            center = self.temp_points[0]
            self.radius = math.hypot(point.x() - center.x(), point.y() - center.y())
            circle = QgsCircle.fromCenterDiameter(
                QgsPoint(center.x(), center.y()),
                self.radius * 2,
            )
            self.geom = QgsGeometry(circle.toPolygon(segments=72))
            self._update_rubber_band()
        except Exception:  # noqa: BLE001
            pass

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
        if event.button() == Qt.MouseButton.LeftButton:
            if not self.temp_points:
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
            else:
                self.temp_points[0] = QgsPointXY(point.x(), point.y())
        elif event.button() == Qt.MouseButton.RightButton:
            if self.draw_manager is not None:
                self.draw_manager.finish_draw(self.geom)
            else:
                self.temp_points.clear()
                self.finish_draw()

    def _update_rubber_band(self) -> None:
        if self.geom is not None and not self.geom.isEmpty():
            self.rubber_band.setToGeometry(self.geom, None)

    def cleanup(self) -> None:
        super().cleanup()
        try:
            self.rubber_band.hide()
        except Exception:  # noqa: BLE001
            pass
