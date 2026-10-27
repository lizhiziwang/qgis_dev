"""添加字段小对话框：图层属性「字段」页用。

仿 QGIS 原生 vector layer properties → Fields → 工具栏的「新建字段」按钮
行为，但本仓库走自建 chrome（:class:`app.ui.common.base_dialog.BaseDialog`），
不调 QGIS 原生的 ``QgsNewVectorLayerDialog`` / ``QgsFieldCalculator``。

字段类型走 QVariant.Type 枚举；**类型切换时只显示该类型有意义的行**：
文本只显示「长度」，双精度显示「长度 + 小数位数」，整型 / 长整型 / 日期 /
日期时间 / 布尔两项都隐藏（QGIS 里也不使用它们）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, QTimer, QVariant
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
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

    def __init__(self, parent=None, initial: dict | None = None) -> None:
        """``initial`` 为 None → 新建字段；传 spec → 编辑已有 / pending 字段。"""
        self._initial = initial
        super().__init__(parent, "编辑字段" if initial else "添加字段")
        self.setMinimumSize(0, 0)
        self.resize(360, 220)

        self._form = form = QFormLayout()
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

        self._length_label = QLabel("长度")
        self.length_spin = QSpinBox()
        self.length_spin.setRange(0, 65535)
        self.length_spin.setSpecialValueText("默认")
        form.addRow(self._length_label, self.length_spin)

        self._precision_label = QLabel("小数位数")
        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(0, 20)
        self.precision_spin.setSpecialValueText("默认")
        form.addRow(self._precision_label, self.precision_spin)

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

        # 初始化默认值（用第一个选项 = 文本）；编辑模式则回填 spec
        if self._initial:
            self._load_initial(self._initial)
        else:
            self._on_type_changed(0)
        self._fit_to_content()
        self.name_edit.setFocus()

    def _load_initial(self, spec: dict) -> None:
        """把传入的字段 spec 回填到各控件（编辑模式）。"""
        self.name_edit.setText(spec.get("name") or "")
        self.alias_edit.setText(spec.get("alias") or "")
        target = spec.get("type_name")
        for i in range(self.type_combo.count()):
            data = self.type_combo.itemData(i)
            if data and data[1] == target:
                # 先选类型（避免触发 _on_type_changed 把下面回填的长度/精度覆盖掉）
                self.type_combo.blockSignals(True)
                self.type_combo.setCurrentIndex(i)
                self.type_combo.blockSignals(False)
                break
        self.length_spin.setValue(int(spec.get("length", 0) or 0))
        self.precision_spin.setValue(int(spec.get("precision", 0) or 0))
        # 只刷新可见性；编辑模式保留 spec 里的长度 / 精度，不按类型默认值重置
        data = self.type_combo.currentData()
        if data:
            self._apply_row_visibility(data[0])

    # ----------------------------------------------------------- 交互

    @staticmethod
    def _row_visibility(qtype) -> tuple[bool, bool]:
        """(显示长度, 显示小数位数)。

        只有文本用长度；双精度用长度 + 小数位数；整型 / 长整型 / 日期 /
        日期时间 / 布尔都没有这两项（QGIS 里也不使用它们）。
        """
        return qtype in (QVariant.String, QVariant.Double), qtype == QVariant.Double

    def _apply_row_visibility(self, qtype) -> None:
        """按类型显示 / 隐藏长度与小数位数的标签和输入框。"""
        show_length, show_precision = self._row_visibility(qtype)
        self._length_label.setVisible(show_length)
        self.length_spin.setVisible(show_length)
        self._precision_label.setVisible(show_precision)
        self.precision_spin.setVisible(show_precision)

    def _fit_to_content(self) -> None:
        """隐藏行后把窗口高度收到内容大小（宽度保持不变）。"""
        try:
            self.layout().activate()
            self.resize(self.width(), self.sizeHint().height())
        except Exception:  # noqa: BLE001 —— 纯外观，失败不影响功能
            pass

    def _on_type_changed(self, index: int) -> None:
        """类型切换：只留下该类型有意义的长度 / 小数位数，并回填默认值。"""
        data = self.type_combo.itemData(index)
        if not data:
            return
        qtype, _, def_len, def_prec = data
        show_length, show_precision = self._row_visibility(qtype)
        self._apply_row_visibility(qtype)
        # 不适用的项归零，避免隐藏值悄悄写进字段定义
        self.length_spin.setValue(def_len if show_length else 0)
        self.precision_spin.setValue(def_prec if show_precision else 0)
        if self.isVisible():
            # 隐藏行要等布局下一拍才生效，延后一拍再按新内容收高度
            QTimer.singleShot(0, self._fit_to_content)

    # ----------------------------------------------------------- 输出

    def field_spec(self) -> dict | None:
        """用户点「确定」后取结果；校验不过 / 取消 → 返回 None。

        返回字典 ``{"name", "alias", "qtype", "type_name", "length", "precision"}``，
        :class:`FieldsTab` 直接拿这个字典构 ``QgsField`` 或写回 pending 行。
        """
        name = self.name_edit.text().strip()
        if not name:
            return None
        alias = self.alias_edit.text().strip()
        qtype, type_name, _, _ = self.type_combo.currentData()
        show_length, show_precision = self._row_visibility(qtype)
        length = self.length_spin.value() if show_length else 0
        precision = self.precision_spin.value() if show_precision else 0
        return {
            "name": name,
            "alias": alias,
            "qtype": qtype,
            "type_name": type_name,
            "length": length,
            "precision": precision,
        }