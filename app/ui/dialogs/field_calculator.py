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

from qgis.core import (
    QgsExpression,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsVectorLayer,
)
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
        # 图层已有选中要素 → 默认勾上并标出数量；没有选中 → 禁用（避免了点确定才
        # 报"没有选中要素"）。清空选择后依旧可改，这里只在打开时定一次初值。
        selected_count = self._layer.selectedFeatureCount()
        if selected_count > 0:
            self.only_selected_check.setChecked(True)
            self.only_selected_check.setText(f"只更新选中要素（已选中 {selected_count} 个）")
        else:
            self.only_selected_check.setEnabled(False)
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

        # 表达式无效（空 / 语法错误 / 字段不存在 / 类型不匹配）时禁用「确定」。
        # 构建器在构造时已算过一次有效性（那时还没连上），故显式设初值。
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.builder.validation_changed.connect(self.ok_button.setEnabled)
        self.ok_button.setEnabled(self.builder.is_expression_valid())

        root.setRowStretch(2, 1)
        self.setLayout(root)

    def _build_new_group(self) -> QGroupBox:
        group = QGroupBox("创建新字段", self)
        group.setObjectName("FieldCalcGroup")
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
        group.setObjectName("FieldCalcGroup")
        group.setCheckable(True)
        group.setChecked(False)
        group.setFlat(True)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(3, 9, 3, 0)
        self.update_combo = QComboBox(group)
        for field in self._layer.fields():
            # 字段前带类型图标（对齐 QGIS 字段计算器）：QgsFields.iconForFieldType
            # 按 QVariant 类型 / 子类型 / 类型名给出与 QGIS 同款图标。
            icon = QgsFields.iconForFieldType(field.type(), field.subType(), field.typeName())
            self.update_combo.addItem(icon, field.name())
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

        layer = self._layer
        creating = self.new_group.isChecked()
        only_selected = self.only_selected_check.isChecked()
        # 虚拟字段是全图层重算，与「只更新选中要素」无关
        virtual = creating and self.virtual_check.isChecked()

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
            target_name = name
        else:
            target_name = self.update_combo.currentText()
            if not target_name:
                QMessageBox.warning(self, "字段计算器", "请选择要更新的字段")
                return
            field = None

        # 目标字段（用于把表达式结果按类型校验后再写）：新建用待建字段，更新取已有字段
        target_field = field if creating else layer.fields().field(target_name)

        fids = layer.selectedFeatureIds() if (only_selected and not virtual) else None
        if only_selected and not virtual and not fids:
            QMessageBox.warning(self, "字段计算器", "没有选中要素")
            return

        # ---- 写操作之前先校验表达式（语法 / 字段是否存在 / 类型是否匹配）----
        # 只查 hasParserError 不够：引用不存在的字段时 prepare 会报错、但 evaluate
        # 会把该错误清掉，于是旧代码一路"成功"。这里在开编辑 / 加字段 / 改值之前就
        # 拦下来（与表达式构建器的预览同一套判定）。
        error = self._validate_expression(expr, fids, target_field)
        if error:
            QMessageBox.warning(self, "字段计算器", f"表达式无效：\n{error}")
            return

        if virtual:
            self._create_virtual_field(expr, field, name)
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
                type_err = self._value_type_error(target_field, value)
                if type_err:
                    error_msg = type_err
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

    def _validate_expression(self, expr: QgsExpression, fids, target_field=None) -> str | None:
        """写操作前的表达式校验；返回错误文本（``None`` = 通过）。

        判定顺序（与 :class:`ExpressionBuilderWidget` 的预览一致）：

        1. ``hasParserError`` —— 语法错误 / 未知函数；
        2. ``prepare`` 后 ``hasEvalError`` —— 例如引用了**不存在的字段**。这步不能
           省：该错误会被随后的 ``evaluate`` 清掉，只在 prepare 之后可见；
        3. 在目标集合的**第一条要素**上试算一次 —— 提前暴露字段类型不匹配等运行期
           错误，避免"字段已加/已改一半才报错"；
        4. 试算结果与 ``target_field`` 的类型是否兼容（字符串表达式不能写数值字段）。
        """
        if expr.hasParserError():
            return expr.parserErrorString().strip()
        layer = self._layer
        ctx = layer.createExpressionContext()
        try:
            expr.prepare(ctx)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        if expr.hasEvalError():
            return expr.evalErrorString().strip()
        request = QgsFeatureRequest()
        if fids is not None:
            request.setFilterFids(fids)
        request.setLimit(1)
        try:
            for feature in layer.getFeatures(request):
                ctx.setFeature(feature)
                value = expr.evaluate(ctx)
                if expr.hasEvalError():
                    return expr.evalErrorString().strip()
                type_err = self._value_type_error(target_field, value)
                if type_err:
                    return type_err
                break
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return None

    @staticmethod
    def _value_type_error(field: QgsField | None, value) -> str | None:
        """表达式结果能否写入 ``field``；不能则返回错误文本（``None`` = 可写）。

        走 :meth:`QgsField.convertCompatible`（与 QGIS 同款判定）：数值字段接受
        数字与数字串（``"12.5"`` 可转），但拒绝非数字串（``"abc"``）；文本字段接受
        一切；``None``（NULL）恒可写。不兼容时该方法会抛异常，这里据此判定。
        """
        if field is None or value is None:
            return None
        try:
            field.convertCompatible(value)
        except Exception:  # noqa: BLE001 —— 不兼容即抛异常
            return (
                f"字段「{field.name()}」类型为 {field.typeName()}，"
                f"表达式结果 {value!r} 无法写入该类型"
            )
        return None

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
