"""矩形绘制工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

from .base import BaseDrawTool
from .snap import snapped_map_point


class RectangleDrawTool(BaseDrawTool):
    """拉框绘制矩形：左键按下，拖动鼠标，左键抬起结束。"""

    def __init__(self, canvas: "QgsMapCanvas", draw_manager=None, *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.draw_manager = draw_manager
        self.temp_points: List[QgsPointXY] = draw_manager.temp_points if draw_manager is not None else []
        self.is_over = False

        self.rubber_band = draw_manager.rubber_band if draw_manager is not None else None
        if self.rubber_band is None:
            self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self.rubber_band.setColor(Qt.GlobalColor.red)
            self.rubber_band.setWidth(2)
            self.rubber_band.setFillColor(QColor(255, 0, 0, 50))

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        try:
            if self.is_over:
                return
            if not self.temp_points:
                return
            point = snapped_map_point(self.canvas, event, wait_index=False)
            if len(self.temp_points) > 1:
                self.temp_points = [self.temp_points[0]]
            self.temp_points.append(QgsPointXY(point.x(), point.y()))
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            rectangle = QgsRectangle(self.temp_points[0], self.temp_points[1])
            self.geom = QgsGeometry.fromRect(rectangle)
            self._update_rubber_band()
        except Exception:  # noqa: BLE001
            pass

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if not self.is_over:
            if event.button() == Qt.MouseButton.LeftButton:
                self.is_over = False
                point = snapped_map_point(self.canvas, event)
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
            else:
                self.is_over = True
                if self.draw_manager is not None:
                    self.draw_manager.finish_draw(self.geom)
                else:
                    self.temp_points.clear()
                    self.finish_draw()

    def _update_rubber_band(self) -> None:
        if len(self.temp_points) < 2:
            return
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        p1, p2 = self.temp_points[0], self.temp_points[1]
        for p in (p1, QgsPointXY(p2.x(), p1.y()), p2, QgsPointXY(p1.x(), p2.y()), p1):
            self.rubber_band.addPoint(p)
        self.rubber_band.show()

    def cleanup(self) -> None:
        super().cleanup()
        try:
            self.rubber_band.hide()
        except Exception:  # noqa: BLE001
            pass
