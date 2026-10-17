"""属性表「组织列」对话框：勾选要显示的字段。

属性表 Dock 工具条上的「组织列」按钮弹出本对话框，列出当前图层的**全部字段**
（带复选框），用户勾选后点「确定」，属性表只显示被勾选的字段列。

- 列显示状态由 :class:`~app.ui.docks.attribute_dock.PagedAttributeTableWidget`
  用 ``QTableView.setColumnHidden`` 实现（模型结构不动，ID 列恒显示）；
- 本对话框只负责收集勾选结果，不直接改表格。
"""

from __future__ import annotations

from collections.abc import Collection

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import CenteredCheckDelegate


class OrganizeColumnsDialog(BaseDialog):
    """勾选属性表要显示的字段列。

    :param layer: 目标矢量图层。
    :param visible_names: 当前可见的字段名集合；``None`` 表示全部可见（默认）。
        对话框据此预置勾选状态。
    :param parent: 父窗口。
    """

    def __init__(
        self,
        layer: QgsVectorLayer,
        visible_names: Collection[str] | None = None,
        parent: QWidget | None = None,
        *,
        title: str = "",
        hint: str = "",
    ) -> None:
        """
        Args:
            layer: 目标矢量图层。
            visible_names: 当前可见的字段名集合；``None`` 表示全部可见（默认）。
            parent: 父窗口。
            title: 自定义窗口标题（默认「<图层> - 组织列」）；导出字段选择复用本对话框。
            hint: 自定义提示文案（默认说明属性表列显示）。
        """
        super().__init__(parent)
        self.layer = layer
        self._visible = None if visible_names is None else set(visible_names)
        self._title = title
        self._hint = hint
        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle(self._title or f"{self.layer.name()} - 组织列")
        self.setModal(True)
        # 无边框标题栏占高 34px，固定高度 +title_bar.height() 保住内容区
        self.setFixedSize(360, 460 + self.title_bar.height())

        main = QVBoxLayout()
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(6)

        hint = QLabel(self._hint or "勾选要在属性表中显示的字段（ID 列固定显示）：")
        main.addWidget(hint)

        self._list = QListWidget()
        self._list.setItemDelegate(CenteredCheckDelegate(self._list))
        self._list.setObjectName("OrganizeColumnsList")
        # 行高由字体决定（约 14px），给 14px 勾选框留出上下间隔，避免相邻行勾选框相接。
        # 不能用 QSS 的 ::item padding / min-height 撑高：那只会把绘制矩形变高而布局
        # 行距不变，导致相邻行勾选框重叠（与启动对话框导航高亮踩过同一个坑）。
        self._list.setSpacing(2)
        for field in self.layer.fields():
            # 显示名优先字段别名，无别名降级字段名；UserRole 存真实字段名，
            # 别名重复时也不影响勾选结果的唯一性
            display = self.layer.attributeAlias(self.layer.fields().indexOf(field.name())) or field.name()
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, field.name())
            item.setToolTip(f"{field.name()}（{field.typeName()}）")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = self._visible is None or field.name() in self._visible
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        main.addWidget(self._list, 1)

        # 全选 / 全不选
        quick = QHBoxLayout()
        all_btn = QPushButton("全选")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn = QPushButton("全不选")
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        quick.addWidget(all_btn)
        quick.addWidget(none_btn)
        quick.addStretch(1)
        main.addLayout(quick)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        main.addWidget(bb)

        self.setLayout(main)

    def _set_all(self, state: Qt.CheckState) -> None:
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)

    def checked_field_names(self) -> list[str]:
        """返回被勾选的字段名（按图层字段顺序）。"""
        names: list[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                names.append(item.data(Qt.ItemDataRole.UserRole))
        return names
