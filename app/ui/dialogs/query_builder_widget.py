"""属性筛选器本体：字段 / 值预览 / 操作符 / 条件编辑框。

「属性表 → 属性筛选」的 :class:`~app.ui.dialogs.query_builder.QueryBuilderDialog`
与「图层属性 → 条件」页共用这一套界面和取值 / 插入逻辑，区别只在条件最终怎么用：

- 属性筛选对话框：点「确定应用」写进图层 ``subsetString``；
- 图层属性 → 条件页：由外层「确定 / 应用」写进 ``subsetString``。

字段下拉支持关键字模糊搜索（同时匹配字段名与别名）；勾选「域字段」后只显示
挂了字典 / 值映射的字段，不勾选显示全部字段。字段值预览会对域字段显示字典
名称，双击时仍插入原始代码（表达式里比较的是库里存的值）。
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass

from qgis.core import QgsExpression, QgsFeatureRequest, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, Qt, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.util.dict_values import layer_dict_field_names, load_field_dict
from app.ui.tools.feature_attrs import field_alias

_OPERATORS = [
    "=", "<", ">", "LIKE", "%", "IN", "NOT IN", "<=", ">=", "!=", "ILIKE",
    "IS NULL", "IS NOT NULL", "AND", "OR", "NOT",
]

#: 「加载采样值」读取的要素条数
_SAMPLE_COUNT = 500


class _SqlCountWorker(QObject):
    """后台统计匹配要素数（「测试条件」全表扫描不阻塞主线程）。"""

    counted = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, layer: QgsVectorLayer, condition: str) -> None:
        super().__init__()
        self.layer = layer
        self.condition = condition
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="condition-count", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            request = QgsFeatureRequest().setFilterExpression(self.condition)
            count = 0
            iterator = self.layer.getFeatures(request)
            for _ in iterator:
                if self._cancel.is_set():
                    break
                count += 1
            with contextlib.suppress(Exception):
                iterator.close()
            if not self._cancel.is_set():
                self.counted.emit(count)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


@dataclass(frozen=True)
class _FieldEntry:
    """字段下拉的一项。"""

    #: 真实字段名（插入表达式 / 取值都用它）
    name: str
    #: 下拉显示文本（别名优先）
    display: str
    #: 是否挂了字典 / 值映射
    is_domain: bool


class _CompactListWidget(QListWidget):
    """值列表：默认高度小一点，窗口变大时仍可继续拉伸。"""

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(150)
        return hint


class _CompactTextEdit(QTextEdit):
    """条件编辑框：默认高度小一点，窗口变大时仍可继续拉伸。"""

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(120)
        return hint


class QueryBuilderWidget(QWidget):
    """字段选择 + 值预览 + 操作符 + 条件编辑框。"""

    def __init__(
        self,
        layer: QgsVectorLayer | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.layer = layer if isinstance(layer, QgsVectorLayer) else None
        self.valid_fields = [field.name() for field in layer.fields()] if self.layer is not None else []
        self.current_field = ""
        self._field_entries: list[_FieldEntry] = []
        self._values: list[object] = []
        self._labels: dict[str, str] = {}
        self._test_worker: _SqlCountWorker | None = None
        self._build()
        self._init_signals()
        self._reload_fields()

    # ------------------------------------------------------------ 装配

    def _build(self) -> None:
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(8)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(splitter, 1)

        # 左：字段 + 值预览
        field_group = QGroupBox("图层字段")
        f_layout = QVBoxLayout(field_group)
        field_title = QHBoxLayout()
        field_title.addWidget(QLabel("选中字段："))
        field_title.addStretch(1)
        self.domain_check = QCheckBox("域字段")
        self.domain_check.setChecked(False)
        field_title.addWidget(self.domain_check)
        f_layout.addLayout(field_title)
        self.field_combo = QComboBox()
        self.field_combo.setEditable(True)
        self.field_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.field_combo.setToolTip("可直接输入字段名 / 别名筛选")
        line_edit = self.field_combo.lineEdit()
        line_edit.setPlaceholderText("输入字段名 / 别名筛选…")
        line_edit.setClearButtonEnabled(True)
        completer = QCompleter(self.field_combo.model(), self.field_combo)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setMaxVisibleItems(15)
        self.field_combo.setCompleter(completer)
        f_layout.addWidget(self.field_combo)
        f_layout.addWidget(QLabel("字段值预览："))
        self.value_list = _CompactListWidget()
        self.value_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        f_layout.addWidget(self.value_list)
        btn_layout = QHBoxLayout()
        self.sample_btn = QPushButton("加载采样值")
        self.sample_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sample_btn.setToolTip(f"加载前{_SAMPLE_COUNT}条数据的去重值")
        self.all_btn = QPushButton("加载全部值")
        self.all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.show_code_check = QCheckBox("显示代码")
        self.show_code_check.setChecked(False)
        self.show_code_check.setToolTip("勾选后域值显示为 名称(代码)")
        btn_layout.addWidget(self.sample_btn)
        btn_layout.addWidget(self.all_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.show_code_check)
        f_layout.addLayout(btn_layout)
        splitter.addWidget(field_group)

        # 右：条件编辑
        sql_group = QGroupBox("筛选条件")
        sql_layout = QVBoxLayout(sql_group)
        grid = QGridLayout()
        for i, op in enumerate(_OPERATORS):
            r, c = divmod(i, 5)
            button = QPushButton(op)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setMinimumSize(60, 30)
            button.clicked.connect(lambda _checked=False, text=op: self._insert_text(text))
            grid.addWidget(button, r, c)
        sql_layout.addLayout(grid)
        self.sql_edit = _CompactTextEdit()
        self.sql_edit.setPlaceholderText("请输入筛选条件（如：县代码 = '360202'）")
        self.sql_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sql_edit.setFocus()
        sql_layout.addWidget(self.sql_edit)
        self.applied_label = QLabel()
        self.applied_label.setObjectName("QueryBuilderAppliedLabel")
        self.applied_label.setWordWrap(True)
        sql_layout.addWidget(self.applied_label)
        action_row = QHBoxLayout()
        self.test_condition_btn = QPushButton("测试条件")
        self.test_condition_btn.setToolTip("统计当前条件匹配的要素数")
        self.test_condition_btn.clicked.connect(self._test_condition)
        self.clear_condition_btn = QPushButton("清除条件")
        self.clear_condition_btn.setToolTip("清空条件；点「确定 / 应用」后恢复显示全部要素")
        self.clear_condition_btn.clicked.connect(self.clear_condition)
        action_row.addWidget(self.test_condition_btn)
        action_row.addWidget(self.clear_condition_btn)
        action_row.addStretch(1)
        sql_layout.addLayout(action_row)
        splitter.addWidget(sql_group)

        splitter.setSizes([300, 420])
        self.setLayout(main)

    def _init_signals(self) -> None:
        self.domain_check.toggled.connect(lambda _checked: self._populate_fields())
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        self.field_combo.activated[int].connect(self._on_field_chosen)
        self.field_combo.editTextChanged.connect(self._on_field_text_edited)
        self.show_code_check.toggled.connect(self._on_show_code_toggled)
        self.sample_btn.clicked.connect(lambda: self._load_values(sample=True))
        self.all_btn.clicked.connect(lambda: self._load_values(sample=False))
        self.value_list.itemDoubleClicked.connect(self._on_value_double_clicked)

    # ------------------------------------------------------------ 对外接口

    def set_layer(self, layer: QgsVectorLayer | None) -> None:
        self.layer = layer if isinstance(layer, QgsVectorLayer) else None
        self.valid_fields = [field.name() for field in layer.fields()] if self.layer is not None else []
        self.current_field = ""
        self._clear_values()
        self._reload_fields()

    def condition(self) -> str:
        return self.sql_edit.toPlainText().strip()

    def set_condition(self, text: str) -> None:
        """回填条件，并把光标放到末尾便于继续编辑。"""
        self.sql_edit.setPlainText(text or "")
        cursor = self.sql_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.sql_edit.setTextCursor(cursor)

    def clear_condition(self) -> None:
        """只清空本地编辑内容，不动图层筛选。"""
        self.sql_edit.clear()
        self._clear_values()

    def set_applied_text(self, text: str) -> None:
        self.applied_label.setText(text)

    def condition_error(self, condition: str) -> str | None:
        """条件合法性检查：返回错误信息，``None`` 表示合法。"""
        if self.layer is None:
            return "请选择要筛选的矢量图层。"
        expression = QgsExpression(condition)
        if expression.hasParserError():
            return f"SQL 语法错误！\n{expression.parserErrorString()}"
        valid_fields = {field.name() for field in self.layer.fields()}
        unknown = [name for name in expression.referencedColumns() if name not in valid_fields]
        if unknown:
            return "无效字段：" + "、".join(unknown)
        return None

    def _test_condition(self) -> None:
        """统计当前条件匹配的要素数。"""
        condition = self.condition()
        if not condition:
            QMessageBox.warning(self, "警告", "请输入有效的筛选条件！")
            return
        error = self.condition_error(condition)
        if error is not None:
            QMessageBox.critical(self, "测试失败", error)
            return
        self.test_condition_btn.setEnabled(False)
        self.test_condition_btn.setText("查询中…")
        worker = _SqlCountWorker(self.layer, condition)
        worker.counted.connect(self._on_test_counted)
        worker.failed.connect(self._on_test_failed)
        self._test_worker = worker
        worker.start()

    def _reset_test_btn(self) -> None:
        self._test_worker = None
        self.test_condition_btn.setEnabled(True)
        self.test_condition_btn.setText("测试条件")

    def _on_test_counted(self, count: int) -> None:
        self._reset_test_btn()
        QMessageBox.information(self, "测试成功", f"条件合法！\n匹配要素：{count}")

    def _on_test_failed(self, message: str) -> None:
        self._reset_test_btn()
        QMessageBox.critical(self, "测试失败", f"查询执行异常！\n{message}")

    def cancel_test(self) -> None:
        """关闭 / 隐藏编辑器时取消后台计数，避免线程继续占用图层。"""
        if self._test_worker is not None:
            self._test_worker.cancel()
        self._reset_test_btn()

    def hideEvent(self, event) -> None:
        self.cancel_test()
        super().hideEvent(event)

    # ------------------------------------------------------------ 字段

    def _reload_fields(self) -> None:
        """重新读取图层字段、域字段标记，并重建下拉。"""
        self._field_entries = []
        if self.layer is not None:
            try:
                domain_fields = set(layer_dict_field_names(self.layer))
            except Exception:  # noqa: BLE001 —— 字典元数据读不到时按普通字段处理
                domain_fields = set()
            fields = self.layer.fields()
            for i in range(fields.size()):
                field = fields.at(i)
                name = field.name()
                display = name
                alias = field_alias(field)
                if alias and alias != name:
                    display = f"{name}（{alias}）"
                self._field_entries.append(
                    _FieldEntry(
                        name=name,
                        display=display,
                        is_domain=name in domain_fields or self._is_value_map(field),
                    )
                )
        self._update_domain_check()
        self._populate_fields()

    @staticmethod
    def _is_value_map(field) -> bool:
        """字段是否配了 QGIS ``ValueMap``（值映射不一定会写字典表）。"""
        try:
            return field.editorWidgetSetup().type() == "ValueMap"
        except Exception:  # noqa: BLE001 —— 老驱动可能没有 widget 配置
            return False

    def _update_domain_check(self) -> None:
        """有域字段时「域字段」勾选才有效（无域字段的图层置灰）。"""
        has_domain = any(entry.is_domain for entry in self._field_entries)
        self.domain_check.blockSignals(True)
        self.domain_check.setEnabled(has_domain)
        self.domain_check.setToolTip(
            "勾选后只显示有值域的字段；不勾选显示全部字段"
            if has_domain
            else "当前图层没有域字段"
        )
        if not has_domain:
            self.domain_check.setChecked(False)
        self.domain_check.blockSignals(False)

    def _populate_fields(self) -> None:
        """按「域字段」勾选重建字段下拉，并尽量保留当前字段。"""
        only_domains = self.domain_check.isChecked()
        previous = self.current_field

        self.field_combo.blockSignals(True)
        self.field_combo.clear()
        selected = -1
        for entry in self._field_entries:
            if only_domains and not entry.is_domain:
                continue
            self.field_combo.addItem(entry.display, entry.name)
            if entry.name == previous:
                selected = self.field_combo.count() - 1
        if selected < 0 and self.field_combo.count():
            selected = 0
        self.field_combo.setCurrentIndex(selected)
        if selected < 0:
            self.field_combo.setEditText("")
        self.field_combo.blockSignals(False)

        current = self.field_combo.itemData(selected) if selected >= 0 else ""
        self.current_field = str(current or "")
        self._clear_values()

    # ------------------------------------------------------------ 值

    def _clear_values(self) -> None:
        """清空值预览；「显示代码」是用户偏好，切换字段时保持不变。"""
        self.value_list.clear()
        self._values = []
        self._labels = {}

    def _on_field_changed(self, index: int) -> None:
        self.current_field = str(self.field_combo.itemData(index) or "")
        self._clear_values()

    def _on_field_text_edited(self, text: str) -> None:
        """输入筛选文字时定位字段；没匹配到具体字段就清空当前字段。

        下拉框本身是 ``QgsFieldComboBox`` 的替代：可编辑 + ``MatchContains``
        补全，输入任意一段字段名 / 别名即可筛选候选字段。只有真正选中某个字段
        （精确匹配或从补全列表选择）后才允许取值 / 双击插入值。
        """
        text = text.strip()
        index = -1
        if text:
            index = self.field_combo.findText(text, Qt.MatchFlag.MatchExactly)
            if index < 0:
                for i in range(self.field_combo.count()):
                    if str(self.field_combo.itemData(i) or "") == text:
                        index = i
                        break
        if index >= 0:
            if index != self.field_combo.currentIndex():
                self.field_combo.setCurrentIndex(index)
            name = str(self.field_combo.itemData(index) or "")
            if name != self.current_field:
                self.current_field = name
                self._clear_values()
            return
        if not text:
            # 点清空按钮 / 手动删空：保持「未选中字段」，不要自动跳回某个字段
            self.field_combo.blockSignals(True)
            self.field_combo.setCurrentIndex(-1)
            self.field_combo.setEditText("")
            self.field_combo.blockSignals(False)
            self.current_field = ""
            self._clear_values()
            return
        # 还没匹配到具体字段：清掉当前字段，避免继续用上一次字段取值
        self.current_field = ""
        self._clear_values()

    def _on_field_chosen(self, index: int) -> None:
        name = self.field_combo.itemData(index)
        self.current_field = str(name or "")
        self._clear_values()
        if name:
            self._insert_text(f' "{name}" ')

    def _insert_text(self, text: str) -> None:
        cur = self.sql_edit.textCursor()
        cur.insertText(text)
        self.sql_edit.setTextCursor(cur)

    def _on_value_double_clicked(self, item: QListWidgetItem) -> None:
        field_name = self.current_field
        if not field_name:
            return
        index = self.layer.fields().indexOf(str(field_name))
        if index < 0:
            return
        field = self.layer.fields().at(index)
        value = item.data(Qt.ItemDataRole.UserRole)
        if value is None:
            value = item.text()
        # QGIS 4.x：QgsField.String 已移除，字段类型用 QVariant.Type 判断
        if field.type() == QVariant.String:
            quote = chr(39)
            text = f"'{str(value).replace(quote, quote * 2)}'"
        else:
            text = str(value)
        self._insert_text(text)

    def _load_values(self, sample: bool = True, sample_count: int = _SAMPLE_COUNT) -> None:
        field_name = self.current_field
        if not field_name or self.layer is None:
            QMessageBox.warning(self, "警告", "无效字段或图层，无法加载值！")
            return
        field_name = str(field_name)
        if self.layer.fields().indexOf(field_name) < 0:
            QMessageBox.warning(self, "警告", "无效字段或图层，无法加载值！")
            return

        labels = self._domain_labels(field_name)
        self._clear_values()
        self._labels = labels
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes([field_name], self.layer.fields())
        seen: set = set()
        values: list[object] = []
        for i, feature in enumerate(self.layer.getFeatures(request)):
            if sample and i >= sample_count:
                break
            value = feature.attribute(field_name)
            if value is None or value in seen:
                continue
            seen.add(value)
            values.append(value)
        self._values = values
        self._render_values()

    def _on_show_code_toggled(self, checked: bool) -> None:
        """预览为空时手动勾选「显示代码」，顺带加载一次采样值。"""
        if (
            checked
            and not self._values
            and self.layer is not None
            and self.current_field
            and self.layer.fields().indexOf(self.current_field) >= 0
        ):
            self._load_values(sample=True)
            return
        self._render_values()

    def _render_values(self) -> None:
        """按「显示代码」当前状态重画值列表（双击插入的始终是原始值）。"""
        self.value_list.clear()
        show_code = self.show_code_check.isChecked()
        for value in self._values:
            code = str(value)
            label = self._labels.get(code, code)
            text = f"{label}({code})" if show_code and label != code else label
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, value)
            if label != code and not show_code:
                item.setToolTip(code)
            self.value_list.addItem(item)

    def _domain_labels(self, field_name: str) -> dict[str, str]:
        """域字段的「代码 → 显示名」；不是域字段返回空映射。"""
        labels: dict[str, str] = {}
        index = self.layer.fields().indexOf(field_name)
        if index >= 0:
            labels.update(self._value_map_labels(self.layer.fields().at(index)))
        try:
            field_dict = load_field_dict(self.layer, field_name)
        except Exception:  # noqa: BLE001 —— 字典读取失败时退回原始值
            field_dict = None
        if field_dict is not None:
            labels.update({item.code: item.name for item in field_dict.values})
        return labels

    @staticmethod
    def _value_map_labels(field) -> dict[str, str]:
        """QGIS ``ValueMap`` 配置里的「代码 → 显示名」。"""
        try:
            setup = field.editorWidgetSetup()
        except Exception:  # noqa: BLE001
            return {}
        if setup.type() != "ValueMap":
            return {}
        config = setup.config()
        mapping = config.get("map") if isinstance(config, dict) else None
        if not mapping:
            return {}
        if isinstance(mapping, dict):
            return {str(code): str(label) for code, label in mapping.items()}
        labels: dict[str, str] = {}
        for pair in mapping:  # PyQGIS 里通常是 [(code, label), ...]
            if isinstance(pair, list | tuple) and len(pair) >= 2:
                labels[str(pair[0])] = str(pair[1])
        return labels
