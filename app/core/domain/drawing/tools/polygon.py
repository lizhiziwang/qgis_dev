"""面绘制工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgsMapCanvas

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.PyQt.QtGui import QColor

from app.infra.logging import get_logger
from .base import BaseDrawTool

logger = get_logger(__name__)


class PolygonDrawTool(BaseDrawTool):
    """点击添加节点，右键结束绘制。"""

    def __init__(self, canvas: "QgsMapCanvas", draw_manager=None, *, is_buffer: bool = False, distance: float = 0.0) -> None:
        super().__init__(canvas, is_buffer=is_buffer, distance=distance)
        self.draw_manager = draw_manager
        self.temp_points: List[QgsPointXY] = draw_manager.temp_points if draw_manager is not None else []
        self.is_over = False

        # 主 rubber_band：已画完的多边形
        self.rubber_band = draw_manager.rubber_band if draw_manager is not None else None
        if self.rubber_band is None:
            self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self.rubber_band.setColor(Qt.GlobalColor.red)
            self.rubber_band.setWidth(2)
            self.rubber_band.setFillColor(QColor(255, 0, 0, 50))

        # 鼠标移动的临时轨迹
        self.line_rubber_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.line_rubber_band.setColor(Qt.GlobalColor.red)
        self.line_rubber_band.setWidth(2)
        self.line_rubber_band.setLineStyle(Qt.PenStyle.DashLine)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        # 先取吸附点并更新磁吸指示器（含「上一笔已画完、正准备画下一笔」的状态：
        # is_over=True 时也要实时显示吸到顶点/边，不能提前 return）
        point = self.snapped_point(event, wait_index=False)
        if self.is_over:
            return
        if not self.temp_points:
            return
        # 预览：闭合边（首点 → 光标）+ 光标到上一顶点的段（追踪开启时该段
        # 实时展示沿已有几何的追踪路径，与提交结果一致）
        path = self.traced_preview_path(point)
        self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.line_rubber_band.addPoint(self.temp_points[0])
        self.line_rubber_band.addPoint(path[-1])
        for preview_pt in reversed(path[:-1]):
            self.line_rubber_band.addPoint(preview_pt)
        self.line_rubber_band.show()

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_over = False
            point = self.snapped_point(event)
            # 追踪：前后两顶点都吸附到已有要素时自动采进沿边中间点
            for traced in self.traced_vertex(point):
                self.temp_points.append(traced)
            self.temp_points.append(QgsPointXY(point.x(), point.y()))
            self._update_rubber_band()
        elif event.button() == Qt.MouseButton.RightButton:
            self.is_over = True
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
            if len(self.temp_points) >= 3:
                # 闭合：首尾重合
                if self.temp_points[0] != self.temp_points[-1]:
                    self.temp_points.append(self.temp_points[0])
                geometry = QgsGeometry.fromPolygonXY([self.temp_points])
                self.geom = geometry
                self.commit_geometry(geometry)
            else:
                QMessageBox.warning(self.canvas, "警告", "面要素至少需要 3 个节点！")
                if self.draw_manager is not None:
                    self.draw_manager.clear_draw_tool()

    def clear_helper_line(self) -> None:
        """辅助虚线 = ``line_rubber_band``（实线是 ``rubber_band``）。"""
        try:
            self.line_rubber_band.reset(QgsWkbTypes.LineGeometry)
        except RuntimeError:
            pass

    def _update_rubber_band(self) -> None:
        if not self.temp_points:
            return
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        for point in self.temp_points:
            self.rubber_band.addPoint(point)
        self.rubber_band.show()

    def cleanup(self) -> None:
        super().cleanup()
        try:
            self.rubber_band.hide()
            self.line_rubber_band.hide()
        except Exception:  # noqa: BLE001
            logger.exception("面绘制工具 cleanup 隐藏橡皮筋失败")
