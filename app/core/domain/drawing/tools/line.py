"""线绘制工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsRubberBand
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox

from .base import BaseDrawTool


class LineDrawTool(BaseDrawTool):
    """点击添加节点，右键结束绘制。"""

    def __init__(self, canvas: "QgsMapCanvas", draw_manager=None, *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.draw_manager = draw_manager
        self.temp_points: List[QgsPointXY] = draw_manager.temp_points if draw_manager is not None else []
        self.is_over = False

        self.line_rubber_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band.setColor(Qt.red)
        self.line_rubber_band.setWidth(2)
        self.line_rubber_band.setLineStyle(Qt.SolidLine)

        # 鼠标移动时的临时轨迹（左键到当前鼠标位置的虚线）
        self.line_rubber_band2 = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band2.setColor(Qt.red)
        self.line_rubber_band2.setWidth(2)
        self.line_rubber_band2.setLineStyle(Qt.DashLine)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self.is_over:
            return
        if not self.temp_points:
            return
        point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
        self.line_rubber_band2.reset(QgsWkbTypes.LineGeometry)
        self.line_rubber_band2.addPoint(self.temp_points[-1])
        self.line_rubber_band2.addPoint(point)
        self.line_rubber_band2.show()

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.button() == Qt.LeftButton:
                self.is_over = False
                point = self.canvas.getCoordinateTransform().toMapCoordinates(event.pos())
                self.temp_points.append(QgsPointXY(point.x(), point.y()))
                self._update_rubber_band()
            elif event.button() == Qt.RightButton:
                self.is_over = True
                self.line_rubber_band2.reset(QgsWkbTypes.LineGeometry)
                if len(self.temp_points) >= 2:
                    geometry = QgsGeometry.fromPolylineXY(self.temp_points)
                    self.geom = geometry
                    if self.draw_manager is not None:
                        self.draw_manager.finish_draw(geometry)
                    else:
                        self.temp_points.clear()
                        self.finish_draw()
                else:
                    QMessageBox.warning(self.canvas, "警告", "线要素至少需要 2 个节点！")
                    if self.draw_manager is not None:
                        self.draw_manager.clear_draw_tool()
        except Exception:  # noqa: BLE001
            pass

    def _update_rubber_band(self) -> None:
        if len(self.temp_points) < 2:
            return
        self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
        for point in self.temp_points:
            self.line_rubber_band.addPoint(point)
        self.line_rubber_band.show()

    def cleanup(self) -> None:
        super().cleanup()
        try:
            self.line_rubber_band.hide()
            self.line_rubber_band2.hide()
        except Exception:  # noqa: BLE001
            pass
