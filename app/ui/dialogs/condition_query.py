"""条件查询 Dialog：按属性条件把命中的要素加入**选择集**。

与 :class:`~app.ui.dialogs.query_builder.QueryBuilderDialog` 的分工：

- 属性表「筛选」/ 导出条件走 ``QueryBuilderDialog`` —— 那个写的是图层的**定义查询**
  （``setSubsetString``），图层本身会真的被过滤掉；
- 本对话框（左侧竖条「条件查询」，界面参照 ``E:\\2026\\project\\code01`` 原型）
  **不改图层筛选**，只把满足条件的要素按「新建 / 增加 / 移除」三种方式写进 QGIS
  的选择集（``selectByExpression``）。

界面元素与原型一致：选择查询图层 / 选择方式、字段信息（字段筛选）、操作按钮、
唯一值（获取唯一值）、条件编辑框，以及底部的查询方案（存储 / 删除 / 清除 /
查询）。「域字段」「显示代码」暂时隐藏（实现保留，后续需要时再放开）。条件语法
按 QGIS 表达式（``QgsExpression``）解析，字段引用统一插成 ``"字段名"``。

面板以**非模态**方式打开（``setModal(False)`` + ``show()``），查询后可以直接回到
图层列表 / 画布继续操作，不必先关闭本面板。

查询方案落在**系统级状态文件**（``<config>/state/condition_query.json``，见
:mod:`app.infra.fs.state`），不跟工程走——与「数据管理」的登记信息同一口径。
每条方案同时记录对应图层的身份（id / 名称 / 数据源）：选用方案时自动把图层
切到该图层；图层不在当前工程里会提示先加载。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qgis.core import QgsExpression, QgsProject, QgsVectorLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.infra.fs.state import load_state, save_state
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.qgis_selection import SELECT_METHODS, selection_behavior
from app.ui.common.value_format import format_value, unwrap
from app.ui.tools.feature_attrs import field_alias

if TYPE_CHECKING:
    from qgis.core import QgsField

logger = get_logger(__name__)

#: 查询方案的状态文件名（系统级，不跟工程走）
_STATE_NAME = "condition_query"

#: 唯一值最多取多少个（防止超大图层把弹窗卡住；列表末尾会提示已截断）
_UNIQUE_LIMIT = 5000

#: 本工程「字典字段」存放的自定义属性前缀（见 create_data/fields.py）
_DICT_TABLE_PROP = "create_data/dict_table/"

#: 条件框下方的常驻提示
_HINT = "查询命中的要素会加入选择集，不会改动图层自身的属性过滤。"

#: 操作按钮：(显示文本, 插入文本, 插入后光标左移几位)
#: ``?`` 在 QGIS 的 LIKE 里是 ``_``（单字符通配），按钮沿用原型的问号文案。
_OPERATORS: tuple[tuple[str, str, int], ...] = (
    ("=", " = ", 0),
    ("<>", " <> ", 0),
    (">", " > ", 0),
    (">=", " >= ", 0),
    ("<", " < ", 0),
    ("<=", " <= ", 0),
    ("in", " IN ", 0),
    ("()", "()", 1),
    ("?", "_", 0),
    ("%", "%", 0),
    ("''", "''", 1),
    ("like", " LIKE ", 0),
    ("and", " AND ", 0),
    ("or", " OR ", 0),
    ("not in", " NOT IN ", 0),
    ("is null", " IS NULL ", 0),
)


@dataclass(frozen=True)
class _FieldEntry:
    """字段列表的一项。"""

    #: 真实字段名（插入表达式 / 取唯一值都用它）
    name: str
    #: 给用户看的文本（别名优先）
    display: str
    #: 域字段的「代码 → 显示名」映射；``None`` = 不是带标签的域字段
    domain_labels: dict[str, str] | None


def _domain_labels(field: "QgsField") -> dict[str, str] | None:
    """取字段的 QGIS ``ValueMap`` 配置：``{代码: 显示名}``；不是域字段返回 ``None``。"""
    try:
        setup = field.editorWidgetSetup()
    except Exception:  # noqa: BLE001 —— 老驱动可能没有 widget 配置
        return None
    if setup.type() != "ValueMap":
        return None
    config = setup.config()
    mapping = config.get("map") if isinstance(config, dict) else None
    if not mapping:
        return None
    if isinstance(mapping, dict):
        return {str(code): str(label) for code, label in mapping.items()}
    labels: dict[str, str] = {}
    for pair in mapping:  # PyQGIS 里通常是 [(code, label), ...]
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            labels[str(pair[0])] = str(pair[1])
    return labels or None


def _has_dict_table(layer: "QgsVectorLayer", field_name: str) -> bool:
    """本工程「创建数据」登记的字典字段（只标记为域字段，标签暂未接入）。"""
    try:
        return bool(layer.customProperty(_DICT_TABLE_PROP + field_name, ""))
    except Exception:  # noqa: BLE001 —— 图层对象已销毁
        return False


def _literal(value: object) -> str:
    """唯一值 → 表达式字面量（字符串加单引号并转义）。"""
    value = unwrap(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _sort_key(value: object) -> tuple[int, float | str]:
    """数值按大小、其余按文本排序；数字整体排在文本前。"""
    value = unwrap(value)
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


class ConditionQueryDialog(BaseDialog):
    """按属性条件选择要素（加入 / 新建 / 移除选择集）。"""

    def __init__(
        self,
        layers: list["QgsVectorLayer"],
        current_layer: "QgsVectorLayer | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("条件查询")
        # 非模态：查询后用户还要回主窗口点图层列表 / 画布，不能挡住输入
        self.setModal(False)
        self.setMinimumSize(700, 620)
        self.resize(780, 700)
        self._layers = list(layers)
        self._current_layer = self._find_layer(current_layer)
        self._field_entries: list[_FieldEntry] = []
        self._active_field = ""
        self._unique_values: list[object] = []
        self._schemes: list[dict[str, Any]] = []
        self._init_ui()
        self._init_signals()
        self._load_schemes()
        self._populate_layers()
        self._refresh_schemes()
        self._on_layer_changed()

    # ------------------------------------------------------------ 装配

    def _init_ui(self) -> None:
        main = QVBoxLayout()
        main.setContentsMargins(10, 8, 10, 8)
        main.setSpacing(8)

        # 查询图层 / 选择方式
        layer_group = QGroupBox()
        layer_grid = QGridLayout(layer_group)
        self.layer_combo = QComboBox()
        self.layer_combo.setMinimumWidth(320)
        self.method_combo = QComboBox()
        for label, action in SELECT_METHODS:
            self.method_combo.addItem(label, action)
        layer_grid.addWidget(QLabel("选择查询图层"), 0, 0)
        layer_grid.addWidget(self.layer_combo, 0, 1)
        layer_grid.addWidget(QLabel("选择方式"), 1, 0)
        layer_grid.addWidget(self.method_combo, 1, 1)
        layer_grid.setColumnStretch(1, 1)
        main.addWidget(layer_group)

        # 字段信息
        field_group = QGroupBox("字段信息")
        field_layout = QVBoxLayout(field_group)
        field_tools = QHBoxLayout()
        self.field_filter_btn = QPushButton("显示字段筛选")
        self.field_filter_btn.setCheckable(True)
        self.field_filter_btn.setToolTip("按关键字筛选下面的字段列表")
        self.domain_check = QCheckBox("域字段")
        self.domain_check.setChecked(True)
        self.domain_check.hide()  # 暂不开放：实现保留，后续需要时去掉 hide()
        field_tools.addWidget(self.field_filter_btn)
        field_tools.addStretch(1)
        field_tools.addWidget(self.domain_check)
        field_layout.addLayout(field_tools)
        self.field_filter_edit = QLineEdit()
        self.field_filter_edit.setPlaceholderText("输入字段名筛选…")
        self.field_filter_edit.hide()
        field_layout.addWidget(self.field_filter_edit)
        self.field_list = QListWidget()
        self.field_list.setMinimumHeight(110)
        field_layout.addWidget(self.field_list)
        main.addWidget(field_group, 1)

        # 操作 / 唯一值
        middle = QHBoxLayout()
        op_group = QGroupBox("操作")
        op_grid = QGridLayout(op_group)
        for index, (label, insert, back) in enumerate(_OPERATORS):
            row, column = divmod(index, 4)
            button = QPushButton(label)
            button.setMinimumHeight(28)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if label == "?":
                button.setToolTip("单字符通配符（QGIS LIKE 里写作 _）")
            button.clicked.connect(
                lambda _checked=False, text=insert, offset=back: self._insert_text(text, offset)
            )
            op_grid.addWidget(button, row, column)
        op_group.setMinimumWidth(340)
        middle.addWidget(op_group, 1)

        unique_group = QGroupBox("唯一值")
        unique_layout = QVBoxLayout(unique_group)
        unique_tools = QHBoxLayout()
        self.show_code_check = QCheckBox("显示代码")
        self.show_code_check.setChecked(False)
        self.show_code_check.setEnabled(False)
        self.show_code_check.hide()  # 暂不开放：实现保留，后续需要时去掉 hide()
        self.get_unique_btn = QPushButton("获取唯一值")
        unique_tools.addWidget(self.show_code_check)
        unique_tools.addWidget(self.get_unique_btn)
        unique_tools.addStretch(1)
        unique_layout.addLayout(unique_tools)
        self.unique_list = QListWidget()
        self.unique_list.setMinimumHeight(110)
        unique_layout.addWidget(self.unique_list)
        middle.addWidget(unique_group, 1)
        main.addLayout(middle, 1)

        # 条件
        condition_group = QGroupBox("条件")
        condition_layout = QVBoxLayout(condition_group)
        self.condition_edit = QTextEdit()
        self.condition_edit.setPlaceholderText('示例："核实完成状态" = \'已核实\'')
        self.condition_edit.setMinimumHeight(90)
        condition_layout.addWidget(self.condition_edit)
        self.result_label = QLabel(_HINT)
        self.result_label.setObjectName("PropHint")
        self.result_label.setWordWrap(True)
        condition_layout.addWidget(self.result_label)
        main.addWidget(condition_group, 1)

        # 底部：查询方案 + 操作按钮
        footer = QHBoxLayout()
        self.scheme_combo = QComboBox()
        self.scheme_combo.setMinimumWidth(180)
        footer.addWidget(self.scheme_combo)
        self.save_btn = QPushButton("存储")
        self.delete_btn = QPushButton("删除")
        self.clear_btn = QPushButton("清除")
        self.query_btn = QPushButton("查询")
        self.query_btn.setObjectName("DialogPrimary")
        self.close_btn = QPushButton("关闭")
        for button in (self.save_btn, self.delete_btn, self.clear_btn, self.query_btn):
            footer.addWidget(button)
        footer.addStretch(1)
        footer.addWidget(self.close_btn)
        main.addLayout(footer)

        self.setLayout(main)

    def _init_signals(self) -> None:
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self.field_filter_btn.toggled.connect(self._on_field_filter_toggled)
        self.field_filter_edit.textChanged.connect(lambda _text: self._refresh_fields())
        self.domain_check.toggled.connect(lambda _checked: self._refresh_fields())
        self.field_list.currentRowChanged.connect(self._on_field_row_changed)
        self.field_list.itemClicked.connect(self._on_field_clicked)
        self.unique_list.itemClicked.connect(self._on_unique_clicked)
        self.get_unique_btn.clicked.connect(self._refresh_unique_values)
        self.show_code_check.toggled.connect(lambda _checked: self._render_unique_values())
        self.scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        self.save_btn.clicked.connect(self._on_save_scheme)
        self.delete_btn.clicked.connect(self._on_delete_scheme)
        self.clear_btn.clicked.connect(self._on_clear)
        self.query_btn.clicked.connect(self._on_query)
        self.close_btn.clicked.connect(self.close)

    def _find_layer(self, layer: "QgsVectorLayer | None") -> "QgsVectorLayer | None":
        if layer is None or sip.isdeleted(layer):
            return None
        for candidate in self._layers:
            if candidate is layer or candidate.id() == layer.id():
                return candidate
        return None

    def _populate_layers(self) -> None:
        """图层下拉：默认选主窗口当前图层（没有就选第一个）。"""
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in self._layers:
            self.layer_combo.addItem(layer.name(), layer)
        target = self._current_layer or (self._layers[0] if self._layers else None)
        if target is not None:
            for index in range(self.layer_combo.count()):
                if self.layer_combo.itemData(index) is target:
                    self.layer_combo.setCurrentIndex(index)
                    break
        self.layer_combo.blockSignals(False)

    def set_layers(
        self,
        layers: list["QgsVectorLayer"],
        current_layer: "QgsVectorLayer | None" = None,
    ) -> None:
        """刷新可查询图层（非模态面板复用同一实例时由主窗口调用）。"""
        self._layers = list(layers)
        target = self._find_layer(current_layer) or self._find_layer(self.current_layer())
        self._current_layer = target or (self._layers[0] if self._layers else None)
        self._populate_layers()
        self._on_layer_changed()

    # ------------------------------------------------------------ 图层 / 字段

    def current_layer(self) -> "QgsVectorLayer | None":
        """当前查询图层（非模态面板会一直开着，图层可能已被用户移除）。"""
        layer = self.layer_combo.currentData()
        if isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer):
            return layer
        return None

    def _on_layer_changed(self, _index: int = -1) -> None:
        """切换图层：重建字段列表 / 唯一值，清掉上一条结果提示。"""
        self._active_field = ""
        self._update_domain_check()
        self._refresh_fields()
        self.result_label.setText(_HINT)

    def _update_domain_check(self) -> None:
        """有域字段时「域字段」勾选才有效（无域字段的图层置灰）。"""
        layer = self.current_layer()
        has_domain = False
        if layer is not None:
            has_domain = any(
                _domain_labels(field) is not None or _has_dict_table(layer, field.name())
                for field in layer.fields()
            )
        self.domain_check.blockSignals(True)
        self.domain_check.setEnabled(has_domain)
        self.domain_check.setToolTip(
            "勾选时字段列表包含域字段；取消勾选只看普通字段"
            if has_domain
            else "当前图层没有域字段"
        )
        if not has_domain:
            self.domain_check.setChecked(True)
        self.domain_check.blockSignals(False)

    def _on_field_filter_toggled(self, checked: bool) -> None:
        self.field_filter_edit.setVisible(checked)
        self._refresh_fields()

    def _refresh_fields(self) -> None:
        """按「域字段」与关键字筛选重建字段列表，并尽量保留当前激活字段。"""
        layer = self.current_layer()
        term = self.field_filter_edit.text().strip().lower() if self.field_filter_btn.isChecked() else ""
        include_domains = self.domain_check.isChecked()

        self.field_list.blockSignals(True)
        self.field_list.clear()
        self._field_entries = []
        if layer is not None:
            for field in layer.fields():
                labels = _domain_labels(field)
                is_domain = labels is not None or _has_dict_table(layer, field.name())
                if is_domain and not include_domains:
                    continue
                display = field.name()
                alias = field_alias(field)
                if alias and alias != display:
                    display = f"{display}（{alias}）"
                if term and term not in display.lower() and term not in field.name().lower():
                    continue
                self._field_entries.append(
                    _FieldEntry(
                        name=field.name(),
                        display=display,
                        domain_labels=labels,
                    )
                )
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, field.name())
                item.setToolTip(f"字段：{field.name()}")
                if alias and alias != field.name():
                    item.setToolTip(f"字段：{field.name()}\n别名：{alias}")
                self.field_list.addItem(item)

        if self._field_entries:
            row = next(
                (
                    index
                    for index, entry in enumerate(self._field_entries)
                    if entry.name == self._active_field
                ),
                0,
            )
            self._active_field = self._field_entries[row].name
            self.field_list.setCurrentRow(row)
        else:
            self._active_field = ""
            self.field_list.setCurrentRow(-1)
        self.field_list.blockSignals(False)
        # 不自动取唯一值：切字段 / 筛选字段后清空，由用户点「获取唯一值」
        self._clear_unique_values()

    def _active_entry(self) -> _FieldEntry | None:
        for entry in self._field_entries:
            if entry.name == self._active_field:
                return entry
        return None

    def _on_field_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._field_entries):
            return
        self._active_field = self._field_entries[row].name
        self._clear_unique_values()

    def _on_field_clicked(self, item: QListWidgetItem) -> None:
        """点击字段：设为当前字段（唯一值需手动点「获取唯一值」），并插入字段名。"""
        name = item.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        self._active_field = str(name)
        self._clear_unique_values()
        self._insert_text(f'"{name}"')

    # ------------------------------------------------------------ 唯一值

    def _clear_unique_values(self) -> None:
        """清空唯一值列表（切字段 / 切图层 / 改筛选条件时调用）。"""
        self.unique_list.clear()
        self._unique_values = []
        self.show_code_check.setEnabled(False)

    def _refresh_unique_values(self) -> None:
        """取当前字段的唯一值（``provider.uniqueValues``，最多 ``_UNIQUE_LIMIT`` 个）。"""
        self.unique_list.clear()
        self._unique_values = []
        layer = self.current_layer()
        entry = self._active_entry()
        if layer is None or entry is None:
            self.show_code_check.setEnabled(False)
            return
        index = layer.fields().indexOf(entry.name)
        if index < 0:
            self.show_code_check.setEnabled(False)
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            values = layer.dataProvider().uniqueValues(index, _UNIQUE_LIMIT)
        except Exception as e:  # noqa: BLE001 —— 驱动不支持时按空列表处理
            logger.warning("取字段 %s 唯一值失败：%s", entry.name, e)
            values = set()
        finally:
            QApplication.restoreOverrideCursor()

        cleaned = [unwrap(value) for value in values]
        cleaned = [value for value in cleaned if value is not None and value != ""]
        cleaned.sort(key=_sort_key)
        self._unique_values = cleaned
        self.show_code_check.setEnabled(bool(entry.domain_labels) and bool(cleaned))
        self._render_unique_values()

    def _render_unique_values(self) -> None:
        """按「显示代码」开关把唯一值渲染成标签或代码。"""
        self.unique_list.clear()
        entry = self._active_entry()
        show_code = self.show_code_check.isChecked()
        for value in self._unique_values:
            code = format_value(value)
            text = code
            if entry is not None and entry.domain_labels and not show_code:
                text = entry.domain_labels.get(code, code)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, value)
            if text != code:
                item.setToolTip(code)
            self.unique_list.addItem(item)
        if len(self._unique_values) >= _UNIQUE_LIMIT:
            hint = QListWidgetItem(f"（仅显示前 {_UNIQUE_LIMIT} 个唯一值）")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.unique_list.addItem(hint)

    def _on_unique_clicked(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if value is None:
            return
        self._insert_text(_literal(value))

    # ------------------------------------------------------------ 条件编辑

    def _insert_text(self, text: str, cursor_back: int = 0) -> None:
        """在条件框光标处插入文本（``cursor_back`` > 0 时插入后光标左移）。"""
        cursor = self.condition_edit.textCursor()
        if self.condition_edit.isReadOnly():
            return
        cursor.insertText(text)
        if cursor_back > 0:
            for _ in range(cursor_back):
                cursor.movePosition(QTextCursor.MoveOperation.Left)
        self.condition_edit.setTextCursor(cursor)
        self.condition_edit.setFocus()

    def _validate_condition(self, condition: str) -> str | None:
        """返回错误信息；``None`` 表示语法与字段引用都合法。"""
        if not condition:
            return "请输入查询条件。"
        expression = QgsExpression(condition)
        if expression.hasParserError():
            return f"条件语法错误！\n{expression.parserErrorString()}"
        layer = self.current_layer()
        if layer is None:
            return "请选择查询图层。"
        valid_fields = {field.name() for field in layer.fields()}
        unknown = sorted(name for name in expression.referencedColumns() if name not in valid_fields)
        if unknown:
            return "无效字段：" + "、".join(unknown)
        return None

    # ------------------------------------------------------------ 查询方案

    def _load_schemes(self) -> None:
        data = load_state(_STATE_NAME)
        schemes = data.get("schemes")
        if not isinstance(schemes, list):
            return
        for entry in schemes:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            expression = entry.get("expression")
            if not (name and isinstance(expression, str)):
                continue
            scheme: dict[str, Any] = {"name": name, "expression": expression}
            layer = entry.get("layer")
            if isinstance(layer, dict):
                info = {
                    key: str(layer.get(key) or "")
                    for key in ("id", "name", "source")
                    if layer.get(key)
                }
                if info:
                    scheme["layer"] = info
            self._schemes.append(scheme)

    def _save_schemes(self) -> None:
        if not save_state(_STATE_NAME, {"schemes": self._schemes}):
            QMessageBox.warning(
                self,
                "保存失败",
                "查询方案写入配置目录失败（本次会话内仍可继续使用）。",
            )

    def _refresh_schemes(self, select: str | None = None) -> None:
        self.scheme_combo.blockSignals(True)
        self.scheme_combo.clear()
        if not self._schemes:
            self.scheme_combo.addItem("（暂无存储方案）", None)
            self.scheme_combo.setEnabled(False)
            self.delete_btn.setEnabled(False)
        else:
            self.scheme_combo.setEnabled(True)
            self.scheme_combo.addItem("（选择已存储方案）", None)
            for scheme in self._schemes:
                self.scheme_combo.addItem(scheme["name"], scheme["name"])
            index = 0
            if select:
                index = next(
                    (
                        i + 1
                        for i, scheme in enumerate(self._schemes)
                        if scheme["name"] == select
                    ),
                    0,
                )
            self.scheme_combo.setCurrentIndex(index)
            self.delete_btn.setEnabled(index > 0)
        self.scheme_combo.blockSignals(False)

    def _layer_info(self) -> dict[str, str]:
        """当前图层的身份信息（随方案一起存储，下次选择方案时据此选层）。"""
        layer = self.current_layer()
        if layer is None:
            return {}
        return {"id": layer.id(), "name": layer.name(), "source": layer.source()}

    def _match_scheme_layer(
        self, info: dict[str, Any], layers: list["QgsVectorLayer"] | None = None
    ) -> "QgsVectorLayer | None":
        """按「id → 数据源 → 图层名」匹配方案对应的图层；找不到返回 ``None``。"""
        candidates = self._layers if layers is None else layers
        layer_id = str(info.get("id") or "")
        source = str(info.get("source") or "")
        name = str(info.get("name") or "")
        for key, value in (("id", layer_id), ("source", source), ("name", name)):
            if not value:
                continue
            for layer in candidates:
                if sip.isdeleted(layer):
                    continue
                if key == "id" and layer.id() == value:
                    return layer
                if key == "source" and layer.source() == value:
                    return layer
                if key == "name" and layer.name() == value:
                    return layer
        return None

    def _select_layer(self, layer: "QgsVectorLayer") -> None:
        """把图层下拉切到指定图层（触发字段列表刷新）。"""
        for index in range(self.layer_combo.count()):
            if self.layer_combo.itemData(index) is layer:
                if self.layer_combo.currentIndex() != index:
                    self.layer_combo.setCurrentIndex(index)
                return

    def _on_scheme_changed(self, index: int) -> None:
        name = self.scheme_combo.itemData(index) if index >= 0 else None
        self.delete_btn.setEnabled(bool(name))
        scheme = next((item for item in self._schemes if item["name"] == name), None)
        if scheme is None:
            return
        info = scheme.get("layer")
        if isinstance(info, dict) and info:
            layer = self._match_scheme_layer(info)
            if layer is None:
                # 面板可能一直开着：用户刚在工程里加载了新图层，按当前工程再找一次
                project_layers = [
                    item
                    for item in QgsProject.instance().mapLayers().values()
                    if isinstance(item, QgsVectorLayer)
                ]
                layer = self._match_scheme_layer(info, project_layers)
                if layer is not None:
                    self._layers = project_layers
                    self._populate_layers()
            if layer is None:
                label = str(info.get("name") or info.get("source") or "未知图层")
                QMessageBox.warning(
                    self,
                    "图层不存在",
                    f"查询方案「{name}」对应的图层「{label}」不在当前工程中"
                    "（对应文件可能已被移除），请先加载该图层后再使用此方案。",
                )
                return
            self._select_layer(layer)
        self.condition_edit.setPlainText(scheme["expression"])
        cursor = self.condition_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.condition_edit.setTextCursor(cursor)

    def _on_save_scheme(self) -> None:
        condition = self.condition_edit.toPlainText().strip()
        if not condition:
            QMessageBox.warning(self, "无法存储", "查询条件为空，无法存储。")
            return
        current = self.scheme_combo.currentData()
        default_name = str(current) if current else f"方案{len(self._schemes) + 1}"
        name, ok = QInputDialog.getText(self, "存储查询方案", "方案名称：", text=default_name)
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "无法存储", "方案名称不能为空。")
            return
        existing = next((item for item in self._schemes if item["name"] == name), None)
        info = self._layer_info()
        if existing is not None:
            existing["expression"] = condition
            if info:
                existing["layer"] = info
        else:
            scheme: dict[str, Any] = {"name": name, "expression": condition}
            if info:
                scheme["layer"] = info
            self._schemes.append(scheme)
        self._save_schemes()
        self._refresh_schemes(select=name)
        self.result_label.setText(f"查询方案「{name}」已存储。")

    def _on_delete_scheme(self) -> None:
        current = self.scheme_combo.currentData()
        if not current:
            return
        name = str(current)
        answer = QMessageBox.question(
            self,
            "删除查询方案",
            f"确定删除查询方案「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._schemes = [item for item in self._schemes if item["name"] != name]
        self._save_schemes()
        self._refresh_schemes()
        self.result_label.setText(f"查询方案「{name}」已删除。")

    # ------------------------------------------------------------ 查询 / 清除

    def _on_query(self) -> None:
        """把条件命中的要素按选择方式写入选择集（**不改图层筛选**）。"""
        layer = self.current_layer()
        if layer is None:
            QMessageBox.warning(self, "无法查询", "请选择要查询的矢量图层。")
            return
        condition = self.condition_edit.toPlainText().strip()
        error = self._validate_condition(condition)
        if error is not None:
            QMessageBox.critical(self, "条件有误", error)
            return

        action = str(self.method_combo.currentData() or "SetSelection")
        try:
            layer.selectByExpression(condition, selection_behavior(action))
        except Exception as e:  # noqa: BLE001 —— 提供者拒绝 / 表达式求值异常
            logger.exception("条件查询执行失败")
            QMessageBox.critical(self, "查询失败", f"查询执行异常！\n{e}")
            return

        selected = layer.selectedFeatureCount()
        if action == "SetSelection":
            text = f"查询完成：命中 {selected} 个图斑，已作为新的选择集。"
        elif action == "AddToSelection":
            text = f"查询完成：命中要素已加入当前选择集，当前共 {selected} 个。"
        else:
            text = f"查询完成：命中要素已从当前选择集移除，当前剩 {selected} 个。"
        self.result_label.setText(text)

    def _on_clear(self) -> None:
        """清空条件框，并清掉当前查询图层的选择集（对应原型的「清除」）。"""
        self.condition_edit.clear()
        layer = self.current_layer()
        if layer is not None:
            try:
                layer.removeSelection()
            except Exception:  # noqa: BLE001 —— 图层已销毁时忽略
                logger.warning("清除图层选择集失败", exc_info=True)
        self.result_label.setText("已清除查询条件与当前查询图层的选择集。")
