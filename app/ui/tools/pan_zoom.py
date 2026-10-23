"""平移 / 缩放工具的薄包装。"""

from __future__ import annotations

from contextlib import suppress

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QMainWindow, QToolBar

from qgis.gui import QgsMapTool, QgsMapToolPan, QgsMapToolZoom


class ShiftZoomTool(QgsMapTool):
    """拉框缩放：默认拉框放大，按住 Shift 拉框缩小。

    与顶部工具条「放大 / 缩小」使用**同一套** :class:`QgsMapToolZoom`：按下
    左键时按 Shift 状态选一个原生工具，后续移动 / 释放事件原样转发。这样拉框
    样式、单击缩放倍率、双击行为都与通用工具完全一致，不另写一套几何计算。
    侧栏「缩放」按钮用本工具。
    """

    def __init__(self, canvas) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._zoom_in = QgsMapToolZoom(canvas, False)
        self._zoom_out = QgsMapToolZoom(canvas, True)
        self._active: QgsMapToolZoom | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _tool_for(self, event) -> QgsMapToolZoom:
        """按 Shift 状态选原生放大 / 缩小工具。"""
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            return self._zoom_out
        return self._zoom_in

    def canvasPressEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._active = self._tool_for(event)
        self._active.canvasPressEvent(event)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if self._active is not None:
            self._active.canvasMoveEvent(event)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        if self._active is None:
            return
        self._active.canvasReleaseEvent(event)
        self._active = None

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        self._tool_for(event).canvasDoubleClickEvent(event)

    def deactivate(self) -> None:
        """切换工具时若正拉着框：清掉原生工具的橡皮筋，避免残影。"""
        if self._active is not None:
            with suppress(Exception):
                self._active.clean()
            self._active = None
        super().deactivate()


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
