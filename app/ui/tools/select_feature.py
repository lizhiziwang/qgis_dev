"""点选 / 框选工具。

通用工具条上的「点选」「框选」两个按钮激活的交互工具：

- :class:`PointSelectTool` —— 单击：选中落在点击位置附近（默认 10px 容差）
  的要素；光标为**箭头**（对齐 QGIS 的选择手感）；
- :class:`RectSelectTool` —— 左键拖拽画矩形，松开时选中矩形内的要素；
  光标为**十字**（提示正在拖框）。

两个工具都**显式** ``setCursor``：不设的话画布会沿用上一个工具遗留的光标
（画完面之后是十字），看起来就像「选择工具的光标不对」。

选择范围 = 画布当前可见的矢量图层（``canvas.layers()`` 只含渲染图层，
隐藏图层不在其中）。默认（无修饰键）每次选中**替换**上一次的选中集，
点空白处清空该层选中；按住 ``Ctrl`` 时**追加**到现有选中（多选）——
行为对齐 QGIS 的 Select Features。

注意：本 QGIS 构建里 ``QgsVectorLayer.select()`` 是**追加**语义，替换必须
显式 ``selectByIds(fids, Qgis.SelectBehavior.SetSelection)``。

编辑态删除：画布焦点下按 ``Delete`` 键 → 把**可编辑**图层（处于编辑态）上的
选中要素整层删除（每层一个撤销步骤，可整体 ``Ctrl+Z`` 撤销；未处于编辑态的
图层即使有选中也不动）。两个工具都继承 :meth:`SelectFeatureToolBase.keyPressEvent`。

空间过滤复用 :func:`app.core.domain.check.query.intersect_features`
（走 provider 的空间过滤 + 精确相交）；几何坐标从画布 CRS 变换到各图层 CRS。
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger

logger = get_logger(__name__)


class SelectFeatureToolBase(QgsMapTool):
    """点选 / 框选公共逻辑：按几何在可见矢量图层上选要素。"""

    def __init__(self, canvas: "QgsMapCanvas") -> None:
        super().__init__(canvas)
        self.canvas = canvas

    # -------------------------------------------------- 核心

    def _select_geometry(self, geom: "QgsGeometry", behavior: Qgis.SelectBehavior) -> None:
        """在画布可见矢量图层上，按 ``geom`` 选中要素（逐层处理选中集）。

        ``behavior`` 取 :class:`Qgis.SelectBehavior`：
        - ``SetSelection`` —— 替换（默认）：每层只保留本次命中的要素，
          无命中的层清空；
        - ``AddToSelection`` —— Ctrl 追加：把命中要素并进现有选中，
          点空白处不清空。

        ``geom`` 位于画布 CRS；与图层 CRS 不同时由
        :func:`intersect_features` 内部做坐标变换。
        """
        if geom is None or geom.isEmpty():
            return
        project = self.canvas.project() or QgsProject.instance()
        source_crs = self.canvas.mapSettings().destinationCrs()
        for layer in self._selectable_layers():
            fids = intersect_features(layer, geom, source_crs, project)
            if fids:
                # 显式 behavior：本 QGIS 构建里 select() 是追加语义，不能替换
                layer.selectByIds(fids, behavior)
            elif behavior == Qgis.SelectBehavior.SetSelection:
                # 无修饰键点到空白处：清空该层选中（Ctrl 追加时保留现有选中）
                layer.removeSelection()
        # 不在这里显式 ``canvas.refresh()``：``selectByIds`` / ``removeSelection``
        # 会发 ``selectionChanged`` -> 图层 ``triggerRepaint()`` -> ``repaintRequested``，
        # 画布自己就会重画（实测两条各发 1 次 ``repaintRequested``）。
        # 再显式刷一次就是白刷第二遍整幅图；而且 ``AppMapCanvas`` 下画布缓存只在
        # ``triggerRepaint`` 通路里生效，多余的显式 refresh 反而会清缓存变慢。

    def _selection_behavior(self, event) -> Qgis.SelectBehavior:
        """按住 Ctrl 追加选择，否则替换上一次选中（对齐 QGIS Select Features）。"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return Qgis.SelectBehavior.AddToSelection
        return Qgis.SelectBehavior.SetSelection

    def _selectable_layers(self) -> List["QgsVectorLayer"]:
        return [
            layer
            for layer in self.canvas.layers()
            if isinstance(layer, QgsVectorLayer) and layer.isValid()
        ]

    def _map_point(self, event) -> "QgsPointXY":
        """事件像素坐标 → 画布 CRS 地图坐标。"""
        return self.canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())

    # -------------------------------------------------- 键盘

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        """Delete 键：删除可编辑图层上的选中要素（点选 / 框选共用）。"""
        if event.key() == Qt.Key.Key_Delete:
            self._delete_selected_editable()
            return
        super().keyPressEvent(event)

    def _delete_selected_editable(self) -> None:
        """删除所有「可编辑且有选中要素」的可见矢量图层上的选中要素。

        与 QGIS「删除选中」对齐：只有处于编辑态（``layer.isEditable()``）的
        图层才会被删，未编辑的图层即使有选中也不受影响。每个图层各包一个
        撤销步骤（``beginEditCommand`` / ``deleteFeatures`` /
        ``endEditCommand``），可整体 ``Ctrl+Z`` 撤销。删除成功后清掉该层
        空间索引缓存（质检批量检查用的 Python 侧 ``QgsSpatialIndex`` 是快照，
        不清会读到已删要素），并推迟出栈刷新画布（``deleteFeatures`` 自身已发
        ``repaintRequested``，这里只做兜底）。
        """
        deleted = 0
        for layer in self._selectable_layers():
            if not layer.isEditable():
                continue
            fids = layer.selectedFeatureIds()
            if not fids:
                continue
            layer.beginEditCommand("删除选中要素")
            try:
                ok = layer.deleteFeatures(list(fids))
            except RuntimeError:
                ok = False
            if not ok:
                layer.cancelEditCommand()
                logger.warning("Delete 键删除要素失败: layer=%s", layer.name())
                continue
            layer.endEditCommand()
            # 删除要素 → 空间索引缓存失效 → 清掉让下次命中重建
            clear_spatial_index(layer)
            deleted += len(fids)
        if deleted:
            logger.info("Delete 键删除选中要素 %d 个", deleted)
            # 仓库约束：事件栈内不直接 refresh，推迟出栈后再刷
            QTimer.singleShot(0, self._safe_refresh)

    def _safe_refresh(self) -> None:
        with suppress(RuntimeError):
            self.canvas.refresh()


class PointSelectTool(SelectFeatureToolBase):
    """点选：单击选中落在点击位置附近（默认 10px 容差）的要素。"""

    #: 点击容差（像素）：把点击点扩成小矩形再求交，点 / 线 / 面要素都能命中。
    TOLERANCE_PX = 10

    def __init__(self, canvas: "QgsMapCanvas") -> None:
        super().__init__(canvas)
        # 显式设光标：不设的话画布会沿用上一个工具遗留的光标（比如画完面之后的
        # 十字），看起来就像「选择工具的光标不对」。点选对齐 QGIS 的选择手感 → 箭头。
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.button() != Qt.MouseButton.LeftButton:
            return
        map_pt = self._map_point(event)
        tol = self.TOLERANCE_PX * self.canvas.mapSettings().mapUnitsPerPixel()
        rect = QgsRectangle(
            map_pt.x() - tol,
            map_pt.y() - tol,
            map_pt.x() + tol,
            map_pt.y() + tol,
        )
        self._select_geometry(
            QgsGeometry.fromRect(rect), self._selection_behavior(event)
        )


class RectSelectTool(SelectFeatureToolBase):
    """框选：左键拖拽画矩形，松开时选中矩形内要素。"""

    def __init__(self, canvas: "QgsMapCanvas") -> None:
        super().__init__(canvas)
        # 框选用十字：提示「正在拖框」，也避免沿用上一个工具遗留的光标
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._start: Optional[QgsPointXY] = None
        self._end: Optional[QgsPointXY] = None
        # 框选用半透明青色，与绘制工具（红色）区分
        self._rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._rubber_band.setColor(Qt.GlobalColor.cyan)
        self._rubber_band.setWidth(2)
        self._rubber_band.setFillColor(QColor(0, 200, 255, 60))

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._start = self._map_point(event)
        self._end = self._start
        self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is None:
            return
        self._end = self._map_point(event)
        self._update_rubber_band()

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        self._end = self._map_point(event)
        rect = QgsRectangle(self._start, self._end, True)
        self._reset()
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            return
        self._select_geometry(
            QgsGeometry.fromRect(rect), self._selection_behavior(event)
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._reset()
            return
        super().keyPressEvent(event)

    def deactivate(self) -> None:  # noqa: N802
        self._reset()
        super().deactivate()

    # -------------------------------------------------- 内部

    def _update_rubber_band(self) -> None:
        if self._start is None or self._end is None:
            return
        rect = QgsRectangle(self._start, self._end, True)
        self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        for p in (
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
        ):
            self._rubber_band.addPoint(p)
        self._rubber_band.show()

    def _reset(self) -> None:
        self._start = None
        self._end = None
        try:
            self._rubber_band.hide()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["PointSelectTool", "RectSelectTool"]
