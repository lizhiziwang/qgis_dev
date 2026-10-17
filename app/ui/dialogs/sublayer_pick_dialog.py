"""从一份容器数据源（``.gdb`` / ``.gpkg`` / ``.sqlite``）里挑图层：**可多选**。

「图层面板 → 添加图层」选了容器类数据源时用。以前的行为是：GeoPackage / SQLite 只
打开第一层、GDB 干脆整库全加载；用户要的是**先把里面的图层列出来，再点某个或某些
加载**（与「数据管理 → 导入数据」的选择体验一致）。

列图层走 :func:`app.core.util.sublayers.list_sublayers`（QGIS 的 ``querySublayers``，
中文版 QGIS 也不受影响）；打开按 :func:`~app.core.util.sublayers.open_sublayer`
（本地库沿用本工程的打开策略 —— ``.sqlite`` 用 spatialite provider，不会多出 ``rowid`` 列）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.util.sublayers import Sublayer, list_sublayers
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)


class SublayerPickDialog(BaseDialog):
    """列出容器数据源里的图层，供用户挑一个或多个加载。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        path: str,
        title: str = "选择要加载的图层",
    ) -> None:
        super().__init__(parent, title)
        self._path = str(path)
        self.setMinimumSize(0, 0)
        self.resize(520, 400)
        self._sublayers = list_sublayers(self._path)
        self._build()

    # --------------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QLabel(f"数据源：{self._path}")
        head.setObjectName("SectionLabel")
        head.setWordWrap(True)
        layout.addWidget(head)
        if self._sublayers:
            layout.addWidget(QLabel(f"共 {len(self._sublayers)} 个图层，按住 Ctrl / Shift 可多选："))

        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.layer_list.itemSelectionChanged.connect(self._refresh_ok)
        for sublayer in self._sublayers:
            text = sublayer.name if sublayer.has_geometry else f"{sublayer.name}（无几何）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, sublayer)
            self.layer_list.addItem(item)
        layout.addWidget(self.layer_list, 1)

        self.status = QLabel()
        self.status.setObjectName("PropHint")
        self.status.setWordWrap(True)
        if not self._sublayers:
            self.status.setText(
                "这份数据里没有可加载的图层（或打不开）。"
                "GDB 若需要 ESRI 的 FileGDB 驱动，请改用 GeoPackage / Shapefile。"
            )
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setEnabled(bool(self._sublayers))
        self.select_all_btn.clicked.connect(self.layer_list.selectAll)
        row.addWidget(self.select_all_btn)
        row.addStretch(1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("加载")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row.addWidget(self.buttons)
        layout.addLayout(row)
        self.setLayout(layout)

        # 单层数据源直接选中（少点一下）；多层不预选，让用户自己挑
        if len(self._sublayers) == 1:
            self.layer_list.item(0).setSelected(True)
        self._refresh_ok()

    # --------------------------------------------------------------- 交互

    def _refresh_ok(self, *_args: object) -> None:
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(self.selected_sublayers()))

    # --------------------------------------------------------------- 取值

    def selected_sublayers(self) -> list[Sublayer]:
        """选中的子图层（可能多个）。"""
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.layer_list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole) is not None
        ]


__all__ = ["SublayerPickDialog"]
