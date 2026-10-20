"""标注画布：缩放/平移、掩膜叠加、多边形/画笔/橡皮擦、SAM 提示。"""

from __future__ import annotations

import contextlib

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
)

from app.infra.logging import get_logger
from app.ui.deep_learning.annotation_model import AnnotationDocument

logger = get_logger(__name__)

MIN_SCALE = 0.02
MAX_SCALE = 32.0
_FIT_MAX_SCALE = 8.0

MODE_PAN = "pan"
MODE_POLYGON = "polygon"
MODE_BRUSH = "brush"
MODE_ERASER = "eraser"
MODE_SAM_POINT = "sam_point"
MODE_SAM_BOX = "sam_box"
MODE_SAM_SCRIBBLE = "sam_scribble"

_ALL_MODES = (
    MODE_PAN,
    MODE_POLYGON,
    MODE_BRUSH,
    MODE_ERASER,
    MODE_SAM_POINT,
    MODE_SAM_BOX,
    MODE_SAM_SCRIBBLE,
)

SCRIBBLE_MAX_POINTS = 64  # 单笔涂鸦最多转成多少个 SAM 正/负提示点


def sample_scribble_points(
    points: list[QPointF], *, max_points: int = SCRIBBLE_MAX_POINTS
) -> list[QPointF]:
    """把一笔涂鸦的轨迹均匀抽稀成至多 ``max_points`` 个提示点。

    末尾点始终保留（保证负涂鸦的收笔位置也参与提示）。
    """
    if not points or max_points <= 0:
        return []
    unique = [points[0]]
    for point in points[1:]:
        if point != unique[-1]:
            unique.append(point)
    if len(unique) <= max_points:
        return unique
    stride = max(1, (len(unique) - 1 + max_points - 2) // (max_points - 1))
    sampled = unique[::stride]
    if sampled[-1] != unique[-1]:
        sampled[-1] = unique[-1]
    return sampled


class _MaskItem(QGraphicsItem):
    """直接绘制文档的 Indexed8 显示图（索引 0 透明），避免每帧转 ARGB。"""

    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        self.setZValue(10)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_image(self, image: QImage | None) -> None:
        self.prepareGeometryChange()
        self._image = image
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 —— Qt 接口
        if self._image is None:
            return QRectF()
        return QRectF(0, 0, self._image.width(), self._image.height())

    def paint(self, painter: QPainter, option, widget=None) -> None:
        if self._image is not None:
            painter.drawImage(QRectF(0, 0, self._image.width(), self._image.height()), self._image)


class _PreviewItem(QGraphicsItem):
    """SAM 候选掩膜的彩色预览（半透明）。"""

    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        self.setZValue(20)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_image(self, image: QImage | None) -> None:
        self.prepareGeometryChange()
        self._image = image
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        if self._image is None:
            return QRectF()
        return QRectF(0, 0, self._image.width(), self._image.height())

    def paint(self, painter: QPainter, option, widget=None) -> None:
        if self._image is not None:
            painter.drawImage(QRectF(0, 0, self._image.width(), self._image.height()), self._image)


class _PromptMarker(QGraphicsItem):
    """固定屏幕大小的正/负提示点（绿 + / 红 −）。"""

    def __init__(self, positive: bool, radius: float = 8.0) -> None:
        super().__init__()
        self._positive = positive
        self._radius = max(4.0, float(radius))
        self.setZValue(100)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_radius(self, radius: float) -> None:
        self.prepareGeometryChange()
        self._radius = max(4.0, float(radius))
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        radius = self._radius + 2
        return QRectF(-radius, -radius, 2 * radius, 2 * radius)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = self._radius
        color = QColor("#2ecc71") if self._positive else QColor("#e74c3c")
        painter.setPen(QPen(QColor(0, 0, 0, 160), 2.0))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(-radius + 1, -radius + 1, 2 * radius - 2, 2 * radius - 2))
        pen = QPen(QColor("white"), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        arm = radius * 0.5
        painter.drawLine(QPointF(-arm, 0), QPointF(arm, 0))
        if self._positive:
            painter.drawLine(QPointF(0, -arm), QPointF(0, arm))


class _PolygonPreview(QGraphicsItem):
    """正在绘制的多边形轮廓。"""

    def __init__(self) -> None:
        super().__init__()
        self.points: list[QPointF] = []
        self.color = QColor("#e6194b")
        self.view_scale = 1.0
        self.setZValue(90)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_points(self, points: list[QPointF], color: QColor | None = None) -> None:
        self.prepareGeometryChange()
        self.points = list(points)
        if color is not None:
            self.color = QColor(color)
        self.update()

    def set_view_scale(self, scale: float) -> None:
        self.prepareGeometryChange()
        self.view_scale = max(float(scale), 1e-9)
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        if not self.points:
            return QRectF()
        xs = [point.x() for point in self.points]
        ys = [point.y() for point in self.points]
        radius = max(2.0, 4.0 / max(self.view_scale, 1e-6))
        return QRectF(
            min(xs) - radius,
            min(ys) - radius,
            max(xs) - min(xs) + 2 * radius,
            max(ys) - min(ys) + 2 * radius,
        )

    def _path(self, *, closed: bool) -> QPainterPath:
        path = QPainterPath()
        if not self.points:
            return path
        path.moveTo(self.points[0])
        for point in self.points[1:]:
            path.lineTo(point)
        if closed:
            path.closeSubpath()
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        if not self.points:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = self._path(closed=len(self.points) >= 3)
        fill = QColor(self.color)
        fill.setAlpha(45)
        painter.setBrush(fill if len(self.points) >= 3 else QBrush(Qt.BrushStyle.NoBrush))
        pen = QPen(self.color, 1.4)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawPath(path)
        radius = max(1.5, 3.0 / max(self.view_scale, 1e-6))
        painter.setBrush(self.color)
        painter.setPen(QPen(QColor("white"), 0))
        for point in self.points:
            painter.drawEllipse(point, radius, radius)


class AnnotationView(QGraphicsView):
    """EISeg 风格画布。"""

    cursor_moved = pyqtSignal(QPointF)
    polygon_finished = pyqtSignal(list)
    sam_prompt_changed = pyqtSignal(list)
    sam_box_changed = pyqtSignal(object)
    sam_apply_requested = pyqtSignal()
    message = pyqtSignal(str)
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
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setBackgroundBrush(QColor("#202124"))
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._image_item = QGraphicsPixmapItem()
        self._image_item.setZValue(0)
        self._image_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._image_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(self._image_item)

        self._mask_item = _MaskItem()
        self._scene.addItem(self._mask_item)
        self._preview_item = _PreviewItem()
        self._scene.addItem(self._preview_item)

        self._polygon_item = _PolygonPreview()
        self._scene.addItem(self._polygon_item)
        self._box_item = QGraphicsRectItem()
        self._box_item.setZValue(95)
        self._box_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        pen = QPen(QColor("#2ecc71"), 1.4)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._box_item.setPen(pen)
        self._box_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._box_item.hide()
        self._scene.addItem(self._box_item)

        self._scribble_positive_item = QGraphicsPathItem()
        self._scribble_negative_item = QGraphicsPathItem()
        for item, color in (
            (self._scribble_positive_item, "#2ecc71"),
            (self._scribble_negative_item, "#e74c3c"),
        ):
            pen = QPen(QColor(color), 2.4)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            item.setPen(pen)
            item.setZValue(96)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            item.hide()
            self._scene.addItem(item)
        self._scribble_preview_item = QGraphicsPathItem()
        preview_pen = QPen(QColor("#2ecc71"), 2.4)
        preview_pen.setCosmetic(True)
        preview_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        preview_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self._scribble_preview_item.setPen(preview_pen)
        self._scribble_preview_item.setZValue(97)
        self._scribble_preview_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scribble_preview_item.hide()
        self._scene.addItem(self._scribble_preview_item)

        self._cursor_item = _CursorRingItem()
        self._scene.addItem(self._cursor_item)
        self._cursor_item.hide()

        self._document: AnnotationDocument | None = None
        self._mode = MODE_PAN
        self._current_label = 0
        self._label_color = QColor("#e6194b")
        self._brush_size = 20
        self._prompt_radius = 8
        self._polygon_points: list[QPointF] = []
        self._sam_points: list[tuple[float, float, int]] = []
        self._markers: list[_PromptMarker] = []
        self._box_start: QPointF | None = None
        self._box_current: QPointF | None = None
        self._box: QRectF | None = None
        self._sam_scribbles: list[tuple[list[QPointF], bool]] = []
        self._scribbling = False
        self._scribble_positive = True
        self._scribble_points: list[QPointF] = []
        self._painting = False
        self._painting_label = 0
        self._last_point: QPointF | None = None
        self._panning = False
        self._pan_last = None
        self._image_size = (0, 0)

    # ------------------------------------------------------------------ 文档
    @property
    def document(self) -> AnnotationDocument | None:
        return self._document

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def scale_factor(self) -> float:
        return max(self.transform().m11(), 1e-9)

    def set_document(self, document: AnnotationDocument | None, image: QImage | None = None) -> None:
        if self._document is not None:
            with contextlib.suppress(TypeError, RuntimeError):
                self._document.changed.disconnect(self._on_document_changed)
        self._document = document
        self.clear_prompts()
        self.clear_preview()
        self._mask_item.set_image(None)
        self._image_item.setPixmap(QPixmap())
        self._image_size = (0, 0)
        if document is None:
            self._scene.setSceneRect(QRectF())
            return
        document.changed.connect(self._on_document_changed)
        self._mask_item.set_image(document.display)
        if image is not None:
            display = image if image.format() == QImage.Format.Format_RGB888 else image.convertToFormat(
                QImage.Format.Format_RGB888
            )
            self._image_item.setPixmap(QPixmap.fromImage(display))
            self._image_size = (display.width(), display.height())
        rect = QRectF(0, 0, document.mask.width(), document.mask.height())
        self._scene.setSceneRect(rect)
        self.setSceneRect(rect)
        self.fit_to_view()

    def set_image(self, image: QImage) -> None:
        """同尺寸地替换底图（波段切换时使用）。"""
        display = image if image.format() == QImage.Format.Format_RGB888 else image.convertToFormat(
            QImage.Format.Format_RGB888
        )
        self._image_item.setPixmap(QPixmap.fromImage(display))
        self._image_size = (display.width(), display.height())

    def _on_document_changed(self, rect) -> None:
        self._mask_item.update(QRectF(rect))

    # ------------------------------------------------------------------ 模式
    def set_mode(self, mode: str) -> None:
        if mode not in _ALL_MODES:
            mode = MODE_PAN
        if mode != self._mode:
            self.clear_prompts()
            self.clear_preview()
        self._mode = mode
        self._update_cursor()
        if mode in (MODE_SAM_POINT, MODE_SAM_BOX, MODE_SAM_SCRIBBLE, MODE_POLYGON):
            self.setFocus()

    def set_current_label(self, class_id: int, color: QColor | str | None = None) -> None:
        self._current_label = int(class_id)
        if color is not None:
            self._label_color = QColor(color)
        if self._mode == MODE_POLYGON:
            self._polygon_item.set_points(self._polygon_points, self._label_color)
        if self._mode == MODE_BRUSH:
            self._cursor_item.set_color(self._label_color)

    def set_brush_size(self, size: int) -> None:
        self._brush_size = max(1, int(size))
        self._cursor_item.set_radius(self._brush_size)
        self._update_cursor()

    def set_prompt_radius(self, radius: int) -> None:
        """SAM 提示点的可视化半径（屏幕像素）。"""
        self._prompt_radius = max(4, int(radius))
        for marker in self._markers:
            marker.set_radius(self._prompt_radius)

    def set_background_color(self, color) -> None:
        self.setBackgroundBrush(QColor(color))

    def set_mask_visible(self, visible: bool) -> None:
        self._mask_item.setVisible(visible)

    def mask_visible(self) -> bool:
        return self._mask_item.isVisible()

    def set_mask_opacity(self, opacity: float) -> None:
        self._mask_item.setOpacity(max(0.0, min(1.0, float(opacity))))

    # ------------------------------------------------------------------ 预览
    def set_preview_mask(self, mask_image: QImage | None, color: QColor | str | None = None) -> None:
        if mask_image is None or mask_image.isNull():
            self._preview_item.set_image(None)
            return
        if self._document is not None:
            width, height = self._document.mask.width(), self._document.mask.height()
            if mask_image.width() != width or mask_image.height() != height:
                mask_image = mask_image.scaled(
                    width,
                    height,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
        color = QColor(color or self._label_color)
        self._preview_item.set_image(_colorize_mask(mask_image, color, alpha=140))

    def clear_preview(self) -> None:
        self._preview_item.set_image(None)

    # ------------------------------------------------------------------ 提示
    def clear_prompts(self) -> None:
        self._polygon_points = []
        self._polygon_item.set_points([])
        self._clear_sam_points()
        self._clear_scribbles()
        self._box = None
        self._box_start = None
        self._box_current = None
        self._box_item.hide()
        self._polygon_item.set_view_scale(self.scale_factor)
        if self._mode == MODE_SAM_BOX:
            self.sam_box_changed.emit(None)

    def _clear_sam_points(self) -> None:
        for marker in self._markers:
            self._scene.removeItem(marker)
        self._markers = []
        self._sam_points = []

    def _clear_scribbles(self) -> None:
        self._scribbles_abort()
        self._sam_scribbles = []
        self._rebuild_scribble_paths()

    def _scribbles_abort(self) -> None:
        self._scribbling = False
        self._scribble_points = []
        self._scribble_preview_item.setPath(QPainterPath())
        self._scribble_preview_item.hide()

    def _sam_prompt_list(self) -> list[tuple[float, float, int]]:
        prompts = list(self._sam_points)
        for points, positive in self._sam_scribbles:
            label = 1 if positive else 0
            prompts.extend((point.x(), point.y(), label) for point in sample_scribble_points(points))
        return prompts

    def _emit_sam_prompts(self) -> None:
        self.sam_prompt_changed.emit(self._sam_prompt_list())

    def _add_sam_point(self, point: QPointF, positive: bool) -> None:
        label = 1 if positive else 0
        self._sam_points.append((point.x(), point.y(), label))
        marker = _PromptMarker(positive, self._prompt_radius)
        marker.setPos(point)
        self._scene.addItem(marker)
        self._markers.append(marker)
        self._emit_sam_prompts()

    def _remove_last_sam_point(self) -> None:
        if not self._sam_points:
            return
        self._sam_points.pop()
        marker = self._markers.pop()
        self._scene.removeItem(marker)
        self._emit_sam_prompts()

    # ------------------------------------------------------------------ 涂鸦提示
    def _start_scribble(self, point: QPointF, positive: bool) -> None:
        self._scribbles_abort()
        self._scribbling = True
        self._scribble_positive = positive
        self._scribble_points = [point]
        pen = self._scribble_preview_item.pen()
        pen.setColor(QColor("#2ecc71" if positive else "#e74c3c"))
        self._scribble_preview_item.setPen(pen)
        path = QPainterPath(point)
        self._scribble_preview_item.setPath(path)
        self._scribble_preview_item.show()

    def _extend_scribble(self, point: QPointF) -> None:
        if not self._scribbling or not self._scribble_points:
            return
        if _screen_distance(self, self._scribble_points[-1], point) < 3:
            return
        self._scribble_points.append(point)
        path = self._scribble_preview_item.path()
        path.lineTo(point)
        self._scribble_preview_item.setPath(path)

    def _finish_scribble(self) -> None:
        if not self._scribbling:
            return
        points = list(self._scribble_points)
        positive = self._scribble_positive
        self._scribbles_abort()
        if not points:
            return
        self._sam_scribbles.append((points, positive))
        self._rebuild_scribble_paths()
        self._emit_sam_prompts()

    def _remove_last_scribble(self) -> None:
        if not self._sam_scribbles:
            return
        self._sam_scribbles.pop()
        self._rebuild_scribble_paths()
        self._emit_sam_prompts()

    def _rebuild_scribble_paths(self) -> None:
        paths = {True: QPainterPath(), False: QPainterPath()}
        for points, positive in self._sam_scribbles:
            if not points:
                continue
            path = paths[positive]
            path.moveTo(points[0])
            for point in points[1:]:
                path.lineTo(point)
        self._scribble_positive_item.setPath(paths[True])
        self._scribble_positive_item.setVisible(not paths[True].isEmpty())
        self._scribble_negative_item.setPath(paths[False])
        self._scribble_negative_item.setVisible(not paths[False].isEmpty())

    def sam_box(self) -> QRectF | None:
        return QRectF(self._box) if self._box is not None else None

    # ------------------------------------------------------------------ 坐标/缩放
    def _scene_pos(self, event) -> QPointF:
        return self.mapToScene(event.position().toPoint())

    def zoom_in(self) -> None:
        self._zoom_by(1.25)

    def zoom_out(self) -> None:
        self._zoom_by(1 / 1.25)

    def _zoom_by(self, factor: float) -> None:
        current = self.scale_factor
        target = current * factor
        if target < MIN_SCALE:
            factor = MIN_SCALE / current
        elif target > MAX_SCALE:
            factor = MAX_SCALE / current
        if abs(factor - 1.0) < 1e-6:
            return
        self.scale(factor, factor)
        self._after_zoom()

    def fit_to_view(self) -> None:
        if self._document is None:
            return
        rect = QRectF(0, 0, self._document.mask.width(), self._document.mask.height())
        if rect.isEmpty():
            return
        self.resetTransform()
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        scale = self.scale_factor
        if scale > _FIT_MAX_SCALE:
            self.resetTransform()
            self.scale(_FIT_MAX_SCALE, _FIT_MAX_SCALE)
            self.centerOn(rect.center())
        elif scale < MIN_SCALE:
            self.resetTransform()
            self.scale(MIN_SCALE, MIN_SCALE)
            self.centerOn(rect.center())
        self._after_zoom()

    def _after_zoom(self) -> None:
        self._polygon_item.set_view_scale(self.scale_factor)
        self._cursor_item.set_view_scale(self.scale_factor)
        self.zoom_changed.emit(self.scale_factor)

    def wheelEvent(self, event) -> None:
        if self._document is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._zoom_by(1.2 if delta > 0 else 1 / 1.2)
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_cursor()

    # ------------------------------------------------------------------ 鼠标
    def mousePressEvent(self, event) -> None:
        if self._document is None:
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        point = self._scene_pos(event)
        if self._mode == MODE_BRUSH and event.button() == Qt.MouseButton.LeftButton:
            self._start_stroke(point, erase=False)
            return
        if self._mode == MODE_ERASER and event.button() == Qt.MouseButton.LeftButton:
            self._start_stroke(point, erase=True)
            return
        if self._mode == MODE_POLYGON and event.button() == Qt.MouseButton.LeftButton:
            self._add_polygon_point(point)
            return
        if self._mode == MODE_SAM_POINT:
            if event.button() == Qt.MouseButton.LeftButton:
                positive = not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._add_sam_point(point, positive)
                return
            if event.button() == Qt.MouseButton.RightButton:
                self._add_sam_point(point, False)
                return
        if self._mode == MODE_SAM_BOX and event.button() == Qt.MouseButton.LeftButton:
            self._box_start = point
            self._box_current = point
            self._box = None
            self._box_item.setRect(QRectF(point, point))
            self._box_item.show()
            return
        if self._mode == MODE_SAM_SCRIBBLE:
            if event.button() == Qt.MouseButton.LeftButton:
                positive = not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._start_scribble(point, positive)
                return
            if event.button() == Qt.MouseButton.RightButton:
                self._start_scribble(point, False)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - delta.y()))
            event.accept()
            return
        point = self._scene_pos(event)
        self.cursor_moved.emit(point)
        if 0 <= point.x() < self._image_size[0] and 0 <= point.y() < self._image_size[1]:
            self._cursor_item.setPos(point)
            if self._mode in (MODE_BRUSH, MODE_ERASER):
                self._cursor_item.show()
        else:
            self._cursor_item.hide()

        if self._painting and self._last_point is not None:
            self._document.stroke_segment(
                self._last_point,
                point,
                self._brush_size / 2.0,
                class_id=self._painting_label,
                erase=self._mode == MODE_ERASER,
            )
            self._last_point = point
            return
        if self._scribbling:
            self._extend_scribble(point)
            return
        if self._mode == MODE_SAM_BOX and self._box_start is not None:
            self._box_current = point
            self._box_item.setRect(QRectF(self._box_start, point).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self._pan_last = None
            self._update_cursor()
            event.accept()
            return
        if self._painting:
            self._finish_stroke()
            return
        if self._scribbling:
            self._finish_scribble()
            return
        if self._mode == MODE_SAM_BOX and self._box_start is not None:
            rect = QRectF(self._box_start, self._box_current or self._box_start).normalized()
            self._box_start = None
            self._box_current = None
            if rect.width() >= 5 and rect.height() >= 5:
                self._box = rect
                self._box_item.setRect(rect)
                self.sam_box_changed.emit(QRectF(rect))
            else:
                self._box = None
                self._box_item.hide()
                self.sam_box_changed.emit(None)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._mode == MODE_POLYGON and event.button() == Qt.MouseButton.LeftButton:
            # 双击前的一次 press 已把同一个点追加进来，先去掉重复点
            if self._polygon_points:
                self._polygon_points.pop()
            self._close_polygon()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:
        self._cursor_item.hide()
        super().leaveEvent(event)

    # ------------------------------------------------------------------ 书写
    def _start_stroke(self, point: QPointF, *, erase: bool) -> None:
        if not erase and self._current_label <= 0:
            self.message.emit("请先在右侧添加并选择标签")
            return
        if erase:
            self._painting_label = 0
        else:
            self._painting_label = self._current_label
        self._document.begin_stroke()
        self._painting = True
        self._last_point = point
        self._document.stroke_segment(
            point,
            point,
            self._brush_size / 2.0,
            class_id=self._painting_label,
            erase=erase,
        )

    def _finish_stroke(self) -> None:
        if not self._painting:
            return
        self._painting = False
        self._last_point = None
        self._document.finish_stroke("橡皮擦" if self._mode == MODE_ERASER else "画笔")

    def _add_polygon_point(self, point: QPointF) -> None:
        if self._current_label <= 0:
            self.message.emit("请先在右侧添加并选择标签")
            return
        if len(self._polygon_points) >= 3 and _screen_distance(
            self, self._polygon_points[0], point
        ) <= 6:
            self._close_polygon()
            return
        self._polygon_points.append(point)
        self._polygon_item.set_points(self._polygon_points, self._label_color)
        self._polygon_item.set_view_scale(self.scale_factor)

    def _close_polygon(self) -> None:
        points = list(self._polygon_points)
        self._polygon_points = []
        self._polygon_item.set_points([])
        if len(points) < 3:
            return
        self.polygon_finished.emit(points)

    def _cancel_polygon(self) -> None:
        self._polygon_points = []
        self._polygon_item.set_points([])

    # ------------------------------------------------------------------ 键盘
    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._polygon_points:
                self._cancel_polygon()
                event.accept()
                return
            if self._sam_points or self._sam_scribbles or self._scribbling or self._box is not None:
                self.clear_prompts()
                event.accept()
                return
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._mode == MODE_POLYGON and len(self._polygon_points) >= 3:
                self._close_polygon()
                event.accept()
                return
            if self._mode in (MODE_SAM_POINT, MODE_SAM_BOX, MODE_SAM_SCRIBBLE):
                self.sam_apply_requested.emit()
                event.accept()
                return
        elif key == Qt.Key.Key_Backspace:
            if self._mode == MODE_POLYGON and self._polygon_points:
                self._polygon_points.pop()
                self._polygon_item.set_points(self._polygon_points, self._label_color)
                event.accept()
                return
            if self._mode == MODE_SAM_POINT and self._sam_points:
                self._remove_last_sam_point()
                event.accept()
                return
            if self._mode == MODE_SAM_SCRIBBLE and self._sam_scribbles:
                self._remove_last_scribble()
                event.accept()
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 光标
    def _update_cursor(self) -> None:
        if self._panning:
            return
        cursor = Qt.CursorShape.CrossCursor
        if self._mode in (MODE_BRUSH, MODE_ERASER):
            cursor = Qt.CursorShape.BlankCursor
        elif self._mode == MODE_PAN:
            cursor = Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)
        self._cursor_item.set_color(self._label_color)
        self._cursor_item.set_erase(self._mode == MODE_ERASER)
        self._cursor_item.set_radius(self._brush_size)
        if self._mode not in (MODE_BRUSH, MODE_ERASER):
            self._cursor_item.hide()


class _CursorRingItem(QGraphicsItem):
    """画笔/橡皮擦的圆形光标（场景坐标，半径即笔刷半径）。"""

    def __init__(self) -> None:
        super().__init__()
        self._radius = 10
        self._view_scale = 1.0
        self._color = QColor("#e6194b")
        self._erase = False
        self.setZValue(110)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_radius(self, radius: int) -> None:
        self.prepareGeometryChange()
        self._radius = max(0.5, float(radius))
        self.update()

    def set_view_scale(self, scale: float) -> None:
        self._view_scale = max(scale, 1e-9)
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def set_erase(self, erase: bool) -> None:
        self._erase = bool(erase)
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        margin = 2.0 / self._view_scale
        return QRectF(-self._radius - margin, -self._radius - margin, 2 * (self._radius + margin), 2 * (self._radius + margin))

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor("#e74c3c") if self._erase else QColor("white"), 1.2)
        pen.setCosmetic(True)
        if self._erase:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawEllipse(QPointF(0, 0), self._radius, self._radius)
        # 中心十字，便于精确落点
        cross = 3.0 / self._view_scale
        painter.drawLine(QPointF(-cross, 0), QPointF(cross, 0))
        painter.drawLine(QPointF(0, -cross), QPointF(0, cross))


def _screen_distance(view: QGraphicsView, first: QPointF, second: QPointF) -> float:
    p1 = view.mapFromScene(first)
    p2 = view.mapFromScene(second)
    return ((p1.x() - p2.x()) ** 2 + (p1.y() - p2.y()) ** 2) ** 0.5


def _colorize_mask(mask_image: QImage, color: QColor, *, alpha: int = 140) -> QImage:
    """把二值/灰度掩膜染成半透明彩色 ARGB 图（非零即前景）。"""
    import numpy as np

    image = mask_image
    if image.format() not in (
        QImage.Format.Format_Grayscale8,
        QImage.Format.Format_Indexed8,
    ):
        image = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height = image.width(), image.height()
    arr = image.constBits()
    arr.setsize(image.sizeInBytes())
    data = np.frombuffer(arr, dtype=np.uint8).reshape(height, image.bytesPerLine())[:, :width]
    out = np.zeros((height, width, 4), dtype=np.uint8)
    foreground = data > 0
    out[:, :, 0] = color.blue()
    out[:, :, 1] = color.green()
    out[:, :, 2] = color.red()
    out[:, :, 3] = np.where(foreground, alpha, 0)
    return QImage(out.data, width, height, width * 4, QImage.Format.Format_ARGB32).copy()
