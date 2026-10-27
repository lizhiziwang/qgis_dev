"""属性表「几何计算」对话框：把周长 / 长度 / 面积写进数值字段。

入口：属性表列头右键 →「几何计算」（在「字段计算器」下面）。界面与 ArcMap 的
Calculate Geometry 对齐：属性（周长 / 面积）、目标字段、计算方法、单位、小数
位数、只计算选中要素。

- **计算方法 / 单位默认取「编辑设置」里的默认配置**（长度方法 / 面积方法、
  长度单位 / 面积单位），用户可以在下拉里临时改；
- 量算口径与「自动计算系统长度 / 面积字段」完全一致（高斯投影 / 等积投影 /
  椭球，见 :func:`app.ui.tools.feature_attrs.measure_geometry_value`）；
- 目标字段只列可写的数值字段；点图层没有长度 / 面积可算，调用方不应打开本对话框。
"""

from __future__ import annotations

from qgis.core import QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from app.infra.edit_settings import (
    AREA_METHODS,
    AREA_UNITS,
    LENGTH_METHODS,
    LENGTH_UNITS,
    MU_IN_SQUARE_METERS,
    EditSettingsManager,
    area_backend_for,
    length_backend_for,
)
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.tools.feature_attrs import measure_geometry_value, readonly_field_indices

logger = get_logger(__name__)

#: 可写入几何计算结果的字段类型
_NUMERIC_TYPES = {
    QVariant.Int,
    QVariant.LongLong,
    QVariant.UInt,
    QVariant.ULongLong,
    QVariant.Double,
}

#: 本对话框的面积单位选项（比「编辑设置」多一个基础单位 平方米）
_AREA_UNIT_OPTIONS: tuple[str, ...] = ("平方米", *AREA_UNITS)

#: 上一次点「确定」时用的选项（进程内记忆，**不落盘**）。
#: 「目标字段」不记 —— 它固定跟随列头右键的那一列；其余（属性 / 方法 / 单位 /
#: 小数位 / 只计算选中）下次打开沿用，直到应用退出才回到「编辑设置」的默认值。
_LAST_CHOICES: dict[str, object] = {}


def _to_unit(value: float, *, is_area: bool, unit: str) -> float:
    """米 / 平方米 → 界面所选单位。"""
    if is_area:
        if unit == "亩":
            return value / MU_IN_SQUARE_METERS
        if unit == "公顷":
            return value / 10_000.0
        if unit == "平方千米":
            return value / 1_000_000.0
        return value
    if unit == "千米":
        return value / 1000.0
    if unit == "厘米":
        return value * 100.0
    return value


class GeometryCalculatorDialog(BaseDialog):
    """把周长 / 长度 / 面积批量写进数值字段。"""

    def __init__(
        self,
        layer: QgsVectorLayer,
        parent: QWidget | None = None,
        *,
        field_name: str = "",
    ) -> None:
        super().__init__(parent, "几何计算")
        self.layer = layer
        #: 结果写入的字段：**打开时固定**为列头右键那一列，界面上不可修改
        self.field_name = str(field_name or "").strip()
        #: 成功计算并写入的要素数（取消 / 没变化为 0）
        self.changed_count = 0
        self._build()
        self._load_defaults()
        self._apply_remembered()

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        # 非模态：属性表可能还要滚动 / 选中要素后再回来改参数
        self.setModal(False)
        # BaseDialog 默认最小 800×600；本对话框是小表单，单独收紧
        self.setMinimumSize(420, 290)
        self.resize(460, 330)
        root = QGridLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setHorizontalSpacing(8)
        root.setVerticalSpacing(8)

        self.property_combo = QComboBox()
        # 目标字段固定为右键点击的那一列：只读展示，不给改
        self.target_edit = QLineEdit(self.field_name or "（未指定字段）")
        self.target_edit.setReadOnly(True)
        self.target_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.method_combo = QComboBox()
        self.unit_combo = QComboBox()
        self.decimals_spin = QSpinBox()
        self.decimals_spin.setRange(0, 10)
        self.decimals_spin.setValue(0)

        self._fill_properties()

        for row, (label, widget) in enumerate(
            (
                ("属　性：", self.property_combo),
                ("目标字段：", self.target_edit),
                ("计算方法：", self.method_combo),
                ("单　位：", self.unit_combo),
                ("小数位数：", self.decimals_spin),
            )
        ):
            root.addWidget(QLabel(label), row, 0)
            root.addWidget(widget, row, 1)

        self.only_selected_check = QCheckBox("只计算选中要素")
        selected = self.layer.selectedFeatureCount()
        if selected > 0:
            self.only_selected_check.setChecked(True)
            self.only_selected_check.setText(f"只计算选中要素（已选中 {selected} 个）")
        else:
            self.only_selected_check.setEnabled(False)
        root.addWidget(self.only_selected_check, 5, 0, 1, 2)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons, 6, 0, 1, 2)

        self.property_combo.currentIndexChanged.connect(self._on_property_changed)
        # 属性没有可选项（点图层）：确定直接禁用，说明原因
        if self.property_combo.count() == 0:
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.setLayout(root)

    def _fill_properties(self) -> None:
        """按几何类型给出可算的属性（点图层为空）。"""
        geometry_type = self.layer.geometryType()
        if geometry_type == QgsWkbTypes.PolygonGeometry:
            choices = (("周长", "length"), ("面积", "area"))
        elif geometry_type == QgsWkbTypes.LineGeometry:
            choices = (("长度", "length"),)
        else:
            choices = ()
        for label, kind in choices:
            self.property_combo.addItem(label, kind)
        if self.property_combo.count():
            # 面图层默认「面积」（最常用）；线图层只有长度
            if geometry_type == QgsWkbTypes.PolygonGeometry:
                self.property_combo.setCurrentIndex(
                    self.property_combo.findData("area")
                )

    def _load_defaults(self) -> None:
        """按「编辑设置」里的默认方法 / 单位填充两个下拉。"""
        settings = EditSettingsManager.instance().current()
        is_area = self.property_combo.currentData() == "area"
        methods = AREA_METHODS if is_area else LENGTH_METHODS
        method = settings.area_method if is_area else settings.len_method
        self.method_combo.clear()
        self.method_combo.addItems(methods)
        index = self.method_combo.findText(method)
        if index >= 0:
            self.method_combo.setCurrentIndex(index)
        units = _AREA_UNIT_OPTIONS if is_area else LENGTH_UNITS
        unit = settings.area_unit if is_area else settings.len_unit
        self.unit_combo.clear()
        self.unit_combo.addItems(units)
        index = self.unit_combo.findText(unit)
        if index >= 0:
            self.unit_combo.setCurrentIndex(index)

    def _on_property_changed(self, _index: int) -> None:
        """周长 ↔ 面积：方法 / 单位两列换成对应那套默认值。"""
        self._load_defaults()

    def _apply_remembered(self) -> None:
        """沿用上次「确定」时的选项（进程内记忆；首次打开 / 重启后才用默认值）。"""
        if not _LAST_CHOICES:
            return
        kind = _LAST_CHOICES.get("property")
        if kind:
            index = self.property_combo.findData(kind)
            if index >= 0:
                self.property_combo.setCurrentIndex(index)
        index = self.method_combo.findText(str(_LAST_CHOICES.get("method") or ""))
        if index >= 0:
            self.method_combo.setCurrentIndex(index)
        index = self.unit_combo.findText(str(_LAST_CHOICES.get("unit") or ""))
        if index >= 0:
            self.unit_combo.setCurrentIndex(index)
        decimals = _LAST_CHOICES.get("decimals")
        if isinstance(decimals, int):
            self.decimals_spin.setValue(decimals)
        only_selected = _LAST_CHOICES.get("only_selected")
        if isinstance(only_selected, bool) and self.only_selected_check.isEnabled():
            self.only_selected_check.setChecked(only_selected)

    def _remember_choices(self) -> None:
        """记住本次的选项（字段除外，它固定跟随被右键的列）。"""
        _LAST_CHOICES.update(
            property=self.property_combo.currentData(),
            method=self.method_combo.currentText(),
            unit=self.unit_combo.currentText(),
            decimals=int(self.decimals_spin.value()),
            only_selected=self.only_selected_check.isChecked(),
        )

    # ------------------------------------------------------------- 计算

    def accept(self) -> None:
        """校验 → 计算 → 写入字段；失败留在窗口里。"""
        field_name = str(self.field_name or "").strip()
        field_index = self.layer.fields().indexOf(field_name)
        if not field_name or field_index < 0:
            MessageBox.warning(
                self, "几何计算", "目标字段已不存在，无法写入几何计算结果。"
            )
            return
        field = self.layer.fields().at(field_index)
        if field_index in readonly_field_indices(self.layer.fields()):
            MessageBox.warning(
                self, "几何计算", f"字段「{field_name}」为系统自维护字段，不可写入。"
            )
            return
        if field.type() not in _NUMERIC_TYPES:
            MessageBox.warning(
                self, "几何计算", f"字段「{field_name}」不是数值字段，无法写入。"
            )
            return
        if self.property_combo.count() == 0:
            MessageBox.warning(
                self, "几何计算", "当前图层的几何类型没有长度 / 面积可计算。"
            )
            return

        is_area = self.property_combo.currentData() == "area"
        method = self.method_combo.currentText()
        unit = self.unit_combo.currentText()
        decimals = int(self.decimals_spin.value())
        backend = area_backend_for(method) if is_area else length_backend_for(method)
        # 记住本次选项（即使本次没有值变化 / 后面失败），下次打开沿用
        self._remember_choices()

        if not self.layer.isEditable() and not self.layer.startEditing():
            MessageBox.warning(
                self, "几何计算", "无法进入编辑状态，几何计算结果未写入。"
            )
            return

        integer_field = field.type() in (
            QVariant.Int,
            QVariant.LongLong,
            QVariant.UInt,
            QVariant.ULongLong,
        )

        if self.only_selected_check.isChecked():
            fids = list(self.layer.selectedFeatureIds())
            if not fids:
                MessageBox.warning(self, "几何计算", "当前没有选中的要素。")
                return
            features = (self.layer.getFeature(fid) for fid in fids)
        else:
            features = self.layer.getFeatures()

        updates: list[tuple[int, object]] = []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for feature in features:
                if not feature.hasGeometry():
                    continue
                geometry = feature.geometry()
                if geometry is None or geometry.isNull() or geometry.isEmpty():
                    continue
                meters = measure_geometry_value(
                    self.layer, geometry, is_area=is_area, backend=backend
                )
                if meters is None:
                    continue
                value = _to_unit(meters, is_area=is_area, unit=unit)
                value = round(value, decimals)
                if integer_field:
                    value = int(value)
                try:
                    current = feature.attribute(field_index)
                except Exception:  # noqa: BLE001
                    current = None
                if current == value:
                    continue
                updates.append((int(feature.id()), value))
        finally:
            QApplication.restoreOverrideCursor()

        if not updates:
            MessageBox.information(
                self, "几何计算", "没有需要更新的要素（计算结果与当前值一致）。"
            )
            return
        self.changed_count = len(updates)
        self.layer.beginEditCommand("几何计算")
        try:
            for fid, value in updates:
                self.layer.changeAttributeValue(fid, field_index, value)
        except Exception as exc:  # noqa: BLE001
            logger.error("几何计算写入失败：%s", exc, exc_info=True)
            MessageBox.warning(self, "几何计算", f"写入失败：{exc}")
            return
        finally:
            self.layer.endEditCommand()
        super().accept()
