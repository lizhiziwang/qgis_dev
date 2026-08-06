"""分析面板 Dock（占位）。"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDockWidget


class AnalyseDock(QDockWidget):
    """分析面板 dock（当前为占位；后续放"分析"业务 UI）。"""

    def __init__(self, parent=None) -> None:
        super().__init__("分析工具", parent)
        self.topParent = parent
        self.setFeatures(QDockWidget.AllDockWidgetFeatures)
        self.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
