"""工具树 Dock。

迁自 :class:`component.TreeTool.PGISTools`，把"工具树 UI"与"瓦片后台任务"分离。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDockWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class ToolsDock(QDockWidget):
    """工具树 dock。"""

    def __init__(self, parent=None) -> None:
        super().__init__("分析工具", parent)
        self.topParent = parent
        self.setFeatures(QDockWidget.AllDockWidgetFeatures)
        self.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self._build_ui()

    def _build_ui(self) -> None:
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        root = QTreeWidgetItem(self.tree)
        root.setText(0, "矢量常规")
        item1 = QTreeWidgetItem(root)
        item1.setText(0, "生成矢量瓦片（XYZ）")
        item1._code_ = "native:writevectortiles_xyz"
        self.tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.tree)
        self.setWidget(wrapper)

    def _on_item_clicked(self, item, column) -> None:
        try:
            if getattr(item, "_code_", "") == "native:writevectortiles_xyz":
                from app.ui.dialogs.vector_tile import VectorTileDialog
                dlg = VectorTileDialog(self.topParent, title="创建矢量瓦片", dia_type="native:writevectortiles_xyz")
                dlg.show()
        except Exception:  # noqa: BLE001
            pass
