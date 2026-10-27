"""选择要登记到数据管理的未登记矢量图层（连库后 / 右键菜单共用）。"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.domain.local_layer_register import UnregisteredLayer
from app.ui.common.base_dialog import BaseDialog


class RegisterLayersDialog(BaseDialog):
    """列出库里未登记的矢量表，勾选后登记到数据管理。"""

    def __init__(self, parent, layers: list[UnregisteredLayer]) -> None:
        super().__init__(
            parent, title="登记未登记图层", show_min_button=False, show_max_button=False
        )
        self.setMinimumSize(0, 0)
        self.resize(580, 420)

        layout = QVBoxLayout()
        hint = QLabel(
            "以下图层在库里已经存在，但还没有登记到数据管理。勾选后点击「确定」登记 —— "
            "只写本工程的目录表（user_relation），不改动图层数据。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        for layer in layers:
            text = f"{layer.name}（{layer.geometry}，{layer.crs or '未知坐标系'}）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, layer.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)

        actions = QHBoxLayout()
        all_btn = QPushButton("全选")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn = QPushButton("全不选")
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        actions.addWidget(all_btn)
        actions.addWidget(none_btn)
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setDefault(True)
        ok.clicked.connect(self._validate)
        actions.addWidget(ok)
        layout.addLayout(actions)

        self.setLayout(layout)

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)

    def selected_names(self) -> list[str]:
        """勾选的表名（保持列表顺序）。"""
        names: list[str] = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                names.append(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        return [name for name in names if name]

    def _validate(self) -> None:
        if not self.selected_names():
            QMessageBox.warning(self, "登记未登记图层", "请至少勾选一个图层")
            return
        self.accept()


__all__ = ["RegisterLayersDialog"]
