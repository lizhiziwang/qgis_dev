"""在本地库里新建字典表（可选从应用级字典管理复制内容）。"""

from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox


class AddLocalDictDialog(BaseDialog):
    """输入字典表名；内容来源可选「创建空表」或某个应用级字典表。"""

    def __init__(self, parent, *, suggested_name: str = "字典表", categories: list[str] = ()) -> None:
        super().__init__(parent, title="添加字典表", show_min_button=False, show_max_button=False)
        self.setMinimumSize(0, 0)
        self.resize(560, 280)
        layout = QVBoxLayout()
        hint = QLabel(
            "在当前本地库里新建一张字典表。可以从「字典管理」复制整张字典表的全部字典域，"
            "也可以先建空表再逐行添加。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.name_edit = QLineEdit(suggested_name)
        form.addRow("字典表名称：", self.name_edit)
        self.source_combo = QComboBox()
        self.source_combo.addItem("创建空字典表", "")
        for category in categories:
            self.source_combo.addItem(f"从字典管理复制：{category}", category)
        form.addRow("内容来源：", self.source_combo)
        layout.addLayout(form)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setDefault(True)
        ok.clicked.connect(self._validate)
        footer.addWidget(ok)
        layout.addLayout(footer)
        self.setLayout(layout)

    def _validate(self) -> None:
        if not self.name_edit.text().strip():
            MessageBox.warning(self, "添加字典表", "字典表名称不能为空")
            return
        self.accept()

    def table_name(self) -> str:
        return self.name_edit.text().strip()

    def source_category(self) -> str:
        return str(self.source_combo.currentData() or "")
