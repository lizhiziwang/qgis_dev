"""新建要素时的属性编辑 Dialog。

迁自 :class:`component.CustomAttrDialog.CustomAttrDialog`。"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qgis.core import QgsVectorLayer

from app.ui.common.base_dialog import BaseDialog


class CustomAttrDialog(BaseDialog):
    """自定义属性编辑 Dialog。"""

    def __init__(self, layer: QgsVectorLayer, feature=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.feature = feature
        self.attr_values = {}
        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle(f"{self.layer.name()} - 要素属性")
        self.setModal(True)
        # 无边框标题栏占高 34px，固定高度 +TITLE_BAR_HEIGHT 保住内容区原尺寸
        self.setFixedSize(500, 550 + self.title_bar.height())

        main = QVBoxLayout()
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(5)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        main.addWidget(scroll)

        form_widget = QWidget()
        scroll.setWidget(form_widget)

        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

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
        button_layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        button_layout.addWidget(bb)
        main.addWidget(button_container)
        self.setLayout(main)

    def _on_focus(self, line_edit, event) -> None:
        if line_edit.text() == "NULL":
            line_edit.clear()
        QLineEdit.focusInEvent(line_edit, event)

    def _save(self) -> None:
        for k, v in self.attr_values.items():
            self.feature.setAttribute(k, v.text())
        self.layer.addFeature(self.feature)
        self.close()

