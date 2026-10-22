"""导入样式：从当前工程同类型图层或外部 QGIS 样式文件导入渲染方式。"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.base_dialog import BaseDialog

_KIND_NAMES = {"point": "点", "line": "线", "polygon": "面"}


class ImportStyleDialog(BaseDialog):
    """选择导入来源：工程内同几何类型的图层，或外部 QGIS 样式文件。"""

    def __init__(self, layers, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, f"导入样式 - {_KIND_NAMES.get(kind, kind)}符号")
        self._layers = list(layers)
        self._kind = kind
        self.setMinimumSize(0, 0)
        self.resize(560, 300)
        self._build()
        self._sync_state()

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        kind_name = _KIND_NAMES.get(self._kind, self._kind)
        hint = QLabel(
            f"只能导入与当前图层相同几何类型（{kind_name}）的样式。", self
        )
        hint.setObjectName("PropHint")
        root.addWidget(hint)

        self.layer_radio = QRadioButton("从当前工程的图层导入", self)
        self.layer_radio.setChecked(bool(self._layers))
        self.layer_radio.setEnabled(bool(self._layers))
        root.addWidget(self.layer_radio)

        self.layer_combo = QComboBox(self)
        for layer in self._layers:
            self.layer_combo.addItem(layer.name(), layer)
        if not self._layers:
            self.layer_combo.addItem("（没有其它同类型的矢量图层）", None)
        root.addWidget(self.layer_combo)

        self.file_radio = QRadioButton("从外部样式文件导入（QGIS .qml / .xml）", self)
        self.file_radio.setChecked(not self._layers)
        root.addWidget(self.file_radio)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText("选择 QGIS 图层样式 / 符号文件")
        self.path_edit.textChanged.connect(self._sync_state)
        self.browse_btn = QPushButton("浏览…", self)
        self.browse_btn.clicked.connect(self._on_browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.browse_btn)
        root.addLayout(row)
        root.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.setLayout(root)

        self.layer_radio.toggled.connect(self._sync_state)
        self.file_radio.toggled.connect(self._sync_state)
        self.layer_combo.currentIndexChanged.connect(self._sync_state)

    def _sync_state(self, *_args) -> None:
        layer_mode = self.layer_radio.isChecked()
        self.layer_combo.setEnabled(layer_mode and bool(self._layers))
        file_mode = not layer_mode
        self.path_edit.setEnabled(file_mode)
        self.browse_btn.setEnabled(file_mode)
        if layer_mode:
            self.ok_button.setEnabled(
                bool(self._layers) and self.layer_combo.currentData() is not None
            )
        else:
            self.ok_button.setEnabled(bool(self.path_edit.text().strip()))

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择样式文件",
            self.path_edit.text().strip() or "",
            "QGIS 样式 (*.qml *.xml);;所有文件 (*)",
        )
        if path:
            self.path_edit.setText(path)

    def source(self) -> tuple[str, object]:
        """``("layer", QgsVectorLayer)`` 或 ``("file", 路径)``。"""
        if self.layer_radio.isChecked():
            return "layer", self.layer_combo.currentData()
        return "file", self.path_edit.text().strip()

    def accept(self) -> None:
        mode, source = self.source()
        if mode == "file" and not os.path.isfile(str(source or "")):
            return
        if mode == "layer" and source is None:
            return
        super().accept()
