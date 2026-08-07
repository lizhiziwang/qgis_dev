"""工具树 Dock。

迁自 :class:`component.TreeTool.PGISTools`，把"工具树 UI"与"瓦片后台任务"分离。

纯工具树展示，点击工具节点弹出对应参数对话框（对话框内部是
"左参数 + 右算法描述"布局，描述逻辑见 :mod:`app.ui.common.algo_desc`）。
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

from app.infra.logging import get_logger

logger = get_logger(__name__)


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
        item2 = QTreeWidgetItem(root)
        item2.setText(0, "矢量缓冲区")
        item2._code_ = "native:buffer"
        item3 = QTreeWidgetItem(root)
        item3.setText(0, "叠加分析")
        item3._code_ = "overlay"
        self.tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.tree)
        self.setWidget(wrapper)

    def _on_item_clicked(self, item, column) -> None:
        code = getattr(item, "_code_", "")
        try:
            if code == "native:writevectortiles_xyz":
                from app.ui.dialogs.vector_tile import VectorTileDialog
                dlg = VectorTileDialog(self.topParent, title="创建矢量瓦片", dia_type=code)
                dlg.show()
            elif code == "native:buffer":
                from app.ui.dialogs.buffer import BufferDialog
                dlg = BufferDialog(self.topParent, title="矢量缓冲区")
                dlg.show()
            elif code == "overlay":
                from app.ui.dialogs.overlay import OverlayDialog
                dlg = OverlayDialog(self.topParent, title="叠加分析")
                dlg.show()
        except Exception as exc:  # noqa: BLE001
            # 不静默吞掉：曾因静默异常导致功能"无反应"难排查（见 CanvasZoomTool 教训）
            logger.error("打开工具对话框失败 code=%s: %s", code, exc, exc_info=True)
