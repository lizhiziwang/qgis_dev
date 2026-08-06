"""平移 / 缩放工具的薄包装。"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAction, QMainWindow, QToolBar

from qgis.gui import QgsMapToolPan, QgsMapToolZoom


class PanZoomTools:
    """平移 / 缩放工具集合。"""

    def __init__(self, canvas) -> None:
        self.canvas = canvas
        self.pan = QgsMapToolPan(canvas)
        self.zoom_in = QgsMapToolZoom(canvas, False)
        self.zoom_out = QgsMapToolZoom(canvas, True)

    def add_to_toolbar(self, main_window: QMainWindow) -> QToolBar:
        tb = main_window.addToolBar("地图工具")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

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
