"""移动要素：拖拽平移**选中**的要素（对应 QGIS 的 Move Features 工具）。

**用法**：先「开始编辑」并用点选 / 框选选中要素 → 点工具条「移动要素」→
在画布上左键按下并拖动，松开即按位移量平移全部选中要素（拖动过程有跟随预览，
Esc 取消本次拖动）。

与 QGIS 一致的地方：

- 光标用**四向箭头**（``SizeAllCursor``）；
- 拖动起止点都走**捕捉**，按住起点吸附到某个顶点、松手再吸附到目标顶点，
  就能精确对齐（等价于 QGIS 里移动时的捕捉）；
- 作用于**全部选中要素**，不只按下的那一个。

平移**不改变面积与周长**，所以不重算面积类字段（与分割 / 补洞 / 补缝不同）。
每次拖动包进**一个** ``beginEditCommand("移动要素")``，Ctrl+Z 一次撤销。
按仓库惯例：改完清空间索引缓存（``clear_spatial_index``），画布刷新由
``QTimer.singleShot(0, ...)`` 推迟出事件栈。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsRubberBand

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.drawing.tools.base import _snap_indicator_for
from app.core.domain.drawing.tools.snap import snapped_match
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 小于这个屏幕距离（像素）视作「没拖动」，不产生编辑命令
_CLICK_TOLERANCE_PX = 2.0


class MoveFeaturesTool(QgsMapTool):
    """拖动平移选中要素的编辑工具（左键拖动 / Esc 取消本次拖动）。"""

    def __init__(
        self,
        canvas,
        editing_layer_getter: Callable[[], QgsVectorLayer | None] = lambda: None,
        hint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._layer_getter = editing_layer_getter
        self._hint = hint or (lambda text: None)
        #: 本次拖动的起点（画布地图坐标）与按下时所捕获的要素几何快照
        self._press_pt: QgsPointXY | None = None
        self._origin: list[tuple[int, QgsGeometry]] = []
        self._layer: QgsVectorLayer | None = None
        self._band: QgsRubberBand | None = None
        # QGIS 的移动工具就是四向箭头
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    # ============================================================== 生命周期

    def _init_band(self, layer: QgsVectorLayer) -> None:
        if self._band is None:
            self._band = QgsRubberBand(self.canvas, layer.geometryType())
            self._band.setColor(QColor(255, 0, 0))
            self._band.setWidth(2)
            self._band.setFillColor(QColor(255, 0, 0, 40))

    def activate(self) -> None:
        super().activate()

    def deactivate(self) -> None:
        self._reset_drag()
        self._set_snap_match(None)
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.key() == Qt.Key.Key_Escape:
            event.preventDefault()
            if self._press_pt is not None:
                self._reset_drag()
                self._hint("已取消本次移动")
            else:
                # 不在键盘事件栈内 unsetMapTool，推迟出栈
                QTimer.singleShot(0, self._quit)
            return
        super().keyPressEvent(event)

    def _quit(self) -> None:
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # ============================================================== 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        layer = self._current_layer()
        if layer is None:
            self._hint("没有编辑图层：请先点「开始编辑」选择图层")
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法移动要素")
            return

        fids = list(layer.selectedFeatureIds())
        if not fids:
            self._hint("请先用点选 / 框选选中要移动的要素")
            return

        origin: list[tuple[int, QgsGeometry]] = []
        for fid in fids:
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            geometry = feature.geometry()
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                continue
            # 深拷贝快照：拖动过程中要用它反复算预览，绝不能被就地改掉
            origin.append((fid, QgsGeometry.fromWkt(geometry.asWkt())))
        if not origin:
            self._hint("选中的要素没有可用几何，无法移动")
            return

        pt = self._snapped_pt(event, wait_index=True)
        if pt is None:
            return
        self._layer = layer
        self._origin = origin
        self._press_pt = pt
        self._init_band(layer)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pt is None or self._layer is None:
            return
        pt = self._snapped_pt(event, wait_index=False)
        if pt is None:
            return
        dx, dy = self._layer_delta(self._press_pt, pt)
        self._update_preview(dx, dy)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._press_pt is None:
            return
        pt = self._snapped_pt(event, wait_index=True)
        press_pt = self._press_pt
        layer = self._layer
        if pt is None or layer is None:
            self._reset_drag()
            return

        # 「没怎么动」按误触处理：不开编辑命令（beginEditCommand 会截断 redo 历史）
        if self._px_distance(press_pt, pt) < _CLICK_TOLERANCE_PX:
            self._reset_drag()
            self._hint("移动距离过小，未做改动")
            return

        dx, dy = self._layer_delta(press_pt, pt)
        origin = list(self._origin)
        self._reset_drag()
        self._apply_move(layer, origin, dx, dy)

    # ============================================================== 内部

    def _current_layer(self) -> QgsVectorLayer | None:
        return self._layer_getter()

    def _reset_drag(self) -> None:
        self._press_pt = None
        self._origin = []
        self._layer = None
        if self._band is not None:
            try:
                self._band.reset(Qgis.GeometryType.Unknown)
            except RuntimeError:
                pass

    def _translated(self, dx: float, dy: float) -> list[QgsGeometry]:
        """按 ``(dx, dy)``（图层 CRS）平移快照几何，返回新几何列表。"""
        moved: list[QgsGeometry] = []
        for _fid, geometry in self._origin:
            copy = QgsGeometry.fromWkt(geometry.asWkt())
            # translate() 就地修改、返回结果对象——不能拿返回值当几何用
            copy.translate(dx, dy)
            moved.append(copy)
        return moved

    def _update_preview(self, dx: float, dy: float) -> None:
        if self._band is None or self._layer is None:
            return
        moved = self._translated(dx, dy)
        if not moved:
            return
        try:
            combined = QgsGeometry.collectGeometry(moved)
            self._band.reset(self._layer.geometryType())
            self._band.setToGeometry(combined, self._layer)
            self._band.show()
        except RuntimeError:
            pass

    def _apply_move(
        self,
        layer: QgsVectorLayer,
        origin: list[tuple[int, QgsGeometry]],
        dx: float,
        dy: float,
    ) -> None:
        """把平移量写回要素；整批一个撤销命令。"""
        moved_geoms: list[tuple[int, QgsGeometry]] = []
        for fid, geometry in origin:
            copy = QgsGeometry.fromWkt(geometry.asWkt())
            copy.translate(dx, dy)
            moved_geoms.append((fid, copy))

        changed = 0
        layer.beginEditCommand("移动要素")
        try:
            for fid, geometry in moved_geoms:
                layer.changeGeometry(fid, geometry)
                changed += 1
        except RuntimeError:
            layer.destroyEditCommand()
            logger.exception("移动要素失败: layer=%s", layer.name())
            self._hint("移动要素失败：图层编辑 API 异常")
            return
        layer.endEditCommand()

        # 几何已变，空间索引缓存失效 → 清掉让下次命中重建。
        # 平移不改面积 / 周长，故不重算面积类字段。
        clear_spatial_index(layer)
        QTimer.singleShot(0, self._safe_refresh)
        self._hint(f"已移动 {changed} 个要素（可 Ctrl+Z 撤销）")

    def _layer_delta(
        self, start: QgsPointXY, end: QgsPointXY
    ) -> tuple[float, float]:
        """画布地图坐标下的位移 → 图层 CRS 下的位移。

        平移量是**向量**，不能直接对单个点做 CRS 变换（那样得到的是位置），
        必须把起止点都变换过去再相减。
        """
        layer = self._layer or self._current_layer()
        if layer is None:
            return (end.x() - start.x(), end.y() - start.y())
        src = self.canvas.mapSettings().destinationCrs()
        dst = layer.crs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return (end.x() - start.x(), end.y() - start.y())
        try:
            xform = QgsCoordinateTransform(
                src, dst, self.canvas.project() or QgsProject.instance()
            )
            a = xform.transform(QgsPointXY(start))
            b = xform.transform(QgsPointXY(end))
            return (b.x() - a.x(), b.y() - a.y())
        except Exception:  # noqa: BLE001
            logger.warning("移动要素：位移坐标转换失败", exc_info=True)
            return (end.x() - start.x(), end.y() - start.y())

    def _pixel_of_event(self, event):
        raw = event.pixelPoint()
        try:
            return raw.toPoint()
        except AttributeError:
            return raw

    def _px_distance(self, a: QgsPointXY, b: QgsPointXY) -> float:
        """两点在屏幕上的像素距离（用于判定「几乎没动」）。"""
        try:
            transform = self.canvas.getCoordinateTransform()
            pa = transform.transform(a)
            pb = transform.transform(b)
            return ((pa.x() - pb.x()) ** 2 + (pa.y() - pb.y()) ** 2) ** 0.5
        except RuntimeError:
            return float("inf")

    def _snapped_pt(self, event, wait_index: bool = True) -> QgsPointXY | None:
        """取点：优先吸附点，吸不到用原始点击位置。"""
        try:
            match = snapped_match(self.canvas, event, wait_index=wait_index)
        except Exception:  # noqa: BLE001 —— 吸附失败退回原始点
            match = None
        if match is not None and match.isValid():
            self._set_snap_match(match)
            return QgsPointXY(match.point())
        self._set_snap_match(None)
        try:
            return self.canvas.getCoordinateTransform().toMapCoordinates(
                self._pixel_of_event(event)
            )
        except RuntimeError:
            return None

    def _set_snap_match(self, match) -> None:
        """把吸附 match 喂给画布共享的 :class:`QgsSnapIndicator`（吸不到隐藏）。"""
        widget = _snap_indicator_for(self.canvas)
        if widget is None:
            return
        try:
            if match is not None and match.isValid():
                widget.setMatch(match)
                widget.setVisible(True)
            else:
                widget.setVisible(False)
        except Exception:  # noqa: BLE001
            pass

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass


__all__ = ["MoveFeaturesTool"]
