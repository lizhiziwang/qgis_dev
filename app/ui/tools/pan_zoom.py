"""平移 / 缩放工具的薄包装。"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QMainWindow, QToolBar

from qgis.gui import QgsMapTool, QgsMapToolPan, QgsMapToolZoom


class ZoomClickTool(QgsMapTool):
    """单击缩放工具：左键单击放大、Shift+单击缩小（以点击点为缩放中心）。

    与 :class:`QgsMapToolZoom` 的拉框缩放不同：不拉框，单击即按固定倍率缩放。
    侧栏「缩放」按钮用本工具；顶部工具条的「放大 / 缩小」仍是 ``QgsMapToolZoom``。
    """

    #: ``zoomByFactor`` 语义：因子 < 1 放大、> 1 缩小（见 QgsMapCanvas 文档）
    _IN_FACTOR = 0.5
    _OUT_FACTOR = 2.0

    def __init__(self, canvas) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.button() != Qt.MouseButton.LeftButton:
            return
        zoom_out = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        factor = self._OUT_FACTOR if zoom_out else self._IN_FACTOR
        # 给定 point → 以点击点为中心（缩放后该点仍在视口原处附近）
        self.canvas.zoomByFactor(factor, event.mapPoint())


class PanZoomTools:
    """平移 / 缩放工具集合。"""

    def __init__(self, canvas) -> None:
        self.canvas = canvas
        self.pan = QgsMapToolPan(canvas)
        self.zoom_in = QgsMapToolZoom(canvas, False)
        self.zoom_out = QgsMapToolZoom(canvas, True)

    def add_to_toolbar(self, main_window: QMainWindow) -> QToolBar:
        tb = main_window.addToolBar("地图工具")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        a_pan = QAction("平移", main_window)
        a_pan.triggered.connect(lambda: self.canvas.setMapTool(self.pan))
        tb.addAction(a_pan)

        a_zi = QAction("放大", main_window)
        a_zi.triggered.connect(lambda: self.canvas.setMapTool(self.zoom_in))
        tb.addAction(a_zi)

        a_zo = QAction("缩小", main_window)
        a_zo.triggered.connect(lambda: self.canvas.setMapTool(self.zoom_out))
        tb.addAction(a_zo)
        return tb
