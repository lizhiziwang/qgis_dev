"""数据浏览器 Dock。

封装 :class:`QgsBrowserGuiModel` + :class:`QgsBrowserTreeView`。

图层项支持拖出到画布添加（:meth:`BrowserDock` 开启视图拖拽，画布侧在
``MainWindow`` 拦截 drop 走与双击一致的 ``_add_layer_to_project`` 路径）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QAbstractItemView, QDockWidget

from qgis.gui import QgsBrowserGuiModel, QgsBrowserTreeView


class BrowserDock(QDockWidget):
    """数据浏览器。"""

    double_clicked = pyqtSignal(object)
    context_menu_requested = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__("数据浏览器", parent)
        self.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )

        self.model = QgsBrowserGuiModel(self)
        self.model.initialize()
        self.model.refresh()

        self.view = QgsBrowserTreeView(self)
        self.view.setModel(self.model)
        self.view.setHeaderHidden(True)
        # 允许把图层项拖出（拖到画布上添加图层）；只拖出不接收
        self.view.setDragEnabled(True)
        self.view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.context_menu_requested.emit)
        self.view.doubleClicked.connect(self.double_clicked.emit)

        self.setWidget(self.view)
