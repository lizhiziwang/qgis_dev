"""自建字段计算器。

替代 QGIS 原生的 ``QgsFieldCalculator``（``qgis.gui``）。之所以不用原生：

原生字段计算器里的「函数编辑器」依赖 ``QgsPythonRunner``，而本应用自建启动
流程没有初始化它（runner 实例在 ``qgis_app.dll`` 里，只有原生 ``QgisApp``
启动时才 ``setInstance``）。实测从 Python 注册 runner 会在 ``evalCommand``
（带 ``QString &`` 输出参数）处直接崩溃（0xC0000409），无法修复。结果就是
原生函数编辑器"添加新文件"会写到盘符根目录、且不即时注册——用户侧的
"保存之后识别不到"。

界面按 QGIS ``qgsfieldcalculatorbase.ui`` 的排布对齐（保留本应用主题）：

- 顶部：「只更新选中要素」复选框；
- 并排两个可勾选分组：「创建新字段」（字段名 / 类型 / 长度 / 精度，可选虚拟字段）
  与「更新已有字段」（字段下拉），互斥；
- 中部：**内嵌** :class:`ExpressionBuilderWidget`（与 QGIS 字段计算器一样，
  表达式编辑 / 字段与函数树 / 函数描述 / 预览都在这里，含「自定义函数…」入口）；
- 底部：「确定 / 取消」。
"""

from __future__ import annotations

from qgis.core import QgsExpression, QgsFeatureRequest, QgsField, QgsVectorLayer
from qgis.PyQt.QtCore import QVariant, Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.expression_builder import ExpressionBuilderWidget

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
    """按表达式新建 / 更新字段（布局对齐 QGIS 字段计算器）。"""

    def __init__(self, layer: QgsVectorLayer, parent=None) -> None:
        super().__init__(parent, f"字段计算器 - {layer.name()}")
        self._layer = layer
        self._result_field: str | None = None
        self._syncing_mode = False
        self.resize(820, 680)

        self._build()
        self._on_new_toggled(True)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QGridLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setHorizontalSpacing(8)
        root.setVerticalSpacing(8)

        # 只更新选中要素
        self.only_selected_check = QCheckBox("只更新选中要素", self)
        root.addWidget(self.only_selected_check, 0, 0, 1, 2)

        # 创建新字段 / 更新已有字段（互斥的可勾选分组）
        self.new_group = self._build_new_group()
        root.addWidget(self.new_group, 1, 0)
        self.update_group = self._build_update_group()
        root.addWidget(self.update_group, 1, 1)

        # 表达式区：内嵌表达式构建器控件（同 QGIS 字段计算器）
        self.builder = ExpressionBuilderWidget(self._layer, self)
        root.addWidget(self.builder, 2, 0, 1, 2)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons, 3, 0, 1, 2)

        root.setRowStretch(2, 1)
        self.setLayout(root)

    def _build_new_group(self) -> QGroupBox:
        group = QGroupBox("创建新字段", self)
        group.setCheckable(True)
        group.setChecked(True)
        group.setFlat(True)
        grid = QGridLayout(group)
        grid.setContentsMargins(3, 9, 3, 0)
        grid.setVerticalSpacing(3)

        self.virtual_check = QCheckBox("创建虚拟字段", group)
        self.virtual_check.setToolTip(
            "虚拟字段每次使用时重算，定义保存在工程文件里，不写入数据源。"
        )
        grid.addWidget(self.virtual_check, 0, 0, 1, 4)

        grid.addWidget(QLabel("输出字段名", group), 1, 0)
        self.name_edit = QLineEdit(group)
        self.name_edit.setPlaceholderText("例：area")
        grid.addWidget(self.name_edit, 1, 1, 1, 3)

        grid.addWidget(QLabel("输出字段类型", group), 2, 0)
        self.type_combo = QComboBox(group)
        for label, qtype, type_name, def_len, def_prec in _TYPE_OPTIONS:
            self.type_combo.addItem(label, (qtype, type_name, def_len, def_prec))
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        grid.addWidget(self.type_combo, 2, 1, 1, 3)

        length_label = QLabel("输出字段长度", group)
        length_label.setToolTip(
            "字段总长度（含小数位数）。例如 123.45 需要长度 5，123456 需要长度 6。"
        )
        grid.addWidget(length_label, 3, 0)
        self.length_spin = QSpinBox(group)
        self.length_spin.setRange(0, 65535)
        self.length_spin.setSpecialValueText("默认")
        grid.addWidget(self.length_spin, 3, 1)

        precision_label = QLabel("精度", group)
        precision_label.setToolTip("小数点后最大位数。例如 123.45 需要精度 2。")
        grid.addWidget(precision_label, 3, 2)
        self.precision_spin = QSpinBox(group)
        self.precision_spin.setRange(0, 20)
        self.precision_spin.setSpecialValueText("默认")
        grid.addWidget(self.precision_spin, 3, 3)

        group.toggled.connect(self._on_new_toggled)
        return group

    def _build_update_group(self) -> QGroupBox:
        group = QGroupBox("更新已有字段", self)
        group.setCheckable(True)
        group.setChecked(False)
        group.setFlat(True)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(3, 9, 3, 0)
        self.update_combo = QComboBox(group)
        for field in self._layer.fields():
            self.update_combo.addItem(field.name())
        layout.addWidget(self.update_combo)
        group.toggled.connect(self._on_update_toggled)
        return group

    # ------------------------------------------------------------- 交互

    def _on_new_toggled(self, checked: bool) -> None:
        if self._syncing_mode:
            return
        self._syncing_mode = True
        try:
            if checked:
                self.update_group.setChecked(False)
        finally:
            self._syncing_mode = False
        if not checked and not self.update_group.isChecked():
            # 两个都取消 → 至少保留一个
            self._set_group_checked("new", True)
            return
        self._set_subtree_enabled(self.new_group, checked)
        self._set_subtree_enabled(self.update_group, not checked)
        self.virtual_check.setEnabled(checked)

    def _on_update_toggled(self, checked: bool) -> None:
        if self._syncing_mode:
            return
        self._syncing_mode = True
        try:
            if checked:
                self.new_group.setChecked(False)
        finally:
            self._syncing_mode = False
        if not checked and not self.new_group.isChecked():
            self._set_group_checked("update", True)
            return
        self._set_subtree_enabled(self.new_group, not checked)
        self._set_subtree_enabled(self.update_group, checked)
        self.virtual_check.setEnabled(not checked)

    def _set_group_checked(self, which: str, checked: bool) -> None:
        """在不触发互斥逻辑的情况下设置勾选（用于兜底）。"""
        self._syncing_mode = True
        try:
            (self.new_group if which == "new" else self.update_group).setChecked(checked)
        finally:
            self._syncing_mode = False

    @staticmethod
    def _set_subtree_enabled(group: QGroupBox, enabled: bool) -> None:
        for widget in group.findChildren(QWidget):
            widget.setEnabled(enabled)

    def _on_type_changed(self, index: int) -> None:
        data = self.type_combo.itemData(index)
        if not data:
            return
        _, _, def_len, def_prec = data
        if self.length_spin.value() == self.length_spin.minimum():
            self.length_spin.setValue(def_len)
        if self.precision_spin.value() == self.precision_spin.minimum():
            self.precision_spin.setValue(def_prec)

    # ------------------------------------------------------------- 应用

    def _apply(self) -> None:
        text = self.builder.expression_text()
        if not text:
            QMessageBox.warning(self, "字段计算器", "表达式不能为空")
            return
        expr = QgsExpression(text)
        if expr.hasParserError():
            QMessageBox.warning(self, "字段计算器", f"表达式有语法错误：\n{expr.parserErrorString()}")
            return

        layer = self._layer
        creating = self.new_group.isChecked()
        only_selected = self.only_selected_check.isChecked()

        if creating:
            name = self.name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "字段计算器", "请填写输出字段名")
                return
            if layer.fields().indexFromName(name) >= 0:
                QMessageBox.warning(self, "字段计算器", f"字段已存在：{name}")
                return
            qtype, type_name, _, _ = self.type_combo.currentData()
            field = QgsField(name, qtype, type_name, self.length_spin.value(), self.precision_spin.value())
            if self.virtual_check.isChecked():
                self._create_virtual_field(expr, field, name)
                return
            target_name = name
        else:
            target_name = self.update_combo.currentText()
            if not target_name:
                QMessageBox.warning(self, "字段计算器", "请选择要更新的字段")
                return
            field = None

        if only_selected and not layer.selectedFeatureIds():
            QMessageBox.warning(self, "字段计算器", "没有选中要素")
            return

        was_editable = layer.isEditable()
        if not was_editable and not layer.startEditing():
            QMessageBox.warning(self, "字段计算器", "无法进入编辑状态")
            return

        if creating:
            if not layer.dataProvider().addAttributes([field]):
                self._fail(was_editable, f"添加字段失败：{target_name}")
                return
            layer.updateFields()

        index = layer.fields().indexFromName(target_name)
        if index < 0:
            self._fail(was_editable, f"找不到字段：{target_name}")
            return

        fids = layer.selectedFeatureIds() if only_selected else None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        updated = 0
        error_msg: str | None = None
        ctx = layer.createExpressionContext()
        try:
            expr.prepare(ctx)
            request = QgsFeatureRequest()
            if fids is not None:
                request.setFilterFids(fids)
            for feature in layer.getFeatures(request):
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

    def _create_virtual_field(self, expr: QgsExpression, field: QgsField, name: str) -> None:
        """创建虚拟字段（定义存工程文件，不写数据源）。"""
        index = self._layer.addExpressionField(expr.expression(), field)
        if index < 0:
            QMessageBox.warning(self, "字段计算器", f"创建虚拟字段失败：{name}")
            return
        self._result_field = name
        logger.info("已创建虚拟字段：图层=%s，字段=%s", self._layer.name(), name)
        QMessageBox.information(self, "字段计算器", f"已创建虚拟字段：{name}")
        self.accept()

    def _fail(self, was_editable: bool, message: str) -> None:
        if not was_editable:
            self._layer.rollBack()
        QMessageBox.warning(self, "字段计算器", message)

    # ------------------------------------------------------------- 输出

    def result_field(self) -> str | None:
        """成功应用的字段名（供调用方刷新列定义）。"""
        return self._result_field
