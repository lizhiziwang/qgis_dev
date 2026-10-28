"""属性表「组织列」对话框：勾选要显示的字段。

属性表 Dock 工具条上的「组织列」按钮弹出本对话框，列出当前图层的**全部字段**
（带复选框），用户勾选后点「确定」，属性表只显示被勾选的字段列。

- 列显示状态由 :class:`~app.ui.docks.attribute_dock.PagedAttributeTableWidget`
  用 ``QTableView.setColumnHidden`` 实现（模型结构不动）；
- 本对话框只负责收集勾选结果，不直接改表格；
- 属性表专用的 ID 列（要素 fid，不是真实字段）默认隐藏，勾选列表里单独提供
  一项「ID（要素 ID）」，需要按 ID 排序 / 冻结时再打开。
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

from app.core.util.text_encoding import repair_mojibake
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import CenteredCheckDelegate

#: 列表里「ID（要素 ID）」条目的标识（不是图层字段名，勾选结果里单独返回）
_ID_ITEM_KEY = "__organize_id__"


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
        include_id: bool = False,
        id_visible: bool = True,
    ) -> None:
        """
        Args:
            layer: 目标矢量图层。
            visible_names: 当前可见的字段名集合；``None`` 表示全部可见（默认）。
            parent: 父窗口。
            title: 自定义窗口标题（默认「<图层> - 组织列」）；导出字段选择复用本对话框。
            hint: 自定义提示文案（默认说明属性表列显示）。
            include_id: 是否在列表顶部额外提供「ID（要素 ID）」勾选项（属性表用）。
            id_visible: ID 列当前是否显示（仅 ``include_id`` 为 True 时有效）。
        """
        super().__init__(parent)
        self.layer = layer
        self._visible = None if visible_names is None else set(visible_names)
        self._title = title
        self._hint = hint
        self._include_id = bool(include_id)
        self._id_visible = bool(id_visible)
        self._id_item: QListWidgetItem | None = None
        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle(self._title or f"{self.layer.name()} - 组织列")
        self.setModal(True)
        # 无边框标题栏占高 34px，固定高度 +title_bar.height() 保住内容区
        self.setFixedSize(360, 460 + self.title_bar.height())

        main = QVBoxLayout()
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(6)

        hint = QLabel(
            self._hint
            or (
                "勾选要在属性表中显示的字段（ID 列可单独勾选）："
                if self._include_id
                else "勾选要在属性表中显示的字段："
            )
        )
        main.addWidget(hint)

        self._list = QListWidget()
        self._list.setItemDelegate(CenteredCheckDelegate(self._list))
        self._list.setObjectName("OrganizeColumnsList")
        # 行高由字体决定（约 14px），给 14px 勾选框留出上下间隔，避免相邻行勾选框相接。
        # 不能用 QSS 的 ::item padding / min-height 撑高：那只会把绘制矩形变高而布局
        # 行距不变，导致相邻行勾选框重叠（与启动对话框导航高亮踩过同一个坑）。
        self._list.setSpacing(2)
        if self._include_id:
            # ID 列不是图层字段，单独一项；默认按调用方传入的可见状态勾选
            item = QListWidgetItem("ID（要素 ID）")
            item.setData(Qt.ItemDataRole.UserRole, _ID_ITEM_KEY)
            item.setToolTip("属性表为每个要素附带的要素 ID（fid），不是图层字段")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if self._id_visible else Qt.CheckState.Unchecked
            )
            self._id_item = item
            self._list.addItem(item)
        fields = self.layer.fields()
        for index, field in enumerate(fields):
            # 显示名优先字段别名，无别名降级字段名；UserRole 存真实字段名，
            # 别名重复时也不影响勾选结果的唯一性。
            # GDB 的中文别名常是「UTF-8 被当成 Latin-1」的乱码，显示前先修；
            # 真实字段名保持原样，勾选结果和导出字段名不受影响。
            name = field.name()
            alias = self.layer.attributeAlias(index)
            display = repair_mojibake(alias or name) or name
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(f"{repair_mojibake(name)}（{field.typeName()}）")
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
        """返回被勾选的字段名（按图层字段顺序；不含 ID 条目）。"""
        names: list[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if data == _ID_ITEM_KEY:
                continue
            names.append(data)
        return names

    def id_visible(self) -> bool:
        """「ID（要素 ID）」是否被勾选（``include_id`` 未开启时恒为 True）。"""
        if self._id_item is None:
            return True
        return self._id_item.checkState() == Qt.CheckState.Checked
