"""自建字段计算器。

替代 QGIS 原生的 ``QgsFieldCalculator``（``qgis.gui``）。之所以不用原生：

原生字段计算器里的「函数编辑器」依赖 ``QgsPythonRunner``，而本应用自建启动
流程没有初始化它（runner 实例在 ``qgis_app.dll`` 里，只有原生 ``QgisApp``
启动时才 ``setInstance``）。实测从 Python 注册 runner 会在 ``evalCommand``
（带 ``QString &`` 输出参数）处直接崩溃（0xC0000409），无法修复。结果就是
原生函数编辑器"添加新文件"会写到盘符根目录、且不即时注册——用户侧的
"保存之后识别不到"。

这个对话框用自建的 :class:`ExpressionBuilderDialog`（内含「自定义函数…」
入口）来选表达式，绕开原生组件，功能对齐原生：新建字段 / 更新已有字段。
"""

from __future__ import annotations

from qgis.core import QgsExpression, QgsField, QgsVectorLayer
from qgis.PyQt.QtCore import QVariant, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog

logger = get_logger(__name__)

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


class FieldCalculatorDialog(BaseDialog):
    """按表达式新建 / 更新字段。"""

    def __init__(self, layer: QgsVectorLayer, parent=None) -> None:
        super().__init__(parent, f"字段计算器 - {layer.name()}")
        self._layer = layer
        self._result_field: str | None = None
        self.resize(720, 480)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._update_preview)

        self._build()
        self._on_mode_changed()
        self._update_preview()

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 模式
        mode_row = QHBoxLayout()
        self.new_radio = QRadioButton("新建字段", self)
        self.update_radio = QRadioButton("更新已有字段", self)
        self.new_radio.setChecked(True)
        self.new_radio.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.new_radio)
        mode_row.addWidget(self.update_radio)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # 新建字段表单
        self.new_form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("新字段名（必填，例：area）")
        self.new_form.addRow("字段名", self.name_edit)

        self.type_combo = QComboBox()
        for label, qtype, type_name, def_len, def_prec in _TYPE_OPTIONS:
            self.type_combo.addItem(label, (qtype, type_name, def_len, def_prec))
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.new_form.addRow("字段类型", self.type_combo)

        self.length_spin = QSpinBox()
        self.length_spin.setRange(0, 65535)
        self.length_spin.setSpecialValueText("默认")
        self.new_form.addRow("长度", self.length_spin)

        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(0, 20)
        self.precision_spin.setSpecialValueText("默认")
        self.new_form.addRow("小数位数", self.precision_spin)
        root.addLayout(self.new_form)

        # 更新字段下拉
        self.update_combo = QComboBox()
        for field in self._layer.fields():
            self.update_combo.addItem(field.name())
        root.addWidget(self.update_combo)

        # 表达式
        expr_row = QHBoxLayout()
        self.expr_edit = QLineEdit()
        self.expr_edit.setPlaceholderText("表达式，或用右侧按钮打开表达式构建器")
        self.expr_edit.textChanged.connect(self._preview_timer.start)
        self.builder_btn = QPushButton("表达式…")
        self.builder_btn.clicked.connect(self._open_builder)
        expr_row.addWidget(QLabel("表达式"))
        expr_row.addWidget(self.expr_edit, 1)
        expr_row.addWidget(self.builder_btn)
        root.addLayout(expr_row)

        # 预览
        self.preview_label = QLabel("", self)
        self.preview_label.setObjectName("PropHint")
        self.preview_label.setWordWrap(True)
        root.addWidget(self.preview_label)
        root.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.setLayout(root)
        self._on_type_changed(self.type_combo.currentIndex())

    # ------------------------------------------------------------- 交互

    def _on_mode_changed(self) -> None:
        creating = self.new_radio.isChecked()
        for i in range(self.new_form.rowCount()):
            item = self.new_form.itemAt(i, QFormLayout.ItemRole.FieldRole)
            label = self.new_form.itemAt(i, QFormLayout.ItemRole.LabelRole)
            if item is not None and item.widget() is not None:
                item.widget().setVisible(creating)
            if label is not None and label.widget() is not None:
                label.widget().setVisible(creating)
        self.update_combo.setVisible(not creating)
        self.name_edit.setFocus()

    def _on_type_changed(self, index: int) -> None:
        data = self.type_combo.itemData(index)
        if not data:
            return
        _, _, def_len, def_prec = data
        if self.length_spin.value() == self.length_spin.minimum():
            self.length_spin.setValue(def_len)
        if self.precision_spin.value() == self.precision_spin.minimum():
            self.precision_spin.setValue(def_prec)

    def _open_builder(self) -> None:
        try:
            from app.ui.dialogs.expression_builder import ExpressionBuilderDialog

            dlg = ExpressionBuilderDialog(self._layer, self.expr_edit.text().strip(), self)
            if dlg.exec():
                self.expr_edit.setText(dlg.expression_text())
        except Exception as exc:  # noqa: BLE001
            logger.error("打开表达式构建器失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "字段计算器", f"打开表达式构建器失败：{exc}")

    def _first_feature(self):
        try:
            return next(self._layer.getFeatures(), None)
        except Exception:  # noqa: BLE001
            return None

    def _update_preview(self) -> None:
        text = self.expr_edit.text().strip()
        if not text:
            self._show_preview("", error=False)
            return
        expr = QgsExpression(text)
        if expr.hasParserError():
            self._show_preview(f"语法错误：{expr.parserErrorString()}", error=True)
            return
        feature = self._first_feature()
        if feature is None:
            self._show_preview("（无要素，无法预览）", error=False)
            return
        ctx = self._layer.createExpressionContext()
        ctx.setFeature(feature)
        try:
            expr.prepare(ctx)
            if expr.hasEvalError():
                self._show_preview(f"无法求值：{expr.evalErrorString()}", error=True)
                return
            value = expr.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001
            self._show_preview(f"无法求值：{exc}", error=True)
            return
        if expr.hasEvalError():
            self._show_preview(f"无法求值：{expr.evalErrorString()}", error=True)
            return
        self._show_preview(f"首行预览：{value}", error=False)

    def _show_preview(self, text: str, error: bool) -> None:
        self.preview_label.setText(text)
        self.preview_label.setStyleSheet("color: #c0392b;" if error else "")

    # ------------------------------------------------------------- 应用

    def _apply(self) -> None:
        text = self.expr_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "字段计算器", "表达式不能为空")
            return
        expr = QgsExpression(text)
        if expr.hasParserError():
            QMessageBox.warning(self, "字段计算器", f"表达式有语法错误：\n{expr.parserErrorString()}")
            return

        layer = self._layer
        creating = self.new_radio.isChecked()
        if creating:
            name = self.name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "字段计算器", "请填写新字段名")
                return
            if layer.fields().indexFromName(name) >= 0:
                QMessageBox.warning(self, "字段计算器", f"字段已存在：{name}")
                return
            qtype, type_name, _, _ = self.type_combo.currentData()
            field = QgsField(name, qtype, type_name, self.length_spin.value(), self.precision_spin.value())
            target_name = name
        else:
            target_name = self.update_combo.currentText()
            if not target_name:
                QMessageBox.warning(self, "字段计算器", "请选择要更新的字段")
                return

        was_editable = layer.isEditable()
        if not was_editable and not layer.startEditing():
            QMessageBox.warning(self, "字段计算器", "无法进入编辑状态")
            return

        if creating:
            if not layer.dataProvider().addAttributes([field]):
                self._fail(was_editable, f"添加字段失败：{name}")
                return
            layer.updateFields()

        index = layer.fields().indexFromName(target_name)
        if index < 0:
            self._fail(was_editable, f"找不到字段：{target_name}")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        updated = 0
        error_msg: str | None = None
        ctx = layer.createExpressionContext()
        try:
            expr.prepare(ctx)
            for feature in layer.getFeatures():
                ctx.setFeature(feature)
                value = expr.evaluate(ctx)
                if expr.hasEvalError():
                    error_msg = expr.evalErrorString()
                    break
                if layer.changeAttributeValue(feature.id(), index, value):
                    updated += 1
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)
        finally:
            QApplication.restoreOverrideCursor()

        if error_msg is not None:
            self._fail(was_editable, f"计算中断（已处理 {updated} 个）：\n{error_msg}")
            return

        self._result_field = target_name
        logger.info("字段计算完成：图层=%s，字段=%s，更新 %d 个要素", layer.name(), target_name, updated)
        QMessageBox.information(self, "字段计算器", f"已更新 {updated} 个要素（字段：{target_name}）")
        self.accept()

    def _fail(self, was_editable: bool, message: str) -> None:
        if not was_editable:
            self._layer.rollBack()
        QMessageBox.warning(self, "字段计算器", message)

    # ------------------------------------------------------------- 输出

    def result_field(self) -> str | None:
        """成功应用的字段名（供调用方刷新列定义）。"""
        return self._result_field
