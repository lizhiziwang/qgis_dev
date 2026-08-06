"""数据浏览器 Dock。

封装 :class:`QgsBrowserGuiModel` + :class:`QgsBrowserTreeView`。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QDockWidget

from qgis.gui import QgsBrowserGuiModel, QgsBrowserTreeView


class BrowserDock(QDockWidget):
    """数据浏览器。"""

    double_clicked = pyqtSignal(object)
    context_menu_requested = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__("数据浏览器", parent)
        self.setAllowedAreas(Qt.RightDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )

        self.model = QgsBrowserGuiModel(self)
        self.model.initialize()
        self.model.refresh()

        self.view = QgsBrowserTreeView(self)
        self.view.setModel(self.model)
        self.view.setHeaderHidden(True)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.context_menu_requested.emit)
        self.view.doubleClicked.connect(self.double_clicked.emit)

        self.setWidget(self.view)
