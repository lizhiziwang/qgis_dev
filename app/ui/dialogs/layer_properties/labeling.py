"""图层属性「标注」页。

仿 QGIS 原生图层属性 → 标注（Labels）：以「标注模式」为总开关，下面按
标注模版 / 内容 / 翻译字段 / 文本 / 文本描边 / 放置 / 渲染 分组。

「标注模版」组从工程（内嵌样式，缺失时退回 QGIS 全局样式库）读取现成的
:class:`QgsPalLayerSettings` 模版；工程里一个都没有时，下拉里只放一个
「无标注」默认项。选中某个模版即把它的设置套进下面的控件（仍需点
「确定 / 应用」才真正写回图层）。

「翻译字段」组（单选）：把所选字段的值翻成别的文本再当标注。

- **使用字典**：暂未实现，固定置灰（字典数据后续从服务端拉取 JSON）。
- **使用转义**：用「设置转义值…」弹窗里配的「值 → 文本」对照表，生成
  ``coalesce(map_get(map(...), to_string(字段)), 字段)`` 表达式，未命中的值
  回退原值。

两种方式互斥，也可都不选（不翻译）；且只有「标注内容」选了真实字段后
才可用，否则置灰。翻译配置存在图层自定义属性 ``label_translate`` 里（JSON），
随工程一同保存。

与「常规 / 字段」页一致的写回策略：**所有修改都推迟到** :meth:`LabelingTab.apply`
（点「确定 / 应用」时），中途点「取消」不落盘。

三种模式：

- **无标注**：``layer.setLabelsEnabled(False)``，但**保留**已有 labeling 配置，
  方便再切回单一标注时恢复（QGIS 的「取消即不生效」语义）。此模式下除
  「标注模版」组外，后面的设置分组整体隐藏（不是灰掉）。
- **单一标注**：用页面上的值重建 :class:`QgsPalLayerSettings`，包成
  :class:`QgsVectorLayerSimpleLabeling` 写回。
- **基于规则标注**：本页不编辑规则，只切换启用状态；规则本身请在 QGIS 规则
  编辑器里维护（本构建没有绑定 ``QgsLabelingWidget``，故本页自建）。

注意本 QGIS 构建（4.2）的 API 与旧版差异：

- ``QgsUnitTypes.RenderUnit`` 的枚举整数值与旧版不同，一律用枚举名而非字面量。
- ``QgsPalLayerSettings.format()`` 返回副本，改完必须 ``setFormat`` 挂回去。
"""

from __future__ import annotations

import json

from qgis.core import (
    QgsPalLayerSettings,
    QgsProject,
    QgsRuleBasedLabeling,
    QgsStyle,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.layer_properties.widgets import ColorButton
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 字段 / 表达式下拉里表示「使用表达式」的哨兵 itemData
_EXPR = "__expr__"

#: 图层自定义属性：标注「翻译字段」配置（JSON，随工程保存）
_TRANSLATE_PROP = "label_translate"

#: 「使用字典」暂未实现：字典数据后续从服务端拉取 JSON
_DICT_TOOLTIP = "字典数据后续从服务端拉取，暂未实现"

#: 「使用转义」拉取字段去重值的上限（大图层防止弹窗卡死）
_VALUE_LIMIT = 2000

#: (显示文本, itemData) —— 标注模式
_MODES = (
    ("无标注", "none"),
    ("单一标注", "single"),
    ("基于规则标注", "rule"),
)

#: 放置方式
_PLACEMENTS = (
    ("围绕点", QgsPalLayerSettings.Placement.AroundPoint),
    ("点的偏移", QgsPalLayerSettings.Placement.OrderedPositionsAroundPoint),
    ("点上方", QgsPalLayerSettings.Placement.OverPoint),
    ("沿线", QgsPalLayerSettings.Placement.Line),
    ("沿曲线", QgsPalLayerSettings.Placement.Curved),
    ("水平", QgsPalLayerSettings.Placement.Horizontal),
    ("自由", QgsPalLayerSettings.Placement.Free),
    ("多边形外侧", QgsPalLayerSettings.Placement.OutsidePolygons),
    ("沿边界曲线", QgsPalLayerSettings.Placement.PerimeterCurved),
)

#: 长度 / 字号单位（枚举名与整数解耦，勿用字面量）
_UNITS = (
    ("毫米", QgsUnitTypes.RenderUnit.Millimeters),
    ("点", QgsUnitTypes.RenderUnit.Points),
    ("像素", QgsUnitTypes.RenderUnit.Pixels),
    ("英寸", QgsUnitTypes.RenderUnit.Inches),
    ("地图单位", QgsUnitTypes.RenderUnit.MapUnits),
    ("百分比", QgsUnitTypes.RenderUnit.Percentage),
)


# --------------------------------------------------------------------- 小工具


def _fill_enum_combo(combo: QComboBox, items) -> None:
    """把 ``(文本, 枚举)`` 列表灌进 combo，itemData 存枚举的整数值。"""
    combo.clear()
    for text, value in items:
        combo.addItem(text, int(value))


def _select_enum(combo: QComboBox, value) -> None:
    """按枚举 / 整数选中 combo 中 itemData 相等的项；找不到保持不动。"""
    if value is None:
        return
    target = int(value)
    for i in range(combo.count()):
        if int(combo.itemData(i)) == target:
            combo.setCurrentIndex(i)
            return


def _fmt_scale(value) -> str:
    """比例尺分母 → 输入框文本（0 / 无效值显示空串，即「不限制」）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    return str(int(num)) if num.is_integer() else f"{num:g}"


def _parse_scale(text: str, field: str) -> float:
    """输入框文本 → 比例尺分母。空串 / 0 表示不限制；容忍用户填 ``1:100000``。"""
    raw = (text or "").strip()
    if not raw:
        return 0.0
    if raw.startswith("1:"):
        raw = raw[2:].strip()
    raw = raw.replace(",", "")
    try:
        num = float(raw)
    except ValueError:
        raise ValueError(f"{field}填写有误：「{text}」不是有效数字") from None
    if num < 0:
        raise ValueError(f"{field}不能为负数")
    return num


def _current_settings(layer) -> QgsPalLayerSettings:
    """取图层当前标注设置作为编辑底稿；无标注 / 规则标注各自兜底。"""
    lab = layer.labeling() if layer is not None else None
    try:
        if isinstance(lab, QgsVectorLayerSimpleLabeling):
            return lab.settings()
        if isinstance(lab, QgsRuleBasedLabeling):
            return lab.rootRule().settings()
    except Exception:  # noqa: BLE001
        logger.warning("读取现有标注设置失败，改用默认值", exc_info=True)
    return QgsPalLayerSettings()


def _mode_of(layer) -> str:
    """当前标注模式：none / single / rule（label 未启用一律视为 none）。"""
    lab = layer.labeling()
    if lab is None or not layer.labelsEnabled():
        return "none"
    if isinstance(lab, QgsRuleBasedLabeling):
        return "rule"
    if isinstance(lab, QgsVectorLayerSimpleLabeling):
        return "single"
    return "none"


def _template_styles() -> list:
    """标注模版的两个来源：工程内嵌样式、QGIS 全局默认样式。

    与 QGIS 自身一致：工程启用内嵌样式（``.qgz`` 里存了样式库）时以工程为准；
    工程没有内嵌样式时就退回用户全局样式库 ``QgsStyle.defaultStyle()``。
    """
    styles = []
    try:
        project_settings = QgsProject.instance().styleSettings()
        project_style = (
            project_settings.projectStyle() if project_settings is not None else None
        )
        if project_style is not None:
            styles.append(project_style)
    except Exception:  # noqa: BLE001
        logger.warning("读取工程样式库失败", exc_info=True)
    try:
        default_style = QgsStyle.defaultStyle()
        if default_style is not None and all(default_style is not s for s in styles):
            styles.append(default_style)
    except Exception:  # noqa: BLE001
        logger.warning("读取全局样式库失败", exc_info=True)
    return styles


def _project_label_templates() -> list[tuple[str, QgsPalLayerSettings]]:
    """工程里可用的标注模版 ``(名称, 设置)``，工程样式优先、按名去重。

    ``QgsStyle.labelSettings(名称)`` 对不存在的名称会返回默认设置而不是空，
    所以这里只遍历 ``labelSettingsNames()`` 里真实存在的名称。
    """
    templates: list[tuple[str, QgsPalLayerSettings]] = []
    seen: set[str] = set()
    for style in _template_styles():
        try:
            names = style.labelSettingsNames()
        except Exception:  # noqa: BLE001
            logger.warning("读取标注模版列表失败", exc_info=True)
            continue
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            try:
                # 复制一份，别把样式库里的对象直接挂到控件上
                templates.append(
                    (name, QgsPalLayerSettings(style.labelSettings(name)))
                )
            except Exception:  # noqa: BLE001
                logger.warning("读取标注模版「%s」失败", name, exc_info=True)
    return templates


def _quote_expr_str(text) -> str:
    """QGIS 表达式里的单引号字符串字面量（内部单引号翻倍转义）。"""
    return "'" + str(text).replace("'", "''") + "'"


def _quote_expr_field(name) -> str:
    """QGIS 表达式里的字段名（双引号，内部双引号翻倍转义）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _build_translate_expression(field: str, mapping=None) -> str:
    """按「值 → 文本」对照表拼标注表达式。

    未命中的键回退原值，如
    ``coalesce(map_get(map('a', 'A'), to_string("f")), "f")``；
    映射为空时直接返回字段本身（等于不翻译）。
    """
    fld = _quote_expr_field(field)
    pairs = [
        f"{_quote_expr_str(k)}, {_quote_expr_str(v)}"
        for k, v in (mapping or {}).items()
        if str(v).strip()
    ]
    if not pairs:
        return fld
    return f"coalesce(map_get(map({', '.join(pairs)}), to_string({fld})), {fld})"


def _read_translate_config(layer) -> dict:
    """读图层上保存的翻译配置；没有 / 解析失败返回空 dict。"""
    if layer is None:
        return {}
    try:
        raw = layer.customProperty(_TRANSLATE_PROP)
    except Exception:  # noqa: BLE001
        logger.warning("读取翻译配置失败", exc_info=True)
        return {}
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("解析翻译配置失败：%r", raw)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _field_values(layer, field: str, limit: int = _VALUE_LIMIT) -> list[str]:
    """字段去重值（字符串化、短值优先排序）；非矢量 / 字段不存在返回空。"""
    if not isinstance(layer, QgsVectorLayer):
        return []
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return []
    try:
        values = layer.uniqueValues(idx, limit)
    except Exception:  # noqa: BLE001
        logger.warning("读取字段「%s」去重值失败", field, exc_info=True)
        return []
    return sorted(
        (str(v) for v in values if v is not None), key=lambda s: (len(s), s)
    )


class _FieldExpressionSelector(QWidget):
    """字段 / 表达式选择器，供「标注内容」使用。

    combo 里列出图层字段，最后一项是「〈表达式…〉」；选中它时旁边出现表达式
    输入框与 ``ε`` 按钮（打开自建的 :class:`ExpressionBuilderDialog`）。
    """

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.combo = QComboBox(self)
        self.combo.setMinimumWidth(160)
        lay.addWidget(self.combo, 1)

        self.expr_edit = QLineEdit(self)
        self.expr_edit.setPlaceholderText("表达式，例如 upper(name)")
        lay.addWidget(self.expr_edit, 1)

        self.expr_btn = QPushButton("ε", self)
        self.expr_btn.setObjectName("RowActionBtn")
        self.expr_btn.setFixedWidth(28)
        self.expr_btn.setToolTip("打开表达式编辑器")
        self.expr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expr_btn.clicked.connect(self._open_builder)
        lay.addWidget(self.expr_btn)

        self.combo.currentIndexChanged.connect(self._sync)
        self._sync()

    # -- 数据 ---------------------------------------------------------

    def set_fields(self, fields) -> None:
        """按 ``QgsFields`` 重填字段项（保留末尾的表达式项）。"""
        self.combo.blockSignals(True)
        self.combo.clear()
        if fields is not None:
            for i in range(fields.size()):
                fld = fields.at(i)
                self.combo.addItem(
                    f"{fld.name()} · {field_alias(fld) or fld.name()}", fld.name()
                )
        self.combo.addItem("〈表达式…〉", _EXPR)
        self.combo.blockSignals(False)
        self._sync()

    def set_value(self, value: str, is_expression: bool) -> None:
        """回显：``is_expression`` 为真走表达式分支，否则按字段名匹配。"""
        value = value or ""
        if is_expression or (value and self.combo.findData(value) < 0):
            idx = self.combo.findData(_EXPR)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
            self.expr_edit.setText(value)
        else:
            idx = self.combo.findData(value) if value else -1
            if idx < 0 and self.combo.count():
                idx = 0
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
            self.expr_edit.clear()
        self._sync()

    def value(self) -> tuple[str, bool]:
        """返回 ``(字段名或表达式, 是否表达式)``。"""
        if self.combo.currentData() == _EXPR:
            return (self.expr_edit.text() or "").strip(), True
        return (self.combo.currentData() or "", False)

    # -- 内部 ---------------------------------------------------------

    def _sync(self) -> None:
        is_expr = self.combo.currentData() == _EXPR
        self.expr_edit.setVisible(is_expr)
        self.expr_btn.setEnabled(bool(self._layer) or is_expr)

    def _open_builder(self) -> None:
        try:
            # 保证用户在 QGIS 函数编辑器里写的 @qgsfunction 自定义函数已注册，
            # 否则表达式构建器里看不到、也用不了它们（启动时已加载，这里兜底）。
            from app.env.user_expressions import load_user_expression_functions

            load_user_expression_functions()

            # 用自建对话框，不依赖 qgis.gui 的 QgsExpressionBuilderDialog。
            from app.ui.dialogs.expression_builder import ExpressionBuilderDialog

            start = self.expr_edit.text() if self.combo.currentData() == _EXPR else ""
            dlg = ExpressionBuilderDialog(self._layer, start, self)
            if dlg.exec():
                idx = self.combo.findData(_EXPR)
                if idx >= 0:
                    self.combo.setCurrentIndex(idx)
                self.expr_edit.setText(dlg.expression_text())
        except Exception as exc:  # noqa: BLE001
            logger.error("打开表达式编辑器失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "标注", f"打开表达式编辑器失败：{exc}")


class _TranslateValueDialog(QDialog):
    """配置字段「原值 → 翻译结果」的转义表。

    左列是字段的去重原值（并上已有映射的键），右列填翻译结果；留空 = 不翻译
    （保留原值）。确定后经 :meth:`mapping` 取回 ``{原值: 翻译}``。
    """

    def __init__(self, field: str, values, mapping=None, parent=None) -> None:
        super().__init__(parent)
        self._field = field
        self.setWindowTitle(f"设置转义值 - {field}")
        self.resize(560, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tip = QLabel(
            f"字段「{field}」的值 → 标注显示文本；翻译结果留空表示不翻译。", self
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["字段值", "翻译结果"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        root.addWidget(self.table, 1)

        tools = QHBoxLayout()
        tools.setSpacing(6)
        add_btn = QPushButton("添加一行", self)
        add_btn.clicked.connect(lambda: self._append_row("", ""))
        del_btn = QPushButton("删除选中行", self)
        del_btn.clicked.connect(self._remove_selected)
        tools.addWidget(add_btn)
        tools.addWidget(del_btn)
        tools.addStretch(1)
        root.addLayout(tools)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._fill(values or (), mapping or {})

    # -- 表格 ---------------------------------------------------------

    def _fill(self, values, mapping) -> None:
        keys = [str(k) for k in mapping]
        for value in values:
            text = str(value)
            if text not in keys:
                keys.append(text)
        for key in keys:
            self._append_row(key, str(mapping.get(key, "")))

    def _append_row(self, value: str, result: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(value))
        self.table.setItem(row, 1, QTableWidgetItem(result))

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def mapping(self) -> dict[str, str]:
        """表格里的映射（原值、翻译结果都非空）。"""
        out: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            val_item = self.table.item(row, 1)
            key = key_item.text().strip() if key_item is not None else ""
            val = val_item.text() if val_item is not None else ""
            if key and val.strip():
                out[key] = val
        return out


class LabelingTab(LayerPropertyTab):
    """图层属性「标注」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        #: 需要随「标注模式」一起启停/显隐的设置分组（不含「标注模版」组）
        self._groups: list[QGroupBox] = []
        #: 「使用转义」的值映射：``{原值: 翻译结果}``
        self._translate_map: dict[str, str] = {}
        self._build()
        if layer is not None:
            self.load(layer)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("标注模式:", self))
        self.mode_combo = QComboBox(self)
        self.mode_combo.setMinimumWidth(150)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(self.mode_combo)
        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("PropHint")
        top.addWidget(self.hint_label, 1)
        root.addLayout(top)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)

        # 「标注模版」组始终可见：它是总入口，选中模版即把设置套进下面的控件
        self._build_template_group(column)
        self._build_content_group(column)
        self._build_translate_group(column)
        self._build_text_group(column)
        self._build_buffer_group(column)
        self._build_placement_group(column)
        self._build_render_group(column)

        column.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _new_form(self, parent) -> QFormLayout:
        form = QFormLayout(parent)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        return form

    def _form_group(self, title: str, layout) -> QFormLayout:
        """建一个带表单布局的分组，登记进 ``_groups``，返回 form 供填行。"""
        box = QGroupBox(title, self)
        form = self._new_form(box)
        layout.addWidget(box)
        self._groups.append(box)
        return form

    def _new_size_spin(self, parent, low=0.0, high=5000.0, value=0.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(parent)
        spin.setRange(low, high)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setValue(value)
        return spin

    def _new_unit_combo(self, parent) -> QComboBox:
        combo = QComboBox(parent)
        _fill_enum_combo(combo, _UNITS)
        return combo

    def _new_opacity_spin(self, parent, value=100) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(0, 100)
        spin.setSuffix(" %")
        spin.setValue(value)
        return spin

    def _build_template_group(self, layout) -> None:
        """「标注模版」组：从工程样式库读现成模版，选中即套用。

        这一组不进 ``_groups``，因此不受「标注模式」显隐控制，始终可见。
        """
        box = QGroupBox("标注模版", self)
        self.template_group = box
        form = self._new_form(box)
        layout.addWidget(box)

        self.template_combo = QComboBox(box)
        self.template_combo.setMinimumWidth(220)
        self.template_combo.setToolTip("选择工程里的标注模版，套用到当前图层")
        self.template_combo.currentIndexChanged.connect(self._on_template_changed)
        form.addRow("模版", self.template_combo)

        self.template_hint = QLabel("", box)
        self.template_hint.setObjectName("PropHint")
        form.addRow("", self.template_hint)

    def _build_content_group(self, layout) -> None:
        form = self._form_group("标注内容", layout)
        self.content_selector = _FieldExpressionSelector(self)
        form.addRow("标注字段", self.content_selector)

    def _build_translate_group(self, layout) -> None:
        """「翻译字段」组：把标注字段的值翻成别的文本。

        两种方式互斥（单选，可都不选 = 不翻译），且只有在「标注内容」选了
        真实字段（非表达式）后才可用，否则置灰。
        """
        form = self._form_group("翻译字段", layout)

        row = QWidget(self)
        rl = QVBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)

        self.translate_dict_radio = QRadioButton("使用字典", row)
        # 暂未实现（后续从服务端拉取字典 JSON），固定置灰
        self.translate_dict_radio.setEnabled(False)
        self.translate_dict_radio.setToolTip(_DICT_TOOLTIP)
        rl.addWidget(self.translate_dict_radio)

        escape_row = QHBoxLayout()
        escape_row.setContentsMargins(0, 0, 0, 0)
        escape_row.setSpacing(6)
        self.translate_escape_radio = QRadioButton("使用转义", row)
        # 关掉 autoExclusive：再点一下可取消（= 不翻译）
        self.translate_escape_radio.setAutoExclusive(False)
        self.translate_escape_radio.setToolTip("按下面配置的「值 → 文本」对照表翻译")
        self.translate_escape_radio.toggled.connect(self._on_translate_escape_toggled)
        escape_row.addWidget(self.translate_escape_radio)
        self.translate_set_btn = QPushButton("设置转义值…", row)
        self.translate_set_btn.setObjectName("RowActionBtn")
        self.translate_set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.translate_set_btn.clicked.connect(self._on_edit_translate_values)
        escape_row.addWidget(self.translate_set_btn)
        escape_row.addStretch(1)
        rl.addLayout(escape_row)

        form.addRow("翻译方式", row)

        self.translate_hint = QLabel("", self)
        self.translate_hint.setObjectName("PropHint")
        self.translate_hint.setWordWrap(True)
        form.addRow("", self.translate_hint)

        # 「标注内容」变化（选字段 / 切表达式）时同步启用态
        self.content_selector.combo.currentIndexChanged.connect(
            self._sync_translate_state
        )
        self.content_selector.expr_edit.textChanged.connect(
            self._sync_translate_state
        )
        self._sync_translate_state()

    def _build_text_group(self, layout) -> None:
        form = self._form_group("文本", layout)

        font_row = QWidget(self)
        fr = QHBoxLayout(font_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(6)
        self.font_combo = QFontComboBox(font_row)
        fr.addWidget(self.font_combo, 1)
        self.font_size_spin = self._new_size_spin(font_row, 0.1, 9999.0, 10.0)
        fr.addWidget(self.font_size_spin)
        self.font_unit_combo = self._new_unit_combo(font_row)
        fr.addWidget(self.font_unit_combo)
        form.addRow("字体", font_row)

        style_row = QWidget(self)
        sr = QHBoxLayout(style_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(8)
        sr.addWidget(QLabel("颜色", style_row))
        self.text_color_btn = ColorButton(QColor(0, 0, 0), style_row)
        sr.addWidget(self.text_color_btn)
        self.bold_check = QCheckBox("加粗", style_row)
        self.italic_check = QCheckBox("斜体", style_row)
        self.underline_check = QCheckBox("下划线", style_row)
        self.strikeout_check = QCheckBox("删除线", style_row)
        for chk in (
            self.bold_check,
            self.italic_check,
            self.underline_check,
            self.strikeout_check,
        ):
            sr.addWidget(chk)
        sr.addStretch(1)
        form.addRow("样式", style_row)

    def _build_buffer_group(self, layout) -> None:
        form = self._form_group("文本描边", layout)
        self.buffer_check = QCheckBox("绘制文本描边", self)
        form.addRow("", self.buffer_check)

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.buffer_size_spin = self._new_size_spin(row, 0.0, 100.0, 1.0)
        rl.addWidget(self.buffer_size_spin)
        self.buffer_unit_combo = self._new_unit_combo(row)
        rl.addWidget(self.buffer_unit_combo)
        rl.addWidget(QLabel("颜色", row))
        self.buffer_color_btn = ColorButton(QColor(255, 255, 255), row)
        rl.addWidget(self.buffer_color_btn)
        rl.addWidget(QLabel("不透明度", row))
        self.buffer_opacity_spin = self._new_opacity_spin(row)
        rl.addWidget(self.buffer_opacity_spin)
        rl.addStretch(1)
        form.addRow("大小", row)

    def _build_placement_group(self, layout) -> None:
        form = self._form_group("放置", layout)
        self.placement_combo = QComboBox(self)
        _fill_enum_combo(self.placement_combo, _PLACEMENTS)
        form.addRow("放置方式", self.placement_combo)

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.dist_spin = self._new_size_spin(row, 0.0, 100000.0, 0.0)
        rl.addWidget(self.dist_spin)
        self.dist_unit_combo = self._new_unit_combo(row)
        rl.addWidget(self.dist_unit_combo)
        rl.addStretch(1)
        form.addRow("距离", row)

    def _build_render_group(self, layout) -> None:
        form = self._form_group("渲染", layout)
        self.scale_check = QCheckBox("按比例尺显示标注", self)
        self.scale_check.toggled.connect(self._on_scale_toggled)
        form.addRow("", self.scale_check)

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(QLabel("最小 1:", row))
        self.min_scale_edit = QLineEdit(row)
        self.min_scale_edit.setPlaceholderText("0")
        rl.addWidget(self.min_scale_edit)
        rl.addWidget(QLabel("最大 1:", row))
        self.max_scale_edit = QLineEdit(row)
        self.max_scale_edit.setPlaceholderText("0")
        rl.addWidget(self.max_scale_edit)
        form.addRow("比例尺范围", row)

        self.display_all_check = QCheckBox("显示所有标注（含冲突）", self)
        form.addRow("", self.display_all_check)
        self.obstacle_check = QCheckBox("要素作为标注障碍物", self)
        form.addRow("", self.obstacle_check)

        self.zindex_spin = QDoubleSpinBox(self)
        self.zindex_spin.setRange(0.0, 9999.0)
        self.zindex_spin.setDecimals(1)
        form.addRow("Z 指数", self.zindex_spin)

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从 ``layer`` 重填控件。"""
        self._layer = layer
        if not isinstance(layer, QgsVectorLayer):
            self.mode_combo.blockSignals(True)
            self.mode_combo.clear()
            self.mode_combo.blockSignals(False)
            self.mode_combo.setEnabled(False)
            self.hint_label.setText("仅矢量图层支持标注")
            self.template_group.setVisible(False)
            self._apply_mode_state("none")
            return

        self.template_group.setVisible(True)
        self._populate_templates()
        self.mode_combo.setEnabled(True)
        self.content_selector._layer = layer
        self.content_selector.set_fields(layer.fields())

        lab = layer.labeling()
        mode = _mode_of(layer)
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for text, data in _MODES:
            if data == "rule" and not isinstance(lab, QgsRuleBasedLabeling):
                continue
            self.mode_combo.addItem(text, data)
        idx = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.mode_combo.blockSignals(False)

        self.hint_label.setText(
            "基于规则标注的规则请在 QGIS 规则编辑器中维护" if mode == "rule" else ""
        )
        self._load_settings(_current_settings(layer))
        self._load_translate(layer)
        self._on_mode_changed()

    def _populate_templates(self) -> None:
        """按工程样式库重填模版下拉；一个都没有就只放默认的「无标注」。"""
        self.template_combo.blockSignals(True)
        try:
            self.template_combo.clear()
            templates = _project_label_templates()
            for name, settings in templates:
                self.template_combo.addItem(name, settings)
            if templates:
                # 不预选：图层当前设置未必对应某个具名模版，空着即「自定义」
                self.template_combo.setCurrentIndex(-1)
                self.template_hint.setText("")
            else:
                self.template_combo.addItem("无标注", None)
                self.template_combo.setCurrentIndex(0)
                self.template_hint.setText("工程中没有标注模版，默认无标注")
        finally:
            self.template_combo.blockSignals(False)

    def _load_settings(self, settings: QgsPalLayerSettings) -> None:
        self.content_selector.set_value(settings.fieldName, settings.isExpression)
        self._load_format(settings.format())
        self._load_placement(settings)
        self._load_render(settings)

    def _load_format(self, tf) -> None:
        font = tf.font()
        self.font_combo.setCurrentFont(QFont(font.family()))
        self.bold_check.setChecked(font.bold())
        self.italic_check.setChecked(font.italic())
        self.underline_check.setChecked(font.underline())
        self.strikeout_check.setChecked(font.strikeOut())
        self.font_size_spin.setValue(tf.size() or 10.0)
        _select_enum(self.font_unit_combo, tf.sizeUnit())
        self.text_color_btn.set_color(tf.color())

        buf = tf.buffer()
        self.buffer_check.setChecked(buf.enabled())
        self.buffer_size_spin.setValue(buf.size())
        _select_enum(self.buffer_unit_combo, buf.sizeUnit())
        self.buffer_color_btn.set_color(buf.color())
        self.buffer_opacity_spin.setValue(round(buf.opacity() * 100))

    def _load_placement(self, settings: QgsPalLayerSettings) -> None:
        _select_enum(self.placement_combo, settings.placement)
        self.dist_spin.setValue(settings.dist)
        _select_enum(self.dist_unit_combo, settings.distUnits)

    def _load_render(self, settings: QgsPalLayerSettings) -> None:
        self.scale_check.setChecked(bool(settings.scaleVisibility))
        self.min_scale_edit.setText(_fmt_scale(settings.maximumScale))
        self.max_scale_edit.setText(_fmt_scale(settings.minimumScale))
        self._on_scale_toggled(self.scale_check.isChecked())
        self.display_all_check.setChecked(bool(settings.displayAll))
        self.obstacle_check.setChecked(bool(settings.obstacle))
        self.zindex_spin.setValue(settings.zIndex)

    def _load_translate(self, layer) -> None:
        """从图层自定义属性恢复「翻译字段」配置（无则清空）。"""
        cfg = _read_translate_config(layer)
        self._translate_map = {}
        self._set_translate_radios(None)
        if cfg.get("field"):
            raw_map = cfg.get("map")
            if isinstance(raw_map, dict):
                self._translate_map = {str(k): str(v) for k, v in raw_map.items()}
            # 标注内容里回显原始字段：settings.fieldName 存的是翻译后的表达式
            self.content_selector.set_value(str(cfg["field"]), False)
            if cfg.get("mode") == "escape":
                self._set_translate_radios("escape")
        self._sync_translate_state()

    # ------------------------------------------------------------- 交互

    def _on_mode_changed(self, *_) -> None:
        self._apply_mode_state(self.mode_combo.currentData())

    def _apply_mode_state(self, mode: str) -> None:
        """无标注隐藏所有设置分组；单一标注可编辑；规则标注可见但只读。"""
        show = mode != "none"
        editable = mode == "single"
        for box in self._groups:
            box.setVisible(show)
            box.setEnabled(editable)

    def _on_template_changed(self, *_) -> None:
        """选中模版 → 把它的设置套进控件（仍需点确定/应用才写回图层）。"""
        settings = self.template_combo.currentData()
        if settings is None:
            self._select_mode("none")
            return
        try:
            self._load_settings(QgsPalLayerSettings(settings))
        except Exception:  # noqa: BLE001
            logger.warning("套用标注模版失败", exc_info=True)
            return
        self._sync_translate_state()
        self._select_mode("single")

    def _select_mode(self, mode: str) -> None:
        idx = self.mode_combo.findData(mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        self._apply_mode_state(self.mode_combo.currentData())

    def _on_scale_toggled(self, checked: bool) -> None:
        self.min_scale_edit.setEnabled(checked)
        self.max_scale_edit.setEnabled(checked)

    # -- 翻译字段 -----------------------------------------------------

    def _translate_mode(self) -> str:
        """当前翻译方式：``escape`` / ``""``（不翻译）。字典暂未实现。"""
        field, is_expr = self.content_selector.value()
        if not field or is_expr:
            return ""
        return "escape" if self.translate_escape_radio.isChecked() else ""

    def _set_translate_radios(self, mode) -> None:
        """静默设置「使用转义」单选（``None`` = 不选，即不翻译）。"""
        self.translate_escape_radio.blockSignals(True)
        self.translate_escape_radio.setChecked(mode == "escape")
        self.translate_escape_radio.blockSignals(False)

    def _on_translate_escape_toggled(self, checked: bool) -> None:
        self._sync_translate_state()

    def _sync_translate_state(self, *_) -> None:
        """按「有没有选字段」与当前选项，刷新启用态与提示。"""
        field, is_expr = self.content_selector.value()
        has_field = bool(field) and not is_expr
        # 「使用字典」暂未实现，始终禁用
        self.translate_dict_radio.setEnabled(False)
        self.translate_escape_radio.setEnabled(has_field)
        if not has_field and self.translate_escape_radio.isChecked():
            self._set_translate_radios(None)
        self.translate_set_btn.setEnabled(
            has_field and self.translate_escape_radio.isChecked()
        )
        if not has_field:
            self.translate_hint.setText("先在「标注内容」里选一个字段")
        elif self.translate_escape_radio.isChecked():
            count = len([v for v in self._translate_map.values() if str(v).strip()])
            self.translate_hint.setText(
                f"已配置 {count} 条转义值" if count else "尚未配置转义值"
            )
        else:
            self.translate_hint.setText("")

    def _on_edit_translate_values(self) -> None:
        field, is_expr = self.content_selector.value()
        if not field or is_expr:
            return
        values = _field_values(self._layer, field)
        dlg = _TranslateValueDialog(field, values, self._translate_map, self)
        if dlg.exec():
            self._translate_map = dlg.mapping()
            self._sync_translate_state()

    def _effective_content(self) -> tuple[str, bool]:
        """标注内容最终写入 ``settings.fieldName`` 的 ``(表达式/字段, 是否表达式)``。"""
        field, is_expr = self.content_selector.value()
        mode = self._translate_mode()
        if not mode or is_expr or not field:
            return field, is_expr
        return _build_translate_expression(field, self._translate_map), True

    def _save_translate_config(self, layer) -> None:
        """把翻译配置写进图层自定义属性（随工程保存）。"""
        mode = self._translate_mode()
        field, _ = self.content_selector.value()
        if not mode or not field:
            try:
                layer.removeCustomProperty(_TRANSLATE_PROP)
            except Exception:  # noqa: BLE001
                logger.warning("清除翻译配置失败", exc_info=True)
            return
        cfg: dict[str, object] = {"mode": mode, "field": field}
        cfg["map"] = {
            k: v for k, v in self._translate_map.items() if str(v).strip()
        }
        try:
            layer.setCustomProperty(
                _TRANSLATE_PROP, json.dumps(cfg, ensure_ascii=False)
            )
        except Exception:  # noqa: BLE001
            logger.warning("保存翻译配置失败", exc_info=True)

    # ------------------------------------------------------------- 写回

    def apply(self, layer) -> bool:
        """把控件值写回图层。三种模式见模块 docstring。"""
        if not isinstance(layer, QgsVectorLayer):
            return True
        mode = self.mode_combo.currentData()
        try:
            if mode == "none":
                # 只关启用，保留已有 labeling 配置（切回单一标注能恢复）
                layer.setLabelsEnabled(False)
                self._save_translate_config(layer)
                return True
            if mode == "rule":
                # 规则不在本页编辑，只确保启用
                layer.setLabelsEnabled(True)
                return True

            settings = QgsPalLayerSettings(_current_settings(layer))
            if not self._read_controls_into(settings):
                return False
            settings.setFormat(self._read_format(settings.format()))
            layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            layer.setLabelsEnabled(True)
            self._save_translate_config(layer)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("应用标注属性失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用标注属性失败：{exc}")
            return False

    def _read_controls_into(self, settings: QgsPalLayerSettings) -> bool:
        name, is_expr = self._effective_content()
        if not name:
            QMessageBox.warning(self, "标注", "标注字段 / 表达式不能为空")
            self.content_selector.combo.setFocus()
            return False
        settings.fieldName = name
        settings.isExpression = is_expr

        settings.placement = QgsPalLayerSettings.Placement(
            int(self.placement_combo.currentData())
        )
        settings.dist = self.dist_spin.value()
        settings.distUnits = QgsUnitTypes.RenderUnit(
            int(self.dist_unit_combo.currentData())
        )

        try:
            max_scale = _parse_scale(self.min_scale_edit.text(), "最小比例尺")
            min_scale = _parse_scale(self.max_scale_edit.text(), "最大比例尺")
        except ValueError as exc:
            QMessageBox.warning(self, "标注", str(exc))
            return False
        use_scale = self.scale_check.isChecked()
        if use_scale and max_scale > 0 and min_scale > 0 and max_scale >= min_scale:
            QMessageBox.warning(
                self, "标注", "最小比例尺的分母必须小于最大比例尺的分母"
            )
            return False
        settings.scaleVisibility = use_scale
        settings.maximumScale = max_scale
        settings.minimumScale = min_scale

        settings.zIndex = self.zindex_spin.value()
        settings.displayAll = self.display_all_check.isChecked()
        settings.obstacle = self.obstacle_check.isChecked()
        return True

    def _read_format(self, tf):
        font = tf.font()
        font.setFamily(self.font_combo.currentFont().family())
        font.setBold(self.bold_check.isChecked())
        font.setItalic(self.italic_check.isChecked())
        font.setUnderline(self.underline_check.isChecked())
        font.setStrikeOut(self.strikeout_check.isChecked())
        tf.setFont(font)
        tf.setSize(self.font_size_spin.value())
        tf.setSizeUnit(QgsUnitTypes.RenderUnit(int(self.font_unit_combo.currentData())))
        tf.setColor(self.text_color_btn.color())

        buf = tf.buffer()
        buf.setEnabled(self.buffer_check.isChecked())
        buf.setSize(self.buffer_size_spin.value())
        buf.setSizeUnit(
            QgsUnitTypes.RenderUnit(int(self.buffer_unit_combo.currentData()))
        )
        buf.setColor(self.buffer_color_btn.color())
        buf.setOpacity(self.buffer_opacity_spin.value() / 100.0)
        tf.setBuffer(buf)
        return tf
