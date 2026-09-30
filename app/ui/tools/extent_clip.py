"""范围裁剪：在地图上拉一个矩形框，交给主窗口把图层过滤到框内。

编辑工具条之外的一个**会话型**侧栏工具（见
:class:`~app.ui.main_window.side_toolbar.SideToolBar` 的 ``_SESSION_KEYS``）：

- 激活后光标变十字，**按住左键拖出矩形**（半透明青色预览框，与框选工具同一套
  视觉），松开即回调 ``on_clip(rect)``——真正把图层过滤到框内是主窗口的事
  （:meth:`~app.ui.main_window.mixins.map_tools.MapToolsMixin._apply_extent_clip`）；
- **Esc** / **右键** → 取消本次拉框（不改变已有裁剪）；
- 拉完即自动退出拉框模式（工具只管"划范围"，裁剪结果由主窗口维持），要改范围
  就再点一次侧栏按钮。

只负责"划出矩形"，不做任何图层改动——与 :mod:`app.ui.tools.select_feature` 的
框选同一分工。矩形坐标是**画布 CRS**，换算到各图层 CRS 由调用方做。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor

#: 拖框小于这个像素数视为误触（只点了一下没真拉框），不触发裁剪
_MIN_DRAG_PX = 4


class ExtentClipTool(QgsMapTool):
    """按住左键拉矩形 → 回调 ``on_clip(QgsRectangle)``。"""

    def __init__(self, canvas, on_clip: Callable[[QgsRectangle], None] | None = None) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._on_clip = on_clip or (lambda rect: None)
        self._start: QgsPointXY | None = None
        self._end: QgsPointXY | None = None
        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(Qt.GlobalColor.cyan)
        self._band.setWidth(2)
        self._band.setFillColor(QColor(0, 200, 255, 60))
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._start = self._map_point(event)
        self._end = self._start
        self._band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is None:
            return
        self._end = self._map_point(event)
        self._refresh_band()

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._reset()
            return
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        self._end = self._map_point(event)
        rect = QgsRectangle(self._start, self._end, True)
        dragged = self._is_real_drag()
        self._reset()
        if not dragged or rect.width() <= 0.0 or rect.height() <= 0.0:
            return  # 只点了一下没拉框：不动已有裁剪
        self._on_clip(rect)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._reset()
            return
        super().keyPressEvent(event)

    def deactivate(self) -> None:  # noqa: N802
        self._reset()
        super().deactivate()

    def cleanup(self) -> None:
        """工具条收尾时清掉预览框（与其它画布工具同一约定）。"""
        self._reset()

    # -------------------------------------------------- 内部

    def _is_real_drag(self) -> bool:
        """按下到松开是否真拖出了一段（防"只点一下"被当成裁剪）。"""
        if self._start is None or self._end is None:
            return False
        mupp = self.canvas.mapSettings().mapUnitsPerPixel()
        if mupp <= 0:
            return True
        d = ((self._start.x() - self._end.x()) ** 2
             + (self._start.y() - self._end.y()) ** 2) ** 0.5
        return d / mupp >= _MIN_DRAG_PX

    def _map_point(self, event) -> QgsPointXY:
        return self.canvas.getCoordinateTransform().toMapCoordinates(event.pixelPoint())

    def _refresh_band(self) -> None:
        if self._start is None or self._end is None:
            return
        rect = QgsRectangle(self._start, self._end, True)
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        for p in (
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
        ):
            self._band.addPoint(p)
        self._band.show()

    def _reset(self) -> None:
        self._start = None
        self._end = None
        try:
            self._band.reset(QgsWkbTypes.PolygonGeometry)
            self._band.hide()
        except RuntimeError:
            pass  # 画布已销毁


__all__ = ["ExtentClipTool"]
