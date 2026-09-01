"""点选 / 框选工具。

通用工具条上的「点选」「框选」两个按钮激活的交互工具：

- :class:`PointSelectTool` —— 单击：选中落在点击位置附近（默认 10px 容差）
  的要素；
- :class:`RectSelectTool` —— 左键拖拽画矩形，松开时选中矩形内的要素。

选择范围 = 画布当前可见的矢量图层（``canvas.layers()`` 只含渲染图层，
隐藏图层不在其中）。默认（无修饰键）每次选中**替换**上一次的选中集，
点空白处清空该层选中；按住 ``Ctrl`` 时**追加**到现有选中（多选）——
行为对齐 QGIS 的 Select Features。

注意：本 QGIS 构建里 ``QgsVectorLayer.select()`` 是**追加**语义，替换必须
显式 ``selectByIds(fids, Qgis.SelectBehavior.SetSelection)``。

空间过滤复用 :func:`app.core.domain.check.query.intersect_features`
（空间索引粗筛 + 精确相交），几何坐标从画布 CRS 变换到各图层 CRS。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayer
    from qgis.gui import QgsMapCanvas

from qgis.PyQt.QtCore import Qt
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
        # 显式刷新，确保选中高亮立即渲染（selectByIds 不会自动触发重绘）
        self.canvas.refresh()

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


class PointSelectTool(SelectFeatureToolBase):
    """点选：单击选中落在点击位置附近（默认 10px 容差）的要素。"""

    #: 点击容差（像素）：把点击点扩成小矩形再求交，点 / 线 / 面要素都能命中。
    TOLERANCE_PX = 10

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
