"""GCP 刺点用的影像视图：滚轮缩放、中键平移、左键取像素坐标。

只加载当前一张全分辨率影像（切图时旧 pixmap 随 items 释放），
标记用 ``ItemIgnoresTransformations``，缩放时保持固定屏幕尺寸。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QImageReader, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)

MIN_SCALE = 0.02
MAX_SCALE = 32.0


class _MarkItem(QGraphicsItem):
    """十字丝 + 点号标注。"""

    def __init__(self, name: str, x: float, y: float) -> None:
        super().__init__()
        self._name = name
        self.setPos(x, y)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(10)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self) -> QRectF:  # noqa: N802 —— Qt 命名
        return QRectF(-12.0, -22.0, 96.0, 44.0)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor("#ff3b30"), 1.6)
        pen.setCosmetic(True)
        painter.setPen(pen)
        radius = 8.0
        painter.drawLine(QPointF(-radius, 0.0), QPointF(radius, 0.0))
        painter.drawLine(QPointF(0.0, -radius), QPointF(0.0, radius))
        painter.drawEllipse(QPointF(0.0, 0.0), 2.5, 2.5)

        font = painter.font()
        font.setBold(True)
        font.setPointSize(8)
        painter.setFont(font)
        text_rect = QRectF(8.0, -16.0, 86.0, 14.0)
        painter.setPen(QColor(0, 0, 0, 200))
        painter.drawText(text_rect.translated(1.0, 1.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._name)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._name)


class GcpMarkView(QGraphicsView):
    """可缩放的影像视图；左键点击发出原始像素坐标。"""

    image_clicked = pyqtSignal(QPointF)
    zoom_changed = pyqtSignal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setBackgroundBrush(QColor("#202124"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setZValue(0)
        self._pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._pixmap_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(self._pixmap_item)

        self._image_rect = QRectF()
        self._mark_items: list[_MarkItem] = []
        self._panning = False
        self._pan_last = QPointF()

    # ------------------------------------------------------------ 影像

    @property
    def has_image(self) -> bool:
        return not self._pixmap_item.pixmap().isNull()

    @property
    def image_size(self) -> tuple[int, int]:
        pixmap = self._pixmap_item.pixmap()
        return pixmap.width(), pixmap.height()

    def load_image(self, path: str) -> bool:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            self.clear()
            return False
        pixmap = QPixmap.fromImage(image)
        self._pixmap_item.setPixmap(pixmap)
        self._image_rect = QRectF(0.0, 0.0, float(pixmap.width()), float(pixmap.height()))
        self._scene.setSceneRect(self._image_rect)
        self.setSceneRect(self._image_rect)
        self.fit_to_view()
        return True

    def clear(self) -> None:
        self.set_marks([])
        self._pixmap_item.setPixmap(QPixmap())
        self._image_rect = QRectF()
        self._scene.setSceneRect(QRectF())
        self.setSceneRect(QRectF())
        self.resetTransform()

    # ------------------------------------------------------------ 标记

    def set_marks(self, marks: list[tuple[str, float, float]]) -> None:
        for item in self._mark_items:
            self._scene.removeItem(item)
        self._mark_items.clear()
        for name, x, y in marks:
            item = _MarkItem(name, x, y)
            self._scene.addItem(item)
            self._mark_items.append(item)

    # ------------------------------------------------------------ 缩放 / 平移

    @property
    def scale_factor(self) -> float:
        return max(self.transform().m11(), 1e-9)

    def zoom_in(self) -> None:
        self._zoom_by(1.25)

    def zoom_out(self) -> None:
        self._zoom_by(1 / 1.25)

    def _zoom_by(self, factor: float) -> None:
        if not self.has_image:
            return
        current = self.scale_factor
        target = current * factor
        if target < MIN_SCALE:
            factor = MIN_SCALE / current
        elif target > MAX_SCALE:
            factor = MAX_SCALE / current
        if abs(factor - 1.0) < 1e-6:
            return
        self.scale(factor, factor)
        self.zoom_changed.emit(self.scale_factor)

    def fit_to_view(self) -> None:
        if self._image_rect.isEmpty():
            return
        self.resetTransform()
        self.fitInView(self._image_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(self.scale_factor)

    def wheelEvent(self, event) -> None:
        if not self.has_image:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._zoom_by(1.2 if delta > 0 else 1 / 1.2)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if not self.has_image:
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            point = self.mapToScene(event.position().toPoint())
            if self._image_rect.contains(point):
                self.image_clicked.emit(point)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._panning and event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


__all__ = ["MAX_SCALE", "MIN_SCALE", "GcpMarkView"]
