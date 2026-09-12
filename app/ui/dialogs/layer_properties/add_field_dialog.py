"""添加字段小对话框：图层属性「字段」页用。

仿 QGIS 原生 vector layer properties → Fields → 工具栏的「新建字段」按钮
行为，但本仓库走自建 chrome（:class:`app.ui.common.base_dialog.BaseDialog`），
不调 QGIS 原生的 ``QgsNewVectorLayerDialog`` / ``QgsFieldCalculator``。

字段类型走 QVariant.Type 枚举，类型切换时自动给「长度 / 小数位数」填默认
值（用户没填的话也按默认走）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from app.ui.common.base_dialog import BaseDialog


#: (中文标签, QVariant.Type, typeName, 默认长度, 默认小数位数)
_TYPE_OPTIONS = (
    ("文本", QVariant.String, "string", 50, 0),
    ("整型", QVariant.Int, "integer", 0, 0),
    ("双精度", QVariant.Double, "double", 0, 2),
    ("长整型", QVariant.LongLong, "integer64", 0, 0),
    ("日期", QVariant.Date, "date", 0, 0),
    ("日期时间", QVariant.DateTime, "datetime", 0, 0),
    ("布尔", QVariant.Bool, "boolean", 0, 0),
)


class AddFieldDialog(BaseDialog):
    """新建字段输入表单。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, "添加字段")
        self.setMinimumSize(0, 0)
        self.resize(360, 220)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("英文名（必填，例：area）")
        form.addRow("字段名", self.name_edit)

        self.alias_edit = QLineEdit()
        self.alias_edit.setPlaceholderText("中文显示（可选）")
        form.addRow("别名", self.alias_edit)

        self.type_combo = QComboBox()
        # itemData 存的是 (QVariant.Type, type_name, def_len, def_prec)
        for label, qtype, type_name, def_len, def_prec in _TYPE_OPTIONS:
            self.type_combo.addItem(label, (qtype, type_name, def_len, def_prec))
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("字段类型", self.type_combo)

        self.length_spin = QSpinBox()
        self.length_spin.setRange(0, 65535)
        self.length_spin.setSpecialValueText("默认")
        form.addRow("长度", self.length_spin)

        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(0, 20)
        self.precision_spin.setSpecialValueText("默认")
        form.addRow("小数位数", self.precision_spin)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(bb)
        self.setLayout(layout)

        # 初始化默认值（用第一个选项 = 文本）
        self._on_type_changed(0)
        self.name_edit.setFocus()

    # ----------------------------------------------------------- 交互

    def _on_type_changed(self, index: int) -> None:
        """类型切换时刷新长度 / 小数位数的默认值（未填时才覆盖）。"""
        data = self.type_combo.itemData(index)
        if not data:
            return
        _, _, def_len, def_prec = data
        # 当前值是「默认」（== 最小值）才覆盖；用户已经手动改过的不动
        if self.length_spin.value() == self.length_spin.minimum():
            self.length_spin.setValue(def_len)
        if self.precision_spin.value() == self.precision_spin.minimum():
            self.precision_spin.setValue(def_prec)

    # ----------------------------------------------------------- 输出

    def field_spec(self) -> dict | None:
        """用户点「确定」后取结果；校验不过 / 取消 → 返回 None。

        返回字典 ``{"name", "alias", "qtype", "type_name", "length", "precision"}``，
        :class:`FieldsTab` 直接拿这个字典构 ``QgsField`` 并 ``addAttributes``。
        """
        name = self.name_edit.text().strip()
        if not name:
            return None
        alias = self.alias_edit.text().strip()
        qtype, type_name, _, _ = self.type_combo.currentData()
        length = self.length_spin.value()
        precision = self.precision_spin.value()
        return {
            "name": name,
            "alias": alias,
            "qtype": qtype,
            "type_name": type_name,
            "length": length,
            "precision": precision,
        }