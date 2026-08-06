"""图层树 Dock。

封装 :class:`QgsLayerTreeView` + :class:`QgsLayerTreeModel`，并发出右键菜单信号。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QDockWidget, QWidget

from qgis.core import QgsLayerTreeModel, QgsProject
from qgis.gui import QgsLayerTreeView


class LayerDock(QDockWidget):
    """独立的图层树面板。"""

    context_menu_requested = pyqtSignal(object)  # QPoint
    layer_clicked = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__("图层列表", parent)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self.project: QgsProject = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()

        self.model = QgsLayerTreeModel(self.tree_root)
        self.model.setFlags(
            QgsLayerTreeModel.AllowNodeRename
            | QgsLayerTreeModel.AllowNodeReorder
            | QgsLayerTreeModel.AllowNodeChangeVisibility
            # 不显示图例：分类/渐变渲染会挤进 "1"、"2" 等图例项
        )

        self.view = QgsLayerTreeView(self)
        self.view.setModel(self.model)
        self.view.clicked.connect(self.layer_clicked.emit)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.context_menu_requested.emit)

        self.setWidget(self.view)
