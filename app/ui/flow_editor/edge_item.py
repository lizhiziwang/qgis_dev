"""流程编排：连线图形项。

三次贝塞尔曲线连接两个端口圆点，终点带箭头；支持选中 / 依赖链高亮。
位置不缓存——每次 :meth:`update_path` 从端口圆点的 scenePos 实时计算，
节点被拖动时自动跟随。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QGraphicsPathItem, QMenu

from .flow_theme import current as current_palette


def _arrow_polygon(tip: QPointF, angle_deg: float, size: float = 9.0) -> QPolygonF:
    """沿路径终点切线方向画一个指向终点的箭头。"""
    rad = math.radians(angle_deg)
    dx, dy = math.cos(rad), math.sin(rad)  # 路径行进方向（指向终点）
    bx, by = tip.x() - dx * size, tip.y() - dy * size  # 箭头基部中心
    px, py = -dy, dx  # 垂直方向
    half = size * 0.45
    return QPolygonF(
        [
            tip,
            QPointF(bx + px * half, by + py * half),
            QPointF(bx - px * half, by - py * half),
        ]
    )


class EdgeItem(QGraphicsPathItem):
    """一条连线。``src_port`` / ``dst_port`` 是 :class:`PortItem`。"""

    def __init__(self, src_port, dst_port, parent=None) -> None:
        super().__init__(parent)
        self.src_port = src_port
        self.dst_port = dst_port
        #: 对应的图模型边 id（右键删除 / 框选删除用）
        self.edge_id = ""
        self._highlighted = False
        self.setFlag(QGraphicsPathItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(-1)
        self.apply_theme()
        self.update_path()

    def update_path(self) -> None:
        """按两端端口圆心重新布路。"""
        p1 = self.src_port.scenePos()
        p2 = self.dst_port.scenePos()
        dx = max(40.0, abs(p2.x() - p1.x()) * 0.5)
        path = QPainterPath(p1)
        path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
        self.prepareGeometryChange()
        self.setPath(path)

    def set_drag_pos(self, pos: QPointF) -> None:
        """临时连线：起点固定在本端口，终点跟随鼠标。"""
        p1 = self.src_port.scenePos()
        p2 = pos
        dx = max(40.0, abs(p2.x() - p1.x()) * 0.5)
        path = QPainterPath(p1)
        path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
        self.prepareGeometryChange()
        self.setPath(path)

    def apply_theme(self) -> None:
        """按当前主题调色板刷新笔色（高亮橙 / 常态连线色）。"""
        pal = current_palette()
        color = pal.edge_highlight if self._highlighted else pal.edge_normal
        self.setPen(QPen(color, 3 if self._highlighted else 2))
        self.update()

    def set_highlighted(self, flag: bool) -> None:
        self._highlighted = flag
        self.apply_theme()

    def boundingRect(self) -> QRectF:  # noqa: N802 —— Qt 命名
        return super().boundingRect().adjusted(-3, -3, 3, 3)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self.path()
        painter.setPen(self.pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if not path.isEmpty():
            tip = path.pointAtPercent(1.0)
            angle = path.angleAtPercent(1.0)
            color = self.pen().color()
            painter.setBrush(color)
            painter.setPen(QPen(color, 1))
            painter.drawPolygon(_arrow_polygon(tip, angle))

    def contextMenuEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        menu = QMenu()
        act = menu.addAction("删除连线")
        act.setData("delete_edge")
        chosen = menu.exec(event.screenPos())
        if chosen is not None:
            scene = self.scene()
            handler = getattr(scene, "on_edge_context_action", None)
            if handler is not None:
                handler(self.edge_id)
        event.accept()
