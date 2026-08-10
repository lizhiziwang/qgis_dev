"""样式 IO Dialog（保存 / 打开 QML）。"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.infra.fs.io import write_text
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir


class StyleSaveDialog(BaseDialog):
    """保存样式 Dialog。"""

    def __init__(self, layer, parent=None) -> None:
        super().__init__(parent, "保存样式")
        self.layer = layer
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setMinimumSize(400, 300)
        main = QVBoxLayout()

        # 文件名
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("文件名："))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("请输入文件名")
        row1.addWidget(self.name_input)
        main.addLayout(row1)

        # 路径
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("文件路径："))
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("点击\"选择文件\"或手动输入")
        sel = QPushButton("选择文件")
        sel.setFixedWidth(80)
        sel.clicked.connect(lambda: self._open_dir())
        row2.addWidget(self.path_input)
        row2.addWidget(sel)
        main.addLayout(row2)
        main.addStretch()

        # 按钮
        btn = QHBoxLayout()
        btn.addStretch()
        save = QPushButton("保存")
        save.setObjectName("confirm_button")
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.close)
        btn.addWidget(save)
        btn.addWidget(cancel)
        main.addLayout(btn)
        self.setLayout(main)

    def _open_dir(self) -> None:
        path = open_file_dir(self, is_dir=True)
        if path:
            self.path_input.setText(path)

    def _save(self) -> None:
        name = self.name_input.text()
        path = self.path_input.text()
        if not name or not path:
            return
        self.layer.styleManager().addStyleFromLayer(name)
        style_obj = self.layer.styleManager().style(name)
        xml = style_obj.xmlData()
        write_text(os.path.join(path, name + ".qml"), xml)
        self.close()


class StyleOpenDialog(BaseDialog):
    """打开样式 Dialog。"""

    def __init__(self, layer, parent=None) -> None:
        super().__init__(parent, "打开样式文件")
        self.style_path: Optional[str] = None
        self.layer = layer
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setMinimumSize(500, 150)
        main = QVBoxLayout()
        row = QHBoxLayout()
        label = QLabel("选择 QML 文件：")
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("点击\"选择文件\"或手动输入 .qml 文件路径")
        sel = QPushButton("选择文件…")
        sel.clicked.connect(lambda: self._open_style())
        row.addWidget(label)
        row.addWidget(self.path_input, 1)
        row.addWidget(sel)
        main.addLayout(row)

        btn = QHBoxLayout()
        btn.addStretch()
        load = QPushButton("加载")
        load.setDefault(True)
        load.clicked.connect(self._load_style)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.close)
        btn.addWidget(load)
        btn.addWidget(cancel)
        main.addLayout(btn)
        self.setLayout(main)

    def _open_style(self) -> None:
        path = open_file_dir(self, is_dir=False, filter_="*.qml")
        if path:
            self.style_path = path
            self.path_input.setText(path)

    def _load_style(self) -> None:
        if not self.style_path:
            return
        self.layer.loadNamedStyle(self.style_path)
        self.close()
