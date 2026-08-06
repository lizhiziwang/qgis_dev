"""新建要素时的属性编辑 Dialog。

迁自 :class:`component.CustomAttrDialog.CustomAttrDialog`。"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsVectorLayer


class CustomAttrDialog(QDialog):
    """自定义属性编辑 Dialog。"""

    def __init__(self, layer: QgsVectorLayer, feature=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.feature = feature
        self.attr_values = {}
        self._init_style()
        self._init_ui()

    def _init_style(self) -> None:
        self.setStyleSheet("""
            QDialog { background-color: #f0f0f0; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 12px; }
            QLabel { color: #222; padding: 3px 0; min-width: 80px; }
            QLineEdit { padding: 2px 4px; border: 1px solid #b9b9b9; border-radius: 1px; background: #fff; font-size: 12px; color: #333; }
            QLineEdit:focus { border-color: #4d90fe; outline: none; }
        """)

    def _init_ui(self) -> None:
        self.setWindowTitle(f"{self.layer.name()} - 要素属性")
        self.setModal(True)
        self.setFixedSize(500, 550)

        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(5)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        main.addWidget(scroll)

        form_widget = QWidget()
        scroll.setWidget(form_widget)

        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)

        for field in self.layer.fields():
            label = QLabel(field.name())
            label.setMinimumWidth(80)
            line_edit = QLineEdit()
            line_edit.setText("NULL")
            line_edit.focusInEvent = lambda e, le=line_edit: self._on_focus(le, e)
            self.attr_values[field.name()] = line_edit
            form.addRow(label, line_edit)

        button_container = QWidget()
        button_container.setObjectName("button_container")
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(5, 0, 0, 0)
        button_layout.setAlignment(Qt.AlignRight)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal)
        bb.button(QDialogButtonBox.Ok).setDefault(True)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        button_layout.addWidget(bb)
        main.addWidget(button_container)

    def _on_focus(self, line_edit, event) -> None:
        if line_edit.text() == "NULL":
            line_edit.clear()
        QLineEdit.focusInEvent(line_edit, event)

    def _save(self) -> None:
        for k, v in self.attr_values.items():
            self.feature.setAttribute(k, v.text())
        self.layer.addFeature(self.feature)
        self.close()

