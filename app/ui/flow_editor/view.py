"""流程编排：画布视图。

滚轮缩放（clamp 到 [0.1, 5]）、按压鼠标滚轮（中键）拖拽平移画布、
空白拖拽框选、接收节点库拖来的模板（mime 见 :data:`TEMPLATE_MIME`）
并转成场景坐标交给场景建节点。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPoint, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QPainter
from qgis.PyQt.QtWidgets import QGraphicsView

from app.ui.common.qt_compat import event_pos
from .flow_theme import current as current_palette
from .scene import FlowScene

#: 节点库拖拽使用的 mime 类型（载荷是模板 key / 算法 id 的 UTF-8 文本）
TEMPLATE_MIME = "application/x-qgis-flow-template"

ZOOM_MIN = 0.1
ZOOM_MAX = 5.0
ZOOM_STEP = 1.15


class FlowView(QGraphicsView):
    """流程画布视图。"""

    template_dropped = pyqtSignal(str, float, float)  # template key, scene_x, scene_y

    def __init__(self, scene: FlowScene, parent=None) -> None:
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAcceptDrops(True)
        self.setBackgroundBrush(QBrush(current_palette().canvas_bg))
        # 按压鼠标滚轮（中键）拖拽平移画布
        self._panning = False
        self._pan_start = QPoint()
        self._pan_cursor = None

    # ------------------------------------------------------------ 视图操作

    def wheelEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
        new_scale = self.transform().m11() * factor
        if not (ZOOM_MIN <= new_scale <= ZOOM_MAX):
            return
        self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(event_pos(event))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if self._panning:
            self._pan_to(event_pos(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._stop_pan()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _start_pan(self, pos: QPoint) -> None:
        """开始中键平移：记录起点并换成抓手光标。"""
        self._panning = True
        self._pan_start = QPoint(pos)
        self._pan_cursor = self.cursor()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _pan_to(self, pos: QPoint) -> None:
        """按鼠标位移滚动视图（内容跟随鼠标移动）。"""
        delta = pos - self._pan_start
        self._pan_start = QPoint(pos)
        h_bar = self.horizontalScrollBar()
        v_bar = self.verticalScrollBar()
        h_bar.setValue(h_bar.value() - delta.x())
        v_bar.setValue(v_bar.value() - delta.y())

    def _stop_pan(self) -> None:
        """结束中键平移，恢复原光标。"""
        self._panning = False
        self.setCursor(self._pan_cursor or Qt.CursorShape.ArrowCursor)

    def zoom_in(self) -> None:
        new_scale = self.transform().m11() * ZOOM_STEP
        if new_scale <= ZOOM_MAX:
            self.scale(ZOOM_STEP, ZOOM_STEP)

    def zoom_out(self) -> None:
        new_scale = self.transform().m11() / ZOOM_STEP
        if new_scale >= ZOOM_MIN:
            self.scale(1.0 / ZOOM_STEP, 1.0 / ZOOM_STEP)

    def fit_to_view(self) -> None:
        rect = self.scene().sceneRect() if self.scene() is not None else None
        if rect is not None and not rect.isNull() and not rect.isEmpty():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def center_on_node(self, node_id: str) -> None:
        scene = self.scene()
        if not isinstance(scene, FlowScene):
            return
        item = scene.node_item(node_id)
        if item is not None:
            self.centerOn(item)

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        scene = self.scene()
        if isinstance(scene, FlowScene) and event.key() in (
            Qt.Key.Key_Delete,
            Qt.Key.Key_Backspace,
        ):
            scene.delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 模板拖放

    def dragEnterEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.mimeData().hasFormat(TEMPLATE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.mimeData().hasFormat(TEMPLATE_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        if event.mimeData().hasFormat(TEMPLATE_MIME):
            key = bytes(event.mimeData().data(TEMPLATE_MIME)).decode("utf-8")
            scene_pos = self.mapToScene(event_pos(event))
            self.template_dropped.emit(key, scene_pos.x(), scene_pos.y())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
