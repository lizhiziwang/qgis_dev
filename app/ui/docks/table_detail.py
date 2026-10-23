"""数据管理右栏详情页：矢量表字段编辑 / 字典表行编辑。

- 矢量表：三列式字段列表（名称 / 别名 / 类型 / 长度 / 小数位 / 字典表 / 字典域 /
  可空 / 可见），支持加列、删列、改名、改类型（重建表）；字典列双击打开选择器。
- 字典表：分页显示行，单元格直接改、可加行 / 删行。

所有读写都在 :mod:`app.core.domain.local_table_edit` 里；本模块只做界面与编排。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.domain.create_data.fields import FIELD_TYPES, to_int
from app.core.domain.local_table_edit import (
    LocalTableError,
    TableColumn,
    TableInfo,
    add_column,
    delete_rows,
    drop_column,
    insert_row,
    read_rows,
    read_table_info,
    rebuild_for_types,
    rename_column,
    update_row,
)
from app.core.util.field_meta import (
    FieldMeta,
    delete_field_meta,
    upsert_field_meta,
)
from app.core.util.local_db_util import local_db_util
from app.infra.logging import get_logger
from app.ui.common.widgets import open_file_dir, save_file
from app.ui.dialogs.dict_pick_dialog import DictPickDialog
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = get_logger(__name__)

_FIELD_HEADERS = (
    "字段名",
    "别名",
    "类型",
    "长度",
    "小数位",
    "字典表",
    "字典域",
    "可空",
    "可见",
)
_COL_NAME, _COL_ALIAS, _COL_TYPE, _COL_LENGTH, _COL_PRECISION, _COL_DICT_TABLE, _COL_DICT_DOMAIN, _COL_NULLABLE, _COL_VISIBLE = range(9)
_TEXT_COLUMNS = {_COL_NAME, _COL_ALIAS, _COL_LENGTH, _COL_PRECISION}
_CHECK_COLUMNS = {_COL_NULLABLE, _COL_VISIBLE}
_DICT_COLUMNS = {_COL_DICT_TABLE, _COL_DICT_DOMAIN}

_TYPE_BY_KEY = {item.key: item for item in FIELD_TYPES}
_NAME_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)
_ROW_LIMIT = 500


def _quote(name: str) -> str:
    """SQL 标识符加引号（表名 / 列名都可能带中文或空格）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _declared_type(type_key: str, length: int, precision: int) -> str:
    item = _TYPE_BY_KEY.get(type_key, FIELD_TYPES[0])
    if item.key in ("text", "dict"):
        return f"TEXT({length})" if length else "TEXT"
    if item.key in ("integer", "integer64"):
        return "INTEGER"
    if item.key == "double":
        return "REAL"
    if item.key == "date":
        return "DATE"
    if item.key == "datetime":
        return "DATETIME"
    if item.key == "bool":
        return "BOOLEAN"
    return "TEXT"


def _parse_type(declared: str) -> tuple[str, int, int]:
    text = str(declared or "").upper()
    match = re.match(r"(?:TEXT|VARCHAR|CHAR)\s*\((\d+)\)", text)
    if match:
        return "text", int(match.group(1)), 0
    if "DATETIME" in text or "TIMESTAMP" in text or text.startswith("TIME"):
        return "datetime", 0, 0
    if "DATE" in text:
        return "date", 0, 0
    if "BOOL" in text:
        return "bool", 0, 0
    if "INT" in text:
        return "integer", 0, 0
    if any(word in text for word in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "double", 0, 0
    return "text", 0, 0


@dataclass
class _FieldRow:
    original: TableColumn | None
    name: str
    alias: str = ""
    type_key: str = "text"
    length: int = 0
    precision: int = 0
    dict_table: str = ""
    dict_domain: str = ""
    nullable: bool = True
    visible: bool = True
    orig_dict_table: str = ""
    orig_dict_domain: str = ""
    orig_dict_local_table: str = ""
    dict_local_table: str = ""


class _DictRowDelegate(QStyledItemDelegate):
    """字典树统一加高行，双击编辑时文字不被裁掉。"""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 30))
        return size


class _RowDialog(QDialog):
    """新增字典行：按列给输入框（自增主键/隐藏列不显示）。"""

    def __init__(self, parent, info: TableInfo, defaults=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增字典行")
        defaults = defaults or {}
        self._columns = list(info.editable_columns)
        self._edits: dict[str, QLineEdit] = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for col in self._columns:
            edit = QLineEdit()
            default = str(col.default or "").strip().strip("'\"")
            if defaults.get(col.name):
                default = str(defaults[col.name])
            edit.setText(default)
            edit.setPlaceholderText("必填" if col.notnull and not default else "")
            form.addRow(col.name, edit)
            self._edits[col.name] = edit
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        for col in self._columns:
            if col.notnull and not self._edits[col.name].text().strip():
                QMessageBox.warning(self, "新增字典行", f"「{col.name}」不能为空")
                return
        self.accept()

    def values(self) -> dict:
        return {
            name: edit.text()
            for name, edit in self._edits.items()
            if edit.text().strip()
        }


class _EditorBase(QWidget):
    """详情页公共头部：返回按钮 + 标题 + 操作提示。"""

    backRequested = pyqtSignal()  # noqa: N815 —— Qt 信号命名
    dataChanged = pyqtSignal(str, str)  # noqa: N815

    def __init__(self, parent=None, *, category: str) -> None:
        super().__init__(parent)
        self._category = category
        self._db: dict = {}
        self._table = ""
        self._db_path = ""
        self._info: TableInfo | None = None
        self._loading = False
        self._before_change: Callable[[dict, str, str], bool] | None = None

    def set_before_change(self, callback) -> None:
        self._before_change = callback

    def _build_header(self, layout: QVBoxLayout) -> QHBoxLayout:
        row = QHBoxLayout()
        self.title = QLabel("")
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        row.addWidget(self.title)
        self.info_label = QLabel("")
        row.addWidget(self.info_label, 1)
        return row

    def _confirm_close_layers(self) -> bool:
        if self._before_change is None:
            return True
        try:
            return bool(self._before_change(self._db, self._category, self._table))
        except Exception as exc:
            logger.exception("准备修改表结构失败")
            QMessageBox.warning(self, "字段管理", f"修改前准备失败：{exc}")
            return False


class VectorFieldEditor(_EditorBase):
    """矢量表字段列表：加列 / 删列 / 改名 / 改类型 / 字典关联 / 隐藏。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, category="spatial")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._build_header(layout))
        actions = QHBoxLayout()
        add_btn = QPushButton("添加字段")
        add_btn.clicked.connect(self._add_field)
        actions.addWidget(add_btn)
        self.del_btn = QPushButton("删除选中")
        self.del_btn.clicked.connect(self._delete_selected)
        actions.addWidget(self.del_btn)
        actions.addStretch(1)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._save)
        actions.addWidget(save_btn)
        reload_btn = QPushButton("撤销 / 刷新")
        reload_btn.clicked.connect(self._reload)
        actions.addWidget(reload_btn)
        layout.addLayout(actions)
        # 字段搜索：只隐藏不匹配的行，不改字段列表本身（保存仍按全部字段）
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索："))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索字段名 / 别名 / 字典表 / 字典域…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _text: self._apply_filter())
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)
        self.table = QTableWidget(0, len(_FIELD_HEADERS))
        self.table.setHorizontalHeaderLabels(_FIELD_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # 字典域名很长（如 FL_SYS_ZQSJZD-LCXZ2_P-DI_LEI_YZT--图斑地类），给它
        # 吃掉剩余宽度；「可空 / 可见」只是两个勾选框，窄一点即可。
        for column, width in ((0, 130), (1, 150), (2, 90), (3, 60), (4, 60),
                              (5, 110), (6, 240), (7, 40), (8, 40)):
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(_COL_DICT_DOMAIN, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        # 点击表头临时排序（只改显示顺序，_rows 的真实顺序不动）
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sectionClicked.connect(self._on_header_sort)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.doubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.table, 1)
        self._rows: list[_FieldRow] = []
        #: 显示顺序（_rows 的下标）；排序只改这个列表，保存仍按 _rows 原顺序
        self._view_order: list[int] = []
        self._sort_column = -1
        self._sort_ascending = True

    # ------------------------------------------------------------ 加载

    def load(self, db: dict, table: str) -> None:
        self._db = dict(db or {})
        self._table = table
        self._db_path = str(self._db.get("path") or "")
        if self.search_edit.text():
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        try:
            info = read_table_info(self._db_path, table)
        except LocalTableError as exc:
            self.title.setText(table)
            self.info_label.setText(str(exc))
            self.table.setRowCount(0)
            self._rows = []
            self._view_order = []
            return
        self._info = info
        self.title.setText(f"矢量表：{table}")
        geometry = info.geometry_column or "（无）"
        self.info_label.setText(
            f"{info.kind} · 几何列：{geometry} · {info.row_count} 个要素"
        )
        rows: list[_FieldRow] = []
        meta_map = info.field_meta or {}
        meta_folded = {name.casefold(): meta for name, meta in meta_map.items()}
        aliases = info.aliases or {}
        for col in info.editable_columns:
            type_key, length, precision = _parse_type(col.declared_type)
            meta = meta_map.get(col.name) or meta_folded.get(col.name.casefold())
            meta = meta or FieldMeta()
            if meta.dict_table:
                type_key = "dict"
            rows.append(
                _FieldRow(
                    original=col,
                    name=col.name,
                    alias=aliases.get(col.name, ""),
                    type_key=type_key,
                    length=length,
                    precision=precision,
                    dict_table=meta.dict_table,
                    dict_domain=meta.dict_domain,
                    nullable=not col.notnull,
                    visible=meta.visible,
                    orig_dict_table=meta.dict_table,
                    orig_dict_domain=meta.dict_domain,
                    orig_dict_local_table=meta.dict_local_table,
                    dict_local_table=meta.dict_local_table,
                )
            )
        self._rows = rows
        self._view_order = list(range(len(rows)))
        self._sort_column = -1
        self._sort_ascending = True
        self.table.horizontalHeader().setSortIndicator(
            -1, Qt.SortOrder.AscendingOrder
        )
        self._render()

    def _reload(self) -> None:
        if self._table:
            self.load(self._db, self._table)

    # ------------------------------------------------------------ 渲染 / 取值

    def _render(self) -> None:
        order = self._view_order or list(range(len(self._rows)))
        self._loading = True
        try:
            self.table.setRowCount(len(order))
            for row_index, original in enumerate(order):
                row = self._rows[original]
                for column, text in (
                    (_COL_NAME, row.name),
                    (_COL_ALIAS, row.alias),
                    (_COL_LENGTH, str(row.length) if row.length else ""),
                    (_COL_PRECISION, str(row.precision) if row.precision else ""),
                    (_COL_DICT_TABLE, row.dict_table),
                    (_COL_DICT_DOMAIN, row.dict_domain),
                ):
                    item = QTableWidgetItem(text)
                    if column in _DICT_COLUMNS:
                        item.setFlags(
                            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                        )
                        item.setToolTip("双击可挂接字典")
                    self.table.setItem(row_index, column, item)
                combo = QComboBox()
                for item_type in FIELD_TYPES:
                    combo.addItem(item_type.label, item_type.key)
                combo.setCurrentIndex(max(0, combo.findData(row.type_key)))
                combo.currentIndexChanged.connect(
                    lambda _index, r=row_index: self._on_type_changed(r)
                )
                self.table.setCellWidget(row_index, _COL_TYPE, combo)
                for column, checked in (
                    (_COL_NULLABLE, row.nullable),
                    (_COL_VISIBLE, row.visible),
                ):
                    item = QTableWidgetItem()
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    item.setCheckState(
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                    )
                    self.table.setItem(row_index, column, item)
        finally:
            self._loading = False
        self._apply_filter()

    def _on_header_sort(self, column: int) -> None:
        """表头点击：按该列临时排序（只改显示顺序，不改字段真实顺序）。"""
        if column < 0 or column >= len(_FIELD_HEADERS) or not self._rows:
            return
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self._view_order = list(range(len(self._rows)))
        self._view_order.sort(
            key=lambda index: self._sort_key(self._rows[index], column),
            reverse=not self._sort_ascending,
        )
        self.table.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder
            if self._sort_ascending
            else Qt.SortOrder.DescendingOrder,
        )
        self._render()

    @staticmethod
    def _sort_key(row: _FieldRow, column: int):
        if column == _COL_NAME:
            return row.name.casefold()
        if column == _COL_ALIAS:
            return row.alias.casefold()
        if column == _COL_TYPE:
            return _TYPE_BY_KEY.get(row.type_key, FIELD_TYPES[0]).label.casefold()
        if column == _COL_LENGTH:
            return row.length
        if column == _COL_PRECISION:
            return row.precision
        if column == _COL_DICT_TABLE:
            return row.dict_table.casefold()
        if column == _COL_DICT_DOMAIN:
            return row.dict_domain.casefold()
        if column == _COL_NULLABLE:
            return row.nullable
        if column == _COL_VISIBLE:
            return row.visible
        return ""

    def _apply_filter(self) -> None:
        """按搜索框隐藏不匹配的字段行（字段列表本身不变，保存仍是全部）。"""
        keyword = self.search_edit.text().strip().casefold()
        order = self._view_order or list(range(len(self._rows)))
        for row_index, original in enumerate(order):
            row = self._rows[original] if 0 <= original < len(self._rows) else None
            if row is None or not keyword:
                hidden = False
            else:
                type_label = _TYPE_BY_KEY.get(row.type_key, FIELD_TYPES[0]).label
                haystack = " ".join(
                    (
                        row.name,
                        row.alias,
                        row.dict_table,
                        row.dict_domain,
                        type_label,
                    )
                ).casefold()
                hidden = keyword not in haystack
            self.table.setRowHidden(row_index, hidden)

    def _row_at(self, row: int) -> _FieldRow | None:
        """显示行号 → 真实字段行（排序只改显示顺序）。"""
        order = self._view_order or list(range(len(self._rows)))
        if not (0 <= row < len(order)):
            return None
        original = order[row]
        return self._rows[original] if 0 <= original < len(self._rows) else None

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row = self._row_at(item.row())
        if row is None:
            return
        column = item.column()
        if column == _COL_NAME:
            row.name = item.text().strip()
        elif column == _COL_ALIAS:
            row.alias = item.text().strip()
        elif column == _COL_LENGTH:
            row.length = to_int(item.text(), 0)
        elif column == _COL_PRECISION:
            row.precision = to_int(item.text(), 0)
        elif column == _COL_NULLABLE:
            row.nullable = item.checkState() == Qt.CheckState.Checked
        elif column == _COL_VISIBLE:
            row.visible = item.checkState() == Qt.CheckState.Checked

    def _on_type_changed(self, row_index: int) -> None:
        if self._loading:
            return
        row = self._row_at(row_index)
        combo = self.table.cellWidget(row_index, _COL_TYPE)
        if row is None or not isinstance(combo, QComboBox):
            return
        row.type_key = str(combo.currentData() or "text")
        if row.type_key != "dict":
            row.dict_table = ""
            row.dict_domain = ""
            self._loading = True
            try:
                for column in _DICT_COLUMNS:
                    item = self.table.item(row_index, column)
                    if item is not None:
                        item.setText("")
            finally:
                self._loading = False

    def _on_double_clicked(self, index) -> None:
        if index.column() not in _DICT_COLUMNS:
            return
        self._pick_dict(index.row())

    def _pick_dict(self, row_index: int) -> None:
        row = self._row_at(row_index)
        if row is None:
            return
        dialog = DictPickDialog(
            self,
            category=row.dict_table,
            domain=row.dict_domain,
            field_name=row.name,
            field_alias=row.alias,
            local_db_path=self._db_path,
        )
        accepted = dialog.exec()
        selected = dialog.selected() if accepted else None
        local_table = dialog.selected_local_table() if accepted and selected else ""
        dialog.deleteLater()
        if not accepted:
            return
        row.dict_table, row.dict_domain = selected or ("", "")
        row.dict_local_table = local_table
        if selected:
            row.type_key = "dict"
        elif row.type_key == "dict":
            # 清掉挂接后按普通文本字段处理，不再要求字典域
            row.type_key = "text"
        self._render()

    # ------------------------------------------------------------ 增删 / 保存

    def _add_field(self) -> None:
        existing = {row.name for row in self._rows}
        index = 1
        name = "new_field"
        while name in existing:
            index += 1
            name = f"new_field_{index}"
        self._rows.append(
            _FieldRow(
                original=None,
                name=name,
                type_key="text",
                length=50,
                nullable=True,
                visible=True,
            )
        )
        # 结构变了：回到真实顺序（临时排序只影响查看）
        self._view_order = list(range(len(self._rows)))
        self._sort_column = -1
        self._sort_ascending = True
        self.table.horizontalHeader().setSortIndicator(
            -1, Qt.SortOrder.AscendingOrder
        )
        self._render()
        self.table.setRowHidden(len(self._rows) - 1, False)
        self.table.setCurrentCell(len(self._rows) - 1, _COL_NAME)

    def _delete_selected(self) -> None:
        selected_display = sorted(
            {index.row() for index in self.table.selectedIndexes()},
            reverse=True,
        )
        if not selected_display:
            QMessageBox.information(self, "字段管理", "请先选中要删除的字段行")
            return
        order = self._view_order or list(range(len(self._rows)))
        selected = sorted(
            {
                order[row]
                for row in selected_display
                if 0 <= row < len(order)
            },
            reverse=True,
        )
        for row_index in selected:
            if 0 <= row_index < len(self._rows):
                self._rows.pop(row_index)
        self._view_order = list(range(len(self._rows)))
        self._sort_column = -1
        self._sort_ascending = True
        self.table.horizontalHeader().setSortIndicator(
            -1, Qt.SortOrder.AscendingOrder
        )
        self._render()

    def _validate(self) -> str:
        names: set[str] = set()
        for row in self._rows:
            if not _NAME_RE.match(row.name):
                return f"字段名「{row.name}」不合法（不能为空、不能以数字开头）"
            key = row.name.casefold()
            if key in names:
                return f"字段名重复：{row.name}"
            names.add(key)
            if row.type_key == "dict" and not row.dict_table:
                return f"字段「{row.name}」选了字典类型，但没有挂接字典表"
            if row.dict_table and not row.dict_domain:
                return f"字段「{row.name}」选了字典表，但没有选择字典域"
            if row.original is None and not row.nullable:
                return f"新增字段「{row.name}」暂不支持「不可为空」，请先建成可空字段"
        return ""

    def _save(self) -> None:
        error = self._validate()
        if error:
            QMessageBox.warning(self, "字段管理", error)
            return
        if self._info is None:
            return
        original = {col.name: col for col in self._info.editable_columns}
        kept_originals = {
            row.original.name for row in self._rows if row.original is not None
        }
        deleted = [name for name in original if name not in kept_originals]
        renames: list[tuple[str, str]] = []
        type_changes: dict[str, tuple[str, bool]] = {}
        new_rows: list[_FieldRow] = []
        for row in self._rows:
            if row.original is None:
                new_rows.append(row)
                continue
            if row.name != row.original.name:
                renames.append((row.original.name, row.name))
            declared = _declared_type(row.type_key, row.length, row.precision)
            original_declared = str(row.original.declared_type or "").strip().upper()
            if (
                declared.upper() != original_declared
                or (not row.nullable) != bool(row.original.notnull)
            ):
                type_changes[row.original.name] = (declared, not row.nullable)
        cleared = [
            row.name
            for row in self._rows
            if row.original is not None and row.orig_dict_table and not row.dict_table
        ]
        if cleared:
            ret = QMessageBox.question(
                self,
                "清除字典挂接",
                "以下字段的字典挂接将被清除：" + "、".join(cleared) + "。是否继续保存？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if not (deleted or renames or type_changes or new_rows):
            # 只有别名 / 字典 / 可见性变化：不需要动表结构
            try:
                self._save_metadata()
            except LocalTableError as exc:
                QMessageBox.warning(self, "字段管理", str(exc))
                return
            QMessageBox.information(self, "字段管理", "字段修改已保存")
            return
        ret = QMessageBox.question(
            self,
            "保存字段结构",
            "将修改库表结构：\n"
            + ("\n".join(
                part
                for part in (
                    f"· 删除 {len(deleted)} 个字段" if deleted else "",
                    f"· 改名 {len(renames)} 个字段" if renames else "",
                    f"· 修改 {len(type_changes)} 个字段的类型/可空（重建表）"
                    if type_changes
                    else "",
                    f"· 新增 {len(new_rows)} 个字段" if new_rows else "",
                )
                if part
            ))
            + "\n\n表已在地图中打开时会先从工程移除，数据保留。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        if not self._confirm_close_layers():
            return
        try:
            if type_changes:
                rebuild_for_types(self._db_path, self._table, type_changes)
            for old, new in renames:
                rename_column(self._db_path, self._table, old, new)
            for row in new_rows:
                add_column(
                    self._db_path,
                    self._table,
                    row.name,
                    _declared_type(row.type_key, row.length, row.precision),
                    notnull=not row.nullable,
                )
            for name in deleted:
                drop_column(self._db_path, self._table, name)
        except LocalTableError as exc:
            QMessageBox.warning(self, "字段管理", str(exc))
            self._reload()
            return
        try:
            self._save_metadata()
        except LocalTableError as exc:
            QMessageBox.warning(self, "字段管理", f"结构已改，但别名/字典元数据保存失败：{exc}")
        self._reload()
        self.dataChanged.emit(self._category, self._table)
        QMessageBox.information(self, "字段管理", "字段结构已保存")

    def _ensure_local_dicts(self) -> list[str]:
        """保存前把选中的字典（系统 / 当前库）确保写进当前库；返回失败提示。"""
        rows = [row for row in self._rows if row.dict_table and row.dict_domain]
        if not rows:
            return []
        from app.core.domain.create_data.dict_tables import (
            create_local_dict_table,
            ensure_local_dict_domain,
        )
        from app.core.domain.dict_manage import DictService
        from app.core.domain.import_layer import unique_table_name

        try:
            service = DictService()
        except Exception:  # noqa: BLE001
            service = None
        local = _local_dict_tables(self._db_path)
        warnings: list[str] = []
        for row in rows:
            table = row.dict_local_table or local.get(row.dict_table, "")
            if not table:
                if service is None:
                    warnings.append(f"「{row.name}」：没有可用的系统字典")
                    continue
                try:
                    table = unique_table_name(self._db_path, row.dict_table)
                    create_local_dict_table(
                        self._db_path,
                        table,
                        source_category=row.dict_table,
                        dict_service=service,
                    )
                    local[row.dict_table] = table
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"「{row.name}」：{exc}")
                    continue
            row.dict_local_table = table
            try:
                ensure_local_dict_domain(
                    self._db_path,
                    table,
                    row.dict_table,
                    row.dict_domain,
                    service,
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"「{row.name}」：{exc}")
        return warnings

    def _save_metadata(self) -> None:
        """保存别名 / 字典关联 / 可见性（不动表结构）。"""
        if not self._db_path:
            return
        warnings = self._ensure_local_dicts()
        try:
            db = local_db_util(self._db_path)
        except Exception as exc:
            raise LocalTableError(f"打不开数据库：{exc}") from exc
        try:
            local_dict_tables = _dict_tables_by_category(db)
            for row in self._rows:
                alias = row.alias.strip()
                with _SuppressDbErrors():
                    db.cursor.execute(
                        "DELETE FROM gpkg_data_columns WHERE table_name = ? AND column_name = ?",
                        (self._table, row.name),
                    )
                with _SuppressDbErrors():
                    db.cursor.execute(
                        "DELETE FROM qgis_dev_column_alias WHERE table_name = ? AND column_name = ?",
                        (self._table, row.name),
                    )
                if alias:
                    if db.kind == "gpkg":
                        db.set_gpkg_column_alias(self._table, row.name, alias)
                    else:
                        db.set_column_aliases(self._table, {row.name: alias})
                meta = FieldMeta(
                    dict_table=row.dict_table,
                    dict_domain=row.dict_domain,
                    dict_local_table=(
                        row.dict_local_table
                        or local_dict_tables.get(row.dict_table, "")
                    ),
                    visible=row.visible,
                )
                if meta.is_default:
                    delete_field_meta(db.conn, self._table, row.name)
                else:
                    upsert_field_meta(db.conn, self._table, row.name, meta)
            db.conn.commit()
        finally:
            db.conn.close()
        if warnings:
            QMessageBox.warning(
                self,
                "字段管理",
                "部分字典未能写入本地库：\n" + "\n".join(warnings),
            )


class DictTableEditor(_EditorBase):
    """字典表内容：canonical 结构用与字典管理类似的树，其他表退回平铺行表。

    canonical = 同时有 ``domain`` / ``code`` / ``name`` 三列（创建数据落库的字典表就是
    这个结构）。树按「字典域 → 字典项（按 parent_code 多级）」懒加载，列与字典管理
    一致：名称 / 代码 / 字典域；双击名称或代码直接改，改代码会同步下级的上级代码。
    非 canonical 的第三方字典表仍按原来的分页平铺表格显示。
    """

    #: 删除了整个字典域（db, 字典表类别, 库内表名, 域名列表）—— 调用方据此
    #: 清掉已打开图层上对应的字典挂接属性
    dictDomainsDeleted = pyqtSignal(dict, str, str, object)  # noqa: N815

    _KIND = int(Qt.ItemDataRole.UserRole)
    _ROWID = _KIND + 1
    _DOMAIN = _KIND + 2
    _CODE = _KIND + 3
    _NAME = _KIND + 4
    _LOADED = _KIND + 5

    _K_DOMAIN = "domain"
    _K_ITEM = "item"
    _K_MORE = "more"
    _K_PLACEHOLDER = "placeholder"
    #: 搜索结果的分组节点（_run_search 用；此前漏定义，搜索槽一抛
    #: AttributeError 就"点了搜索没反应"）
    _K_GROUP = "group"

    def __init__(self, parent=None) -> None:
        super().__init__(parent, category="dict")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._build_header(layout))
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索："))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "搜索代码 / 名称 / 字典域（最多显示前 1000 条）"
        )
        self.search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._run_search)
        self.search_edit.textChanged.connect(lambda _text: self._search_timer.start())
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)
        actions = QHBoxLayout()
        add_btn = QPushButton("新增行")
        add_btn.clicked.connect(self._add_row)
        actions.addWidget(add_btn)
        del_btn = QPushButton("删除选中")
        del_btn.clicked.connect(self._delete_selected)
        actions.addWidget(del_btn)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._reload)
        actions.addWidget(refresh_btn)
        import_btn = QPushButton("Excel导入")
        import_btn.clicked.connect(self._on_import_dict)
        actions.addWidget(import_btn)
        export_btn = QPushButton("Excel导出")
        export_btn.clicked.connect(self._on_export_dict)
        actions.addWidget(export_btn)
        actions.addStretch(1)
        self.prev_btn = QPushButton("上一页")
        self.prev_btn.clicked.connect(lambda: self._page(-1))
        actions.addWidget(self.prev_btn)
        self.page_label = QLabel("")
        actions.addWidget(self.page_label)
        self.next_btn = QPushButton("下一页")
        self.next_btn.clicked.connect(lambda: self._page(1))
        actions.addWidget(self.next_btn)
        layout.addLayout(actions)

        self.stack = QStackedWidget(self)

        # 字典管理风格：名称 / 代码 / 字典域
        self.tree_page = QWidget(self)
        tree_layout = QVBoxLayout(self.tree_page)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["名称", "代码", "字典域"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 360)
        self.tree.setColumnWidth(1, 130)
        self.tree.setItemDelegate(_DictRowDelegate(self.tree))
        self.tree.itemExpanded.connect(self._on_tree_expanded)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        tree_layout.addWidget(self.tree, 1)

        # 非 canonical：原来的分页平铺表格
        self.table_page = QWidget(self)
        table_layout = QVBoxLayout(self.table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        table_layout.addWidget(self.table, 1)

        self.stack.addWidget(self.tree_page)
        self.stack.addWidget(self.table_page)
        layout.addWidget(self.stack, 1)
        self._offset = 0
        self._total = 0
        self._columns: list[str] = []
        self._rows: list[dict] = []
        self._canonical = False
        self._search_text = ""

    # ------------------------------------------------------------ 加载

    def load(self, db: dict, table: str) -> None:
        self._db = dict(db or {})
        self._table = table
        self._db_path = str(self._db.get("path") or "")
        self._offset = 0
        # 老库的字典逻辑名（attr.dict_table）可能还是复制来源的系统类别名；
        # 打开字典表时统一成库内表名，字段列表 / 挂接框里就只有一个名字了。
        try:
            from app.core.domain.create_data.dict_tables import (
                normalize_local_dict_names,
            )

            normalize_local_dict_names(self._db_path)
        except Exception:  # noqa: BLE001 —— 迁移失败不影响查看
            logger.warning("规范化本地字典表名失败", exc_info=True)
        self._reload()

    def _reload(self) -> None:
        if not self._db_path or not self._table:
            return
        self._search_text = ""
        if self.search_edit.text():
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        self._search_timer.stop()
        try:
            info = read_table_info(self._db_path, self._table)
        except LocalTableError as exc:
            self.title.setText(self._table)
            self.info_label.setText(str(exc))
            self.tree.clear()
            self.table.setRowCount(0)
            return
        self._info = info
        names = {col.name for col in info.editable_columns}
        self._canonical = (
            {"domain", "code", "name"} <= names and not info.without_rowid
        )
        if self._canonical:
            # 老库可能没有 (domain, parent_code) 索引；展开 2 万条的域会从
            # 0.03s 退化到 15s。CREATE INDEX IF NOT EXISTS 是幂等的快操作。
            self._ensure_lookup_index({col.name for col in info.editable_columns})
        meta = info.attr or {}
        logical = str(meta.get("dict_table") or "")
        # 本地字典的唯一名字就是表名；只有历史数据里少数"逻辑名 ≠ 表名"时
        # 才额外标注，避免标题里出现两个看起来像表名的东西。
        extra = f" · 别名：{logical}" if logical and logical != self._table else ""
        self.title.setText(f"字典表：{self._table}")
        self.info_label.setText(f"{info.kind} · {info.row_count} 行{extra}")
        if self._canonical:
            for widget in (self.prev_btn, self.next_btn):
                widget.setVisible(False)
            self.page_label.setVisible(False)
            self.stack.setCurrentWidget(self.tree_page)
            self._load_tree()
        else:
            for widget in (self.prev_btn, self.next_btn):
                widget.setVisible(True)
            self.page_label.setVisible(True)
            self.stack.setCurrentWidget(self.table_page)
            self._reload_table()

    def _ensure_lookup_index(self, names: set[str]) -> None:
        """确保 ``(domain, parent_code)`` 索引存在（老库打开时补建，幂等）。

        ``_load_children`` 按域 + 上级代码取子项，并对每一行做 ``EXISTS``
        子查询找下级；没有这个索引时，2 万条字典展开一次实测 15.6 秒，
        建索引后 0.03 秒。索引只是查询加速，不影响数据。
        """
        if "parent_code" not in names or not self._db_path or not self._table:
            return
        index_name = "ux_" + re.sub(r"\W+", "_", self._table) + "_domain_parent"
        try:
            db = local_db_util(self._db_path)
            try:
                db.cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {_quote(index_name)} "
                    f"ON {_quote(self._table)} (domain, parent_code)"
                )
                db.conn.commit()
            finally:
                db.conn.close()
        except Exception:  # noqa: BLE001 —— 建索引失败不该拦住查看字典
            logger.warning("补建字典表索引失败：%s", self._table, exc_info=True)

    # ------------------------------------------------------------ 字典树

    def _load_tree(self) -> None:
        self._loading = True
        try:
            self.tree.clear()
        finally:
            self._loading = False
        db = local_db_util(self._db_path)
        try:
            rows = db.cursor.execute(
                f'SELECT domain, COUNT(*) AS n FROM {_quote(self._table)} '
                "GROUP BY domain ORDER BY domain"
            ).fetchall()
        except Exception as exc:
            logger.warning("读取字典域失败：%s", exc, exc_info=True)
            QMessageBox.warning(self, "字典表", f"读取字典域失败：{exc}")
            return
        finally:
            db.conn.close()
        for domain, count in rows:
            node = QTreeWidgetItem([str(domain), "", ""])
            node.setData(0, self._KIND, self._K_DOMAIN)
            node.setData(0, self._DOMAIN, str(domain))
            node.setData(0, self._LOADED, 0)
            node.setData(0, self._CODE, "")
            font = node.font(0)
            font.setBold(True)
            node.setFont(0, font)
            if int(count):
                node.addChild(self._placeholder())
            self.tree.addTopLevelItem(node)

    def _placeholder(self) -> QTreeWidgetItem:
        node = QTreeWidgetItem(["加载中…", "", ""])
        node.setData(0, self._KIND, self._K_PLACEHOLDER)
        node.setFlags(Qt.ItemFlag.NoItemFlags)
        return node

    def _more_node(self, loaded: int, total: int) -> QTreeWidgetItem:
        node = QTreeWidgetItem([f"加载更多（已显示 {loaded} / {total}）", "", ""])
        node.setData(0, self._KIND, self._K_MORE)
        node.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return node

    def _on_tree_expanded(self, node: QTreeWidgetItem) -> None:
        if node.data(0, self._KIND) in (self._K_DOMAIN, self._K_ITEM):
            self._load_children(node)

    def _on_tree_clicked(self, node: QTreeWidgetItem, _column: int) -> None:
        if node.data(0, self._KIND) == self._K_MORE:
            parent = node.parent()
            if parent is not None:
                self._load_children(parent)
                self.tree.setCurrentItem(parent)

    def _load_children(self, node: QTreeWidgetItem) -> None:
        kind = node.data(0, self._KIND)
        if kind not in (self._K_DOMAIN, self._K_ITEM):
            return
        loaded = int(node.data(0, self._LOADED) or 0)
        has_sentinel = any(
            node.child(i).data(0, self._KIND) in (self._K_PLACEHOLDER, self._K_MORE)
            for i in range(node.childCount())
        )
        if not has_sentinel:
            return
        domain = str(node.data(0, self._DOMAIN) or "")
        parent_code = (
            "" if kind == self._K_DOMAIN else str(node.data(0, self._CODE) or "")
        )
        info = self._info
        if info is None:
            return
        names = {col.name for col in info.editable_columns}
        parent_expr = "COALESCE(parent_code, '')" if "parent_code" in names else "''"
        has_child_expr = (
            f"EXISTS (SELECT 1 FROM {_quote(self._table)} c "
            "WHERE c.domain = t.domain AND c.parent_code = t.code)"
            if "parent_code" in names
            else "0"
        )
        order = "t.sort_order, t.rowid" if "sort_order" in names else "t.rowid"
        db = local_db_util(self._db_path)
        try:
            total = int(
                db.cursor.execute(
                    f"SELECT COUNT(*) FROM {_quote(self._table)} t "
                    f"WHERE t.domain = ? AND {parent_expr} = ?",
                    (domain, parent_code),
                ).fetchone()[0]
            )
            rows = db.cursor.execute(
                f"SELECT t.rowid AS __rowid, t.code, t.name, "
                f"{parent_expr} AS parent_code, {has_child_expr} AS has_children "
                f"FROM {_quote(self._table)} t "
                f"WHERE t.domain = ? AND {parent_expr} = ? "
                f"ORDER BY {order} LIMIT ? OFFSET ?",
                (domain, parent_code, _ROW_LIMIT, loaded),
            ).fetchall()
        except Exception as exc:
            logger.warning("读取字典项失败：%s", exc, exc_info=True)
            QMessageBox.warning(self, "字典表", f"读取字典项失败：{exc}")
            return
        finally:
            db.conn.close()

        self._loading = True
        try:
            for index in reversed(range(node.childCount())):
                child = node.child(index)
                if child.data(0, self._KIND) in (self._K_PLACEHOLDER, self._K_MORE):
                    node.removeChild(child)
            for row in rows:
                child = QTreeWidgetItem([str(row["name"]), str(row["code"]), domain])
                child.setData(0, self._KIND, self._K_ITEM)
                child.setData(0, self._ROWID, int(row["__rowid"]))
                child.setData(0, self._DOMAIN, domain)
                child.setData(0, self._CODE, str(row["code"]))
                child.setData(0, self._NAME, str(row["name"]))
                child.setData(0, self._LOADED, 0)
                child.setFlags(
                    Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsEditable
                )
                if int(row["has_children"] or 0):
                    child.addChild(self._placeholder())
                node.addChild(child)
            loaded += len(rows)
            node.setData(0, self._LOADED, loaded)
            if loaded < total:
                node.addChild(self._more_node(loaded, total))
            node.setExpanded(True)
        finally:
            self._loading = False

    def _on_tree_double_clicked(self, node: QTreeWidgetItem, column: int) -> None:
        if node.data(0, self._KIND) == self._K_ITEM and column in (0, 1):
            self.tree.editItem(node, column)

    def _on_tree_item_changed(self, node: QTreeWidgetItem, column: int) -> None:
        if self._loading or column not in (0, 1):
            return
        if node.data(0, self._KIND) != self._K_ITEM:
            return
        rowid = node.data(0, self._ROWID)
        domain = str(node.data(0, self._DOMAIN) or "")
        old_code = str(node.data(0, self._CODE) or "")
        old_name = str(node.data(0, self._NAME) or "")
        new_value = node.text(column).strip()
        if not new_value:
            QMessageBox.warning(self, "字典表", "名称 / 代码不能为空")
            self._revert_tree_cell(node, column, old_name if column == 0 else old_code)
            return
        db = local_db_util(self._db_path)
        try:
            if column == 0:
                db.cursor.execute(
                    f'UPDATE {_quote(self._table)} SET name = ? WHERE rowid = ?',
                    (new_value, rowid),
                )
                self._loading = True
                try:
                    node.setData(0, self._NAME, new_value)
                finally:
                    self._loading = False
            else:
                exists = db.cursor.execute(
                    f'SELECT 1 FROM {_quote(self._table)} '
                    "WHERE domain = ? AND code = ? AND rowid <> ?",
                    (domain, new_value, rowid),
                ).fetchone()
                if exists is not None:
                    raise LocalTableError(
                        f"字典域「{domain}」里已存在代码「{new_value}」"
                    )
                names = {
                    col.name for col in (self._info.editable_columns if self._info else ())
                }
                if "parent_code" in names:
                    db.cursor.execute(
                        f'UPDATE {_quote(self._table)} SET parent_code = ? '
                        "WHERE domain = ? AND parent_code = ?",
                        (new_value, domain, old_code),
                    )
                db.cursor.execute(
                    f'UPDATE {_quote(self._table)} SET code = ? WHERE rowid = ?',
                    (new_value, rowid),
                )
                self._loading = True
                try:
                    node.setData(0, self._CODE, new_value)
                finally:
                    self._loading = False
            db.conn.commit()
        except LocalTableError as exc:
            db.conn.rollback()
            QMessageBox.warning(self, "字典表", str(exc))
            self._revert_tree_cell(node, column, old_name if column == 0 else old_code)
        except Exception as exc:  # noqa: BLE001
            db.conn.rollback()
            QMessageBox.warning(self, "字典表", f"保存失败：{exc}")
            self._revert_tree_cell(node, column, old_name if column == 0 else old_code)
        finally:
            db.conn.close()

    def _revert_tree_cell(self, node: QTreeWidgetItem, column: int, value: str) -> None:
        self._loading = True
        try:
            node.setText(column, value)
        finally:
            self._loading = False

    # ------------------------------------------------------------ 新增 / 删除

    def _selected_domain(self) -> str:
        node = self.tree.currentItem()
        if node is not None:
            domain = str(node.data(0, self._DOMAIN) or "")
            if domain:
                return domain
        if self.tree.topLevelItemCount():
            return str(self.tree.topLevelItem(0).data(0, self._DOMAIN) or "")
        return ""

    def _add_row(self) -> None:
        if self._info is None:
            return
        defaults = {"domain": self._selected_domain()} if self._canonical else None
        dialog = _RowDialog(self, self._info, defaults=defaults)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            insert_row(self._db_path, self._table, dialog.values())
        except LocalTableError as exc:
            QMessageBox.warning(self, "字典表", str(exc))
            return
        self._reload()
        self.dataChanged.emit(self._category, self._table)

    def _delete_selected(self) -> None:
        if self._canonical:
            nodes = self.tree.selectedItems()
            if not nodes:
                QMessageBox.information(self, "字典表", "请先选中要删除的字典域或字典项")
                return
            rowids: list[int] = []
            deleted_domains: list[str] = []
            db = local_db_util(self._db_path)
            try:
                for node in nodes:
                    kind = node.data(0, self._KIND)
                    domain = str(node.data(0, self._DOMAIN) or "")
                    if kind == self._K_DOMAIN:
                        if domain:
                            deleted_domains.append(domain)
                        rowids.extend(
                            int(row[0])
                            for row in db.cursor.execute(
                                f'SELECT rowid FROM {_quote(self._table)} WHERE domain = ?',
                                (domain,),
                            )
                        )
                    elif kind == self._K_ITEM:
                        rowids.extend(
                            self._subtree_rowids(
                                db, domain, str(node.data(0, self._CODE) or "")
                            )
                        )
            finally:
                db.conn.close()
            rowids = sorted(set(rowids))
            if not rowids:
                return
            detail = ""
            if deleted_domains:
                detail = (
                    "\n\n并会清除挂接到这些字典域的字段关联："
                    + "、".join(sorted(set(deleted_domains)))
                )
            ret = QMessageBox.question(
                self,
                "删除字典行",
                f"确定删除选中的 {len(rowids)} 行（含下级）吗？{detail}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            self._delete_rowids(rowids)
            if deleted_domains:
                self._drop_domains(sorted(set(deleted_domains)))
                self._reload()
            elif self._search_text:
                self._run_search()
            else:
                self._load_tree()
            self.dataChanged.emit(self._category, self._table)
            return

        # 平铺表沿用原有逻辑
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "字典表", "请先选中要删除的行")
            return
        keys = [self._key_for_row(row) for row in rows]
        keys = [key for key in keys if key]
        if not keys:
            return
        ret = QMessageBox.question(
            self,
            "删除字典行",
            f"确定删除选中的 {len(keys)} 行吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_rows(self._db_path, self._table, keys)
        except LocalTableError as exc:
            QMessageBox.warning(self, "字典表", str(exc))
            return
        self._reload()
        self.dataChanged.emit(self._category, self._table)

    def _subtree_rowids(self, db, domain: str, start_code: str) -> list[int]:
        if not start_code:
            return []
        names = {
            col.name for col in (self._info.editable_columns if self._info else ())
        }
        if "parent_code" not in names:
            row = db.cursor.execute(
                f'SELECT rowid FROM {_quote(self._table)} '
                "WHERE domain = ? AND code = ?",
                (domain, start_code),
            ).fetchone()
            return [int(row[0])] if row is not None else []
        rows = db.cursor.execute(
            f'SELECT rowid, code, COALESCE(parent_code, "") AS parent_code '
            f'FROM {_quote(self._table)} WHERE domain = ?',
            (domain,),
        ).fetchall()
        children: dict[str, list[str]] = {}
        by_code: dict[str, int] = {}
        for rowid, code, parent in rows:
            by_code[str(code)] = int(rowid)
            if parent:
                children.setdefault(str(parent), []).append(str(code))
        selected: list[int] = []
        stack = [start_code]
        seen: set[str] = set()
        while stack:
            code = stack.pop()
            if code in seen:
                continue
            seen.add(code)
            rowid = by_code.get(code)
            if rowid is not None:
                selected.append(rowid)
            stack.extend(children.get(code, []))
        return selected

    def _drop_domains(self, domains: list[str]) -> None:
        """整个字典域被删除后：清字段关联、更新注册信息里的域清单并通知调用方。

        只删了域里的部分条目时不会走到这里（域还在，字段挂接依然有效）；
        用户在树上删掉「字典域」节点，才意味着这个域不再存在。
        """
        from app.core.util.field_meta import clear_dict_domain_links

        category = (
            str((self._info.attr or {}).get("dict_table") or self._table)
            if self._info is not None
            else self._table
        )
        db = local_db_util(self._db_path)
        try:
            for domain in domains:
                clear_dict_domain_links(
                    db.conn,
                    domain=domain,
                    category=category,
                    local_table=self._table,
                )
            row = db.cursor.execute(
                "SELECT attr FROM user_relation WHERE table_name = ? AND type = 'dict'",
                (self._table,),
            ).fetchone()
            if row:
                try:
                    data = json.loads(row[0]) if row[0] else {}
                except (TypeError, ValueError):
                    data = {}
                if isinstance(data, dict):
                    removed = set(domains)
                    data["domains"] = [
                        str(item)
                        for item in (data.get("domains") or [])
                        if str(item) not in removed
                    ]
                    db.cursor.execute(
                        "UPDATE user_relation SET attr = ? "
                        "WHERE table_name = ? AND type = 'dict'",
                        (json.dumps(data, ensure_ascii=False), self._table),
                    )
            db.conn.commit()
        except Exception as exc:  # noqa: BLE001 —— 删除已经完成，元数据失败只记日志
            db.conn.rollback()
            logger.warning("删除字典域后清字段关联失败：%s（%s）", self._table, exc)
            return
        finally:
            db.conn.close()
        self.dictDomainsDeleted.emit(
            dict(self._db), category, self._table, list(domains)
        )

    def _delete_rowids(self, rowids: list[int]) -> None:
        db = local_db_util(self._db_path)
        try:
            for start in range(0, len(rowids), 400):
                chunk = rowids[start : start + 400]
                marks = ", ".join("?" for _ in chunk)
                db.cursor.execute(
                    f'DELETE FROM {_quote(self._table)} WHERE rowid IN ({marks})',
                    chunk,
                )
            db.conn.commit()
        except Exception as exc:  # noqa: BLE001
            db.conn.rollback()
            QMessageBox.warning(self, "字典表", f"删除失败：{exc}")
        finally:
            db.conn.close()

    # ------------------------------------------------------------ 搜索 / Excel

    def _run_search(self) -> None:
        """按代码 / 名称 / 字典域搜索当前本地字典表（最多 1000 条，防止大字典卡住）。"""
        keyword = self.search_edit.text().strip()
        if not keyword:
            if self._search_text:
                self._search_text = ""
                self._reload()
            return
        if not self._canonical or self._info is None:
            return
        self._search_text = keyword
        pattern = "%" + keyword + "%"
        db = local_db_util(self._db_path)
        try:
            rows = db.cursor.execute(
                f'SELECT rowid AS __rowid, domain, code, name FROM {_quote(self._table)} '
                "WHERE code LIKE ? OR name LIKE ? OR domain LIKE ? "
                "ORDER BY domain, code LIMIT 1000",
                (pattern, pattern, pattern),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            db.conn.close()
            QMessageBox.warning(self, "字典表", f"搜索失败：{exc}")
            return
        db.conn.close()
        # **保持原来的「字典域 → 字典项」结构**：先建域树，再隐藏不匹配的
        # 数据。这样搜「地貌」时域节点还在，可以直接选中后删掉整个域。
        self._load_tree()
        by_domain: dict[str, list] = {}
        for row in rows:
            by_domain.setdefault(str(row["domain"]), []).append(row)
        visible = 0
        self._loading = True
        try:
            for index in range(self.tree.topLevelItemCount()):
                node = self.tree.topLevelItem(index)
                if node.data(0, self._KIND) != self._K_DOMAIN:
                    node.setHidden(True)
                    continue
                domain = str(node.data(0, self._DOMAIN) or node.text(0))
                matched = by_domain.get(domain, [])
                name_matched = keyword.casefold() in domain.casefold()
                if not matched and not name_matched:
                    node.setHidden(True)
                    continue
                # 清掉分页占位：搜索时只显示匹配项，展开不再加载未过滤数据
                for child_index in reversed(range(node.childCount())):
                    child = node.child(child_index)
                    if child.data(0, self._KIND) in (
                        self._K_PLACEHOLDER,
                        self._K_MORE,
                    ):
                        node.removeChild(child)
                for row in matched:
                    child = QTreeWidgetItem(
                        [str(row["name"]), str(row["code"]), str(row["domain"])]
                    )
                    child.setData(0, self._KIND, self._K_ITEM)
                    child.setData(0, self._ROWID, int(row["__rowid"]))
                    child.setData(0, self._DOMAIN, str(row["domain"]))
                    child.setData(0, self._CODE, str(row["code"]))
                    child.setData(0, self._NAME, str(row["name"]))
                    child.setData(0, self._LOADED, 0)
                    child.setFlags(
                        Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsEditable
                    )
                    node.addChild(child)
                node.setHidden(False)
                if node.childCount():
                    node.setExpanded(True)
                visible += 1
            self.tree.setCurrentItem(None)
        finally:
            self._loading = False
        if visible:
            self.info_label.setText(
                f"搜索「{keyword}」：命中 {visible} 个字典域、{len(rows)} 条字典项"
                "（已隐藏其它数据）"
            )
        else:
            self.info_label.setText(f"没有找到匹配「{keyword}」的字典域 / 字典项")

    def _on_export_dict(self) -> None:
        if not self._canonical:
            QMessageBox.warning(self, "导出字典", "这张表不是标准字典表结构")
            return
        path = save_file(
            self,
            title="导出字典表",
            init_path=str(Path.home() / f"{self._table}.xls"),
            filter_="Excel 97-2003 工作簿 (*.xls);;所有文件 (*)",
        )
        if not path:
            return
        from app.core.domain.local_dict_excel import (
            LocalDictExcelError,
            export_local_dict_xls,
        )

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            written, count = export_local_dict_xls(path, self._db_path, self._table)
        except LocalDictExcelError as exc:
            QMessageBox.warning(self, "导出字典", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(
            self, "导出字典", f"已导出 {count} 条字典项到：{written}"
        )

    def _on_import_dict(self) -> None:
        if not self._canonical:
            QMessageBox.warning(self, "导入字典", "这张表不是标准字典表结构")
            return
        path = open_file_dir(
            self,
            is_dir=False,
            title="Excel 导入字典",
            init_path=str(Path.home()),
            filter_="Excel 字典 (*.xls *.xlsx);;所有文件 (*)",
        )
        if not path:
            return
        ret = QMessageBox.question(
            self,
            "导入字典",
            "导入前是否清空当前字典表的已有条目？",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Cancel:
            return
        from app.core.domain.local_dict_excel import (
            LocalDictExcelError,
            import_local_dict_xls,
        )

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = import_local_dict_xls(
                path,
                self._db_path,
                self._table,
                replace=ret == QMessageBox.StandardButton.Yes,
            )
        except LocalDictExcelError as exc:
            QMessageBox.warning(self, "导入字典", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(
            self,
            "导入字典",
            f"读取 {report.rows_total} 行，新增 {report.inserted} 条，"
            f"更新 {report.updated} 条，跳过 {report.skipped} 行",
        )
        self._reload()
        self.dataChanged.emit(self._category, self._table)

    # ------------------------------------------------------------ 平铺表（非 canonical）

    def _reload_table(self) -> None:
        try:
            page = read_rows(
                self._db_path, self._table, offset=self._offset, limit=_ROW_LIMIT
            )
        except LocalTableError as exc:
            self.info_label.setText(str(exc))
            self.table.setRowCount(0)
            return
        self._total = page.total
        self._rows = list(page.rows)
        info = self._info
        visible = [
            col.name
            for col in (info.editable_columns if info is not None else ())
            if col.name in (page.columns or ())
        ]
        if not visible:
            visible = [name for name in page.columns if name != "__rowid"]
        self._columns = visible
        self._render_table()
        self._update_page_label()

    def _render_table(self) -> None:
        self._loading = True
        try:
            self.table.setColumnCount(len(self._columns))
            self.table.setHorizontalHeaderLabels(self._columns)
            self.table.setRowCount(len(self._rows))
            for row_index, row in enumerate(self._rows):
                for column, name in enumerate(self._columns):
                    item = QTableWidgetItem(
                        "" if row.get(name) is None else str(row.get(name))
                    )
                    self.table.setItem(row_index, column, item)
        finally:
            self._loading = False

    def _update_page_label(self) -> None:
        if self._canonical:
            return
        start = self._offset + 1 if self._total else 0
        end = min(self._offset + _ROW_LIMIT, self._total)
        self.page_label.setText(f"{start}-{end} / {self._total}")
        self.prev_btn.setEnabled(self._offset > 0)
        self.next_btn.setEnabled(self._offset + _ROW_LIMIT < self._total)

    def _page(self, delta: int) -> None:
        self._offset = max(0, self._offset + delta * _ROW_LIMIT)
        self._reload_table()

    def _key_for_row(self, row_index: int) -> dict:
        if not (0 <= row_index < len(self._rows)):
            return {}
        row = self._rows[row_index]
        info = self._info
        if info is None:
            return {}
        if info.uses_rowid:
            return {"__rowid": row.get("__rowid")}
        return {name: row.get(name) for name in info.pk_columns}

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row_index, column = item.row(), item.column()
        if not (0 <= column < len(self._columns)):
            return
        name = self._columns[column]
        key = self._key_for_row(row_index)
        if not key:
            return
        value = item.text()
        try:
            update_row(self._db_path, self._table, key, {name: value})
        except LocalTableError as exc:
            QMessageBox.warning(self, "字典表", str(exc))
            self._reload_table()
            return
        self._rows[row_index][name] = value


class TableDetailPage(QWidget):
    """右栏详情容器：按 category 切到矢量字段编辑器 / 字典行编辑器。"""

    backRequested = pyqtSignal()  # noqa: N815 —— Qt 信号命名
    dataChanged = pyqtSignal(str, str)  # noqa: N815

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.vector = VectorFieldEditor(self)
        self.dictionary = DictTableEditor(self)
        self.stack.addWidget(self.vector)
        self.stack.addWidget(self.dictionary)
        layout.addWidget(self.stack, 1)
        self.vector.backRequested.connect(self.backRequested.emit)
        self.dictionary.backRequested.connect(self.backRequested.emit)
        self.vector.dataChanged.connect(self.dataChanged.emit)
        self.dictionary.dataChanged.connect(self.dataChanged.emit)

    def set_before_change(self, callback) -> None:
        self.vector.set_before_change(callback)

    def show_table(self, db: dict, category: str, table: str) -> None:
        if category == "dict":
            self.dictionary.load(db, table)
            self.stack.setCurrentWidget(self.dictionary)
        else:
            self.vector.load(db, table)
            self.stack.setCurrentWidget(self.vector)


def _local_dict_tables(path: str) -> dict[str, str]:
    """``{字典表名: 库内表名}``（保存字段字典前判断本地是否已有该字典表）。"""
    try:
        db = local_db_util(path)
    except Exception:  # noqa: BLE001
        return {}
    try:
        return _dict_tables_by_category(db)
    finally:
        db.conn.close()


def _dict_tables_by_category(db) -> dict[str, str]:
    """``{字典表名: 库内字典表名}``（字段元数据里记本地字典表用）。"""
    out: dict[str, str] = {}
    try:
        rows = db.cursor.execute(
            "SELECT table_name, attr FROM user_relation WHERE type = 'dict'"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return out
    for table, attr in rows:
        try:
            data = json.loads(attr) if attr else {}
        except (TypeError, ValueError):
            continue
        category = str((data or {}).get("dict_table") or "")
        if category and category not in out:
            out[category] = str(table)
    return out


class _SuppressDbErrors:
    """忽略「别名表不存在」这类无害 SQL 错误。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None
