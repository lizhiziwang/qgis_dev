"""创建数据向导第二步「数据字段设置」整页（含「外部数据集」选择弹窗）。

对话框（``create_data_dialog``）把这一页塞进「单图层数据」页签的步骤栈里当第 2 页，
自己只管向导外壳（步骤切换 + 页脚按钮 + 校验串联）；**字段表格、工具条、结构来源
这一整块都在这里**，免得那个已经 1000 多行的对话框继续膨胀（见编码规范 §0.3）。

结构照原型 ``code01/app.js`` 的 ``cdStep2Html`` 搭：标题、工具条（字段筛选 / 搜索 /
查找 / 清除 / 删除选中字段 / 清空字段列表）、九列表格、结构来源行。文案逐字沿用原型：

- 标题：``数据字段设置：请设置新建数据的数据字段结构``
- 空表：``暂无字段，可通过"导入"或双击下方表格添加``
- 底部提示：``双击单元格编辑 · 回车/失焦确认 · 方向键切换 · Delete 删除选中行 · 右键菜单删除``

**表格就是唯一真相**：``fields()`` 每次现从表格里读一遍（解析类型下拉、勾选框、文本
单元格），增删行一律「读出来 → 改列表 → 整表重填」。只有一份状态，就不会出现
「列表说 5 个字段、表格显示 4 行」这类对不上的 bug（参看项目里那条
「状态 bug 先找单一来源」的经验）。

本页**不做 IO**：模板读写、图层字段结构转换都是 ``app.core.domain.create_data`` 的事，
这里只负责把结果摆进表格、把失败原因交给 :attr:`statusMessage` 让对话框去提示。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from app.core.domain.create_data import (
    FIELD_TYPES,
    MAX_FIELD_LENGTH,
    MAX_FIELD_PRECISION,
    TEMPLATE_EXTENSION,
    TEMPLATE_FILTER,
    TEMPLATE_READ_FILTER,
    FieldSpec,
    FieldTemplateError,
    fields_from_layer,
    read_template,
    to_int,
    write_template,
    xlsx_available,
)
from app.core.domain.create_data.dict_tables import (
    DictPlan,
    DictSource,
    source_dict_tables,
)
from app.core.util.sublayers import (
    VECTOR_FILE_FILTER,
    Sublayer,
    geometry_kind_of_layer,
    list_sublayers,
    open_sublayer,
)
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import CenteredCheckDelegate, open_file_dir, save_file
from app.ui.dialogs.dict_pick_dialog import DictPickDialog
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

#: 列下标（顺序与原型一致；第 0 列是「选中标记」，没有表头文字）
(
    _COL_MARK,
    _COL_NAME,
    _COL_ALIAS,
    _COL_TYPE,
    _COL_LENGTH,
    _COL_PRECISION,
    _COL_DICT_TABLE,
    _COL_DICT_DOMAIN,
    _COL_NULLABLE,
    _COL_VISIBLE,
) = range(10)

#: 表头（第 0 列留空 —— 它是选中标记列）
_HEADERS: tuple[str, ...] = (
    "",
    "字段名",
    "字段别名",
    "字段类型",
    "字段长度",
    "小数位数",
    "字典表",
    "字典域",
    "是否可为空",
    "是否可见",
)

#: 可键盘编辑的文本列
_TEXT_COLUMNS: tuple[int, ...] = (
    _COL_NAME,
    _COL_ALIAS,
    _COL_LENGTH,
    _COL_PRECISION,
    _COL_DICT_TABLE,
    _COL_DICT_DOMAIN,
)

#: 「字段真实顺序」存在字段名单元格上；表头排序只改显示，不改新建图层的字段顺序
_ROLE_ORDER = int(Qt.ItemDataRole.UserRole) + 30

#: 勾选列（是否可为空 / 是否可见）
_CHECK_COLUMNS: tuple[int, ...] = (_COL_NULLABLE, _COL_VISIBLE)

#: 各列默认宽度（像素）；「字段别名」列设 Stretch 吃掉剩余宽度。
#: **固定列要留够「别名」的份**：这些数加起来加上最小别名宽度不能超过对话框的内容宽度，
#: 否则默认大小下就冒出横向滚动条，别名列被挤成一条缝（用户报过）。字段别名是
#: 中文名，最需要宽度的一列，所以让它 Stretch、其余列按「够显示表头 + 一点余量」给。
_COLUMN_WIDTHS: dict[int, int] = {
    _COL_MARK: 32,
    _COL_NAME: 120,
    _COL_TYPE: 96,
    _COL_LENGTH: 60,
    _COL_PRECISION: 60,
    _COL_DICT_TABLE: 88,
    _COL_DICT_DOMAIN: 70,
    _COL_NULLABLE: 84,
    _COL_VISIBLE: 72,
}

#: 字段别名列的最小宽度（Stretch 列也得有下限，否则窗口一小它先被压没）
_ALIAS_MIN_WIDTH = 150

#: 空表时那一行的提示文案（逐字照抄原型）
_EMPTY_TEXT = '暂无字段，可通过"导入"或双击下方表格添加'

#: 底部操作提示（逐字照抄原型）
_HINT_TEXT = (
    "双击单元格编辑 · 双击「字典表 / 字典域」选择字典 · 回车/失焦确认 · "
    "方向键切换 · Delete 删除选中行 · 右键菜单删除"
)

#: 几何类别的中文名（按类型筛数据源的提示用）
_KIND_LABELS: dict[str, str] = {"point": "点", "line": "线", "polygon": "面"}


class _FieldsTable(QTableWidget):
    """字段表：只多一件事 —— Delete 键删行（其余导航 / 编辑交给 QTableWidget 原生行为）。"""

    #: 用户按了 Delete
    deletePressed = pyqtSignal()  # noqa: N815 —— Qt 信号命名

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)


class FieldsStepPage(QWidget):
    """第二步整页：字段结构表格 + 工具条 + 结构来源。"""

    #: 字段表内容变了（对话框据此刷新底部提示）
    fieldsChanged = pyqtSignal()  # noqa: N815
    #: 要显示给用户的一句话（对话框负责展示；本页不自己弹窗/发消息条）
    statusMessage = pyqtSignal(str)  # noqa: N815

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        vector_layers: Sequence[object] = (),
        default_dir: str = "",
        target_db_path: Callable[[], str] | None = None,
    ) -> None:
        """
        Args:
            parent: 父控件。
            vector_layers: 工程里已加载的矢量图层（「结构来源 → 外部数据集」用；
                由对话框从主窗口注入，本页不自己去问 ``QgsProject``）。
            default_dir: 「生成空模板文件」保存对话框的起始目录。
            target_db_path: 当前创建目标（库文件 / 待建文件）路径的取值函数。
                字典挂接只列**该库里的字典表**，所以每次打开选择器现取一遍
                （用户在向导里切目标库后不用重建本页）。
        """
        super().__init__(parent)
        #: 空表时表格里那一行是占位行（不是字段）—— ``fields()`` 要把它跳过
        self._placeholder = True
        #: 正在重填表格：期间的 ``itemChanged`` 一律忽略（重填本身就会发一堆）
        self._loading = False
        self._vector_layers = list(vector_layers)
        self._default_dir = default_dir
        self._target_db_path = target_db_path
        #: 外部数据集源库里匹配到的 dict 表（创建时一并落进目标库）
        self._source_dicts: list[DictSource] = []
        self._build()

    # --------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(8)

        title = QLabel("数据字段设置：请设置新建数据的数据字段结构")
        title.setObjectName("SectionLabel")
        root.addWidget(title)

        root.addLayout(self._build_toolbar())
        root.addWidget(self._build_table(), 1)
        root.addLayout(self._build_import_row())

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.filter_check = QPushButton("字段筛选")
        self.filter_check.setCheckable(True)
        self.filter_check.setToolTip("勾上之后，搜索框里输入的文字用来隐藏不匹配的字段行")
        self.filter_check.toggled.connect(self._apply_filter)
        row.addWidget(self.filter_check)
        row.addWidget(QLabel("搜索"))

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索字段名/别名…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        self.search_edit.returnPressed.connect(self._on_find)
        row.addWidget(self.search_edit, 1)

        find_btn = QPushButton("查找")
        find_btn.setToolTip("跳到第一个匹配的行（勾上「字段筛选」只显示匹配行）")
        find_btn.clicked.connect(self._on_find)
        row.addWidget(find_btn)
        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(lambda: self.search_edit.setText(""))
        row.addWidget(clear_btn)

        row.addStretch(1)
        self.delete_btn = QPushButton("删除选中字段")
        self.delete_btn.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_btn)
        self.clear_btn = QPushButton("清空字段列表")
        self.clear_btn.clicked.connect(lambda: self.set_fields([]))
        row.addWidget(self.clear_btn)
        return row

    def _build_table(self) -> _FieldsTable:
        self.table = _FieldsTable(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._refresh_marks)
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.table.deletePressed.connect(self.delete_selected)
        # 勾选框列去掉焦点框、居中显示（与「字段」属性页同一套观感）
        self.table.setItemDelegate(CenteredCheckDelegate(self.table))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(32)
        header.setStretchLastSection(False)
        # 点击表头临时排序（只改显示；fields() 仍按隐藏的真实顺序返回）
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sectionClicked.connect(self._on_header_sort)
        self._sort_column = -1
        self._sort_ascending = True
        for column, width in _COLUMN_WIDTHS.items():
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(_COL_ALIAS, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(_COL_ALIAS, _ALIAS_MIN_WIDTH)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # 表头文字不省略、让列宽自己适配内容（中文表头被截成 "字段…" 很难认）
        header.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.set_fields([])
        return self.table

    def _build_import_row(self) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(QLabel("结构来源："))
        self.template_radio = QRadioButton("模板文件(XLS)")
        self.template_radio.setChecked(True)
        row.addWidget(self.template_radio)
        self.dataset_radio = QRadioButton("外部数据集")
        row.addWidget(self.dataset_radio)
        import_btn = QPushButton("导入")
        import_btn.setToolTip("按所选的「结构来源」导入字段结构（会替换当前整张字段表）")
        import_btn.clicked.connect(self._on_import)
        row.addWidget(import_btn)

        self.template_btn = QPushButton("生成空模板文件")
        if xlsx_available():
            self.template_btn.setToolTip("导出一份只有表头的 Excel 模板（.xlsx），填好后用「导入」读回来")
        else:
            # 本机 GDAL 没有可写的 XLSX 驱动：灰着说清楚，别让用户点了才报错
            self.template_btn.setEnabled(False)
            self.template_btn.setToolTip("本机 QGIS 没有可写 Excel（XLSX）的驱动，生成不了模板文件")
        self.template_btn.clicked.connect(self._on_generate_template)
        row.addWidget(self.template_btn)
        row.addStretch(1)
        box.addLayout(row)

        hint = QLabel(_HINT_TEXT)
        hint.setObjectName("PropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        box.addWidget(hint)
        return box

    # --------------------------------------------------------------- 取值 / 回填

    def fields(self) -> list[FieldSpec]:
        """当前表格里的字段结构（空表返回空表）。

        表头排序只改**显示顺序**；这里按隐藏的「真实顺序」返回，保证新建图层的
        字段顺序不受临时排序影响。
        """
        if self._placeholder:
            return []
        entries = [
            (self._order_at(row), self._spec_at(row))
            for row in range(self.table.rowCount())
        ]
        entries.sort(key=lambda pair: pair[0])
        return [spec for _order, spec in entries]

    def _order_at(self, row: int) -> int:
        item = self.table.item(row, _COL_NAME)
        if item is None:
            return row
        value = item.data(_ROLE_ORDER)
        return int(value) if value is not None else row

    def _on_header_sort(self, column: int) -> None:
        """表头点击：按该列临时排序（只改显示，fields() 的真实顺序不变）。"""
        if self._placeholder or column < 0 or column >= len(_HEADERS):
            return
        if column == _COL_MARK:
            return
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        entries = [
            (self._order_at(row), self._spec_at(row))
            for row in range(self.table.rowCount())
        ]
        entries.sort(
            key=lambda pair: self._sort_spec_key(pair[1], column),
            reverse=not self._sort_ascending,
        )
        self._loading = True
        try:
            self.table.clearSpans()
            self.table.setRowCount(0)
            self.table.setRowCount(len(entries))
            for row, (order, spec) in enumerate(entries):
                self._fill_row(row, spec, order=order)
        finally:
            self._loading = False
        self.table.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder
            if self._sort_ascending
            else Qt.SortOrder.DescendingOrder,
        )
        self._refresh_marks()
        self._apply_filter()

    @staticmethod
    def _sort_spec_key(spec: FieldSpec, column: int):
        if column == _COL_NAME:
            return spec.name.casefold()
        if column == _COL_ALIAS:
            return spec.alias.casefold()
        if column == _COL_TYPE:
            return next(
                (item.label for item in FIELD_TYPES if item.key == spec.type_key),
                spec.type_key,
            ).casefold()
        if column == _COL_LENGTH:
            return spec.length
        if column == _COL_PRECISION:
            return spec.precision
        if column == _COL_DICT_TABLE:
            return spec.dict_table.casefold()
        if column == _COL_DICT_DOMAIN:
            return spec.dict_domain.casefold()
        if column == _COL_NULLABLE:
            return spec.nullable
        if column == _COL_VISIBLE:
            return spec.visible
        return ""

    def dict_plan(self) -> DictPlan:
        """字段表涉及的字典需求：应用级 ``(字典表, 字典域)`` + 外部源库 dict 表。"""
        fields = self.fields()
        domains = frozenset(
            (field.dict_table.strip(), field.dict_domain.strip())
            for field in fields
            if field.dict_table.strip() and field.dict_domain.strip()
        )
        sources = tuple(
            source
            for source in self._source_dicts
            if (source.category.strip(), source.domain.strip()) in domains
        )
        return DictPlan(domains=domains, sources=sources)

    def set_fields(self, fields: Sequence[FieldSpec]) -> None:
        """整表替换（导入 / 删除 / 清空都走这里）。"""
        self._placeholder = not fields
        self._loading = True
        try:
            # 空表那行是跨列的（setSpan）：不先清掉，后面填真字段时合并还在，表格会错位
            self.table.clearSpans()
            self.table.setRowCount(0)
            if not fields:
                self._show_placeholder()
            else:
                self.table.setRowCount(len(fields))
                for row, spec in enumerate(fields):
                    self._fill_row(row, spec, order=row)
        finally:
            self._loading = False
        # 结构变了：回到真实顺序（临时排序只影响查看）
        self._sort_column = -1
        self._sort_ascending = True
        self.table.horizontalHeader().setSortIndicator(
            -1, Qt.SortOrder.AscendingOrder
        )
        self._apply_filter()
        self.fieldsChanged.emit()

    def focus_table(self) -> None:
        """把焦点给表格（校验失败时对话框用来指位置）。"""
        self.table.setFocus()

    def _show_placeholder(self) -> None:
        """空表：一行跨列的灰字提示（双击它就开始加字段）。"""
        self.table.setRowCount(1)
        item = QTableWidgetItem(_EMPTY_TEXT)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setForeground(QBrush(self.palette().placeholderText()))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(0, _COL_MARK, item)
        self.table.setSpan(0, _COL_MARK, 1, len(_HEADERS))

    def _fill_row(self, row: int, spec: FieldSpec, *, order: int | None = None) -> None:
        """把一条 :class:`FieldSpec` 填进第 ``row`` 行。"""
        mark = QTableWidgetItem("")
        mark.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, _COL_MARK, mark)

        for column, text in (
            (_COL_NAME, spec.name),
            (_COL_ALIAS, spec.alias),
            (_COL_LENGTH, str(spec.length) if spec.length else ""),
            (_COL_PRECISION, str(spec.precision) if spec.precision else ""),
            (_COL_DICT_TABLE, spec.dict_table),
            (_COL_DICT_DOMAIN, spec.dict_domain),
        ):
            item = QTableWidgetItem(text)
            if column in (_COL_DICT_TABLE, _COL_DICT_DOMAIN):
                # 字典两列只能经选择器写入，别让 F2 / 编辑框乱填
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
            if column == _COL_NAME:
                item.setData(_ROLE_ORDER, row if order is None else order)
            self.table.setItem(row, column, item)

        combo = QComboBox()
        for field_type in FIELD_TYPES:
            combo.addItem(field_type.label, field_type.key)
        index = combo.findData(spec.type_key)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(lambda _i, r=row: self._on_type_changed(r))
        self.table.setCellWidget(row, _COL_TYPE, combo)

        for column, checked in ((_COL_NULLABLE, spec.nullable), (_COL_VISIBLE, spec.visible)):
            item = QTableWidgetItem()
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.table.setItem(row, column, item)

    def _spec_at(self, row: int) -> FieldSpec:
        """把第 ``row`` 行读成 :class:`FieldSpec`（表格是唯一真相，见模块头）。"""

        def text(column: int) -> str:
            item = self.table.item(row, column)
            return item.text().strip() if item is not None else ""

        def checked(column: int) -> bool:
            item = self.table.item(row, column)
            return item is not None and item.checkState() == Qt.CheckState.Checked

        combo = self.table.cellWidget(row, _COL_TYPE)
        type_key = combo.currentData() if isinstance(combo, QComboBox) else None
        return FieldSpec(
            name=text(_COL_NAME),
            alias=text(_COL_ALIAS),
            type_key=str(type_key or "text"),
            length=to_int(text(_COL_LENGTH), MAX_FIELD_LENGTH),
            precision=to_int(text(_COL_PRECISION), MAX_FIELD_PRECISION),
            dict_table=text(_COL_DICT_TABLE),
            dict_domain=text(_COL_DICT_DOMAIN),
            nullable=checked(_COL_NULLABLE),
            visible=checked(_COL_VISIBLE),
        )

    # --------------------------------------------------------------- 表格交互

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """单元格改完（回车 / 失焦）后同步一次 —— 只影响提示，结构不用回写。"""
        if self._loading or self._placeholder:
            return
        if item.column() not in (*_TEXT_COLUMNS, *_CHECK_COLUMNS):
            return
        self._refresh_marks()
        self.fieldsChanged.emit()

    def _on_type_changed(self, row: int) -> None:
        """切换类型：顺手把空的「长度 / 小数位数」填成该类型的默认值。"""
        if self._loading or self._placeholder:
            return
        spec = self._spec_at(row)
        filled = spec
        if not spec.length and spec.field_type.default_length:
            filled = replace(filled, length=spec.field_type.default_length)
        if not spec.precision and spec.field_type.default_precision:
            filled = replace(filled, precision=spec.field_type.default_precision)
        if filled is not spec:
            previous = self._loading
            self._loading = True
            try:
                length_item = self.table.item(row, _COL_LENGTH)
                if length_item is not None:
                    length_item.setText(str(filled.length) if filled.length else "")
                precision_item = self.table.item(row, _COL_PRECISION)
                if precision_item is not None:
                    precision_item.setText(str(filled.precision) if filled.precision else "")
            finally:
                self._loading = previous
        # 类型改成非「字典」时，字典关联一并清掉，避免留下对不上的元数据
        if spec.type_key != "dict":
            previous = self._loading
            self._loading = True
            try:
                for column in (_COL_DICT_TABLE, _COL_DICT_DOMAIN):
                    item = self.table.item(row, column)
                    if item is not None:
                        item.setText("")
            finally:
                self._loading = previous
        self.fieldsChanged.emit()

    def _append_field(self) -> None:
        """在末尾加一个空字段行并开始编辑（空表时的双击 / 右键「添加字段」）。"""
        fields = self.fields()
        fields.append(FieldSpec(name="", type_key="text", length=50, precision=0))
        self.set_fields(fields)
        self._reveal_row(len(fields) - 1)

    def _reveal_row(self, row: int) -> None:
        """把刚加的那一行露出来并进入编辑。

        **筛选开着的时候新行一定是「不匹配」的**（名字还空着），不显式放开就一直藏着 ——
        用户看着像没加上，接着却被「存在空字段名」拦住，而那一行点都点不到。
        """
        item = self.table.item(row, _COL_NAME)
        if item is None:
            return
        self.table.setRowHidden(row, False)
        self.table.setCurrentCell(row, _COL_NAME)
        self.table.editItem(item)

    def _on_double_clicked(self, index) -> None:
        """双击：空表行 = 加字段；字典列 = 选择字典；其余文本列 = 内联编辑。"""
        if self._placeholder:
            self._append_field()
            return
        row, column = index.row(), index.column()
        if column in (_COL_DICT_TABLE, _COL_DICT_DOMAIN):
            self._pick_dict(row)
            return
        if column in _CHECK_COLUMNS:
            item = self.table.item(row, column)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
            return
        item = self.table.item(row, column)
        if item is not None and column in _TEXT_COLUMNS:
            self.table.editItem(item)

    def _target_path(self) -> str:
        """当前创建目标库 / 文件的路径（没有可挂字典的库时返回空串）。"""
        if self._target_db_path is None:
            return ""
        try:
            return str(self._target_db_path() or "")
        except Exception:  # noqa: BLE001 —— 取路径失败按"没有库"处理
            return ""

    def _pick_dict(self, row: int) -> None:
        """打开字典选择器；确定后同时回填「字典表 / 字典域」并把类型切成字典。

        选择器**只列目标库里的字典表**（见 :class:`DictPickDialog`）；当前库
        还没有字典时，可以在选择器里现场创建 / 从系统字典或外部文件导入。
        """
        table_item = self.table.item(row, _COL_DICT_TABLE)
        domain_item = self.table.item(row, _COL_DICT_DOMAIN)
        name_item = self.table.item(row, _COL_NAME)
        alias_item = self.table.item(row, _COL_ALIAS)
        current_table = table_item.text().strip() if table_item is not None else ""
        current_domain = domain_item.text().strip() if domain_item is not None else ""
        dialog = DictPickDialog(
            self,
            category=current_table,
            domain=current_domain,
            field_name=name_item.text().strip() if name_item is not None else "",
            field_alias=alias_item.text().strip() if alias_item is not None else "",
            local_db_path=self._target_path(),
        )
        accepted = dialog.exec()
        selected = dialog.selected() if accepted else None
        dialog.deleteLater()
        if not accepted:
            return
        if selected is None:
            self._set_dict_cells(row, "", "")
            self.statusMessage.emit("已清空该字段的字典关联")
            return
        category, domain = selected
        self._set_dict_cells(row, category, domain)
        self.statusMessage.emit(f"已选择字典：{category} / {domain}")

    def _clear_dict(self, row: int) -> None:
        self._set_dict_cells(row, "", "")
        self.statusMessage.emit("已清空该字段的字典关联")

    def _set_dict_cells(self, row: int, category: str, domain: str) -> None:
        """程序化回填字典两列（期间不触发 itemChanged 的重复校验）。"""
        previous = self._loading
        self._loading = True
        try:
            for column, text in (
                (_COL_DICT_TABLE, category),
                (_COL_DICT_DOMAIN, domain),
            ):
                item = self.table.item(row, column)
                if item is not None:
                    item.setText(text)
            if not category and not domain:
                combo = self.table.cellWidget(row, _COL_TYPE)
                if isinstance(combo, QComboBox) and combo.currentData() == "dict":
                    text_index = combo.findData("text")
                    if text_index >= 0:
                        combo.setCurrentIndex(text_index)
            if category and domain:
                combo = self.table.cellWidget(row, _COL_TYPE)
                if isinstance(combo, QComboBox):
                    index = combo.findData("dict")
                    if index >= 0:
                        combo.setCurrentIndex(index)
                length_item = self.table.item(row, _COL_LENGTH)
                if length_item is not None and not length_item.text().strip():
                    length_item.setText("20")
                precision_item = self.table.item(row, _COL_PRECISION)
                if precision_item is not None and not precision_item.text().strip():
                    precision_item.setText("0")
        finally:
            self._loading = previous
        self._refresh_marks()
        self.fieldsChanged.emit()

    def _on_context_menu(self, pos) -> None:
        """右键菜单（原型：右键菜单删除）。"""
        row = self.table.rowAt(pos.y())
        menu = QMenu(self)
        if self._placeholder or row < 0:
            menu.addAction("添加字段", self._append_field)
        else:
            menu.addAction("在前面插入一行", lambda: self._insert_row(row))
            menu.addAction("在后面插入一行", lambda: self._insert_row(row + 1))
            menu.addSeparator()
            menu.addAction("选择字典…", lambda: self._pick_dict(row))
            menu.addAction("清空字典", lambda: self._clear_dict(row))
            menu.addSeparator()
            menu.addAction("删除本行", lambda: self._delete_rows({row}))
            menu.addAction("删除选中字段", self.delete_selected)
            menu.addSeparator()
            menu.addAction("清空字段列表", lambda: self.set_fields([]))
        menu.exec(self.table.viewport().mapToGlobal(pos))
        # 每次右键都新建一个菜单，用完交给事件循环回收（挂在 self 上不会自己消失）
        menu.deleteLater()

    def _insert_row(self, row: int) -> None:
        """在第 ``row`` 行前插一个空字段（排序状态下按真实顺序定位）。"""
        entries = sorted(
            (self._order_at(index), self._spec_at(index))
            for index in range(self.table.rowCount())
        )
        fields = [spec for _order, spec in entries]
        if 0 <= row < self.table.rowCount():
            target_order = self._order_at(row)
            index = next(
                (
                    position
                    for position, (order, _spec) in enumerate(entries)
                    if order == target_order
                ),
                len(fields),
            )
        else:
            index = len(fields)
        fields.insert(index, FieldSpec(name="", type_key="text", length=50, precision=0))
        self.set_fields(fields)
        self._reveal_row(index)

    def delete_selected(self) -> None:
        """「删除选中字段」（工具条按钮 / Delete 键 / 右键菜单共用）。"""
        if self._placeholder:
            self.statusMessage.emit("还没有字段行可删")
            return
        rows = {index.row() for index in self.table.selectedIndexes() if index.row() >= 0}
        if not rows:
            self.statusMessage.emit("请先选中要删除的字段行（点行首，或按住 Ctrl 多选）")
            return
        self._delete_rows(rows)

    def _delete_rows(self, rows: set[int]) -> None:
        delete_orders = {
            self._order_at(row)
            for row in rows
            if 0 <= row < self.table.rowCount()
        }
        entries = sorted(
            (self._order_at(index), self._spec_at(index))
            for index in range(self.table.rowCount())
        )
        kept = [
            spec for order, spec in entries if order not in delete_orders
        ]
        self.set_fields(kept)
        self.statusMessage.emit(f"已删除 {len(delete_orders)} 行字段")

    def _refresh_marks(self) -> None:
        """把「选中标记」列画成当前选中行（原型第 0 列就是这个作用）。

        **``_loading`` 要「存了再还原」，不能直接置 False**：本方法会在 ``set_fields``
        填表的过程中被调到（``setRowCount(0)`` 会丢选区 → ``itemSelectionChanged``），
        直接置 False 就把外层那道「正在重填」的闸门拆掉了 —— 之后每个 ``setItem`` 都触发
        ``itemChanged`` → ``fieldsChanged`` → 对话框整表重校验。实测 200 个字段删一行
        要 5.8 秒、跑 1593 次全量校验，就是这里漏的。
        """
        if self._placeholder:
            return
        selected = {index.row() for index in self.table.selectedIndexes()}
        previous = self._loading
        self._loading = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, _COL_MARK)
                if item is not None:
                    item.setText("✓" if row in selected else "")
        finally:
            self._loading = previous

    # --------------------------------------------------------------- 筛选

    def _apply_filter(self, *_args: object) -> None:
        """「字段筛选」勾上时按搜索词隐藏不匹配的行（原型同名行为）。"""
        if self._placeholder:
            return
        keyword = self.search_edit.text().strip().lower()
        enabled = self.filter_check.isChecked()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, _COL_NAME)
            alias_item = self.table.item(row, _COL_ALIAS)
            text = f"{name_item.text() if name_item else ''} {alias_item.text() if alias_item else ''}".lower()
            self.table.setRowHidden(row, enabled and bool(keyword) and keyword not in text)

    def _on_find(self) -> None:
        """「查找」：跳到第一个匹配行（并把它选中，方便接着删）。"""
        keyword = self.search_edit.text().strip().lower()
        if not keyword:
            self.statusMessage.emit("请先在搜索框里输入要查找的字段名或别名")
            return
        for row in range(self.table.rowCount()):
            if self._placeholder:
                break
            name_item = self.table.item(row, _COL_NAME)
            alias_item = self.table.item(row, _COL_ALIAS)
            text = f"{name_item.text() if name_item else ''} {alias_item.text() if alias_item else ''}".lower()
            if keyword in text:
                self.table.clearSelection()
                self.table.selectRow(row)
                self.table.setCurrentCell(row, _COL_NAME)
                self.table.setRowHidden(row, False)
                self.statusMessage.emit(f"找到字段：{self._spec_at(row).name or '（空字段名）'}")
                return
        self.statusMessage.emit(f"没有找到包含「{self.search_edit.text().strip()}」的字段")

    # --------------------------------------------------------------- 结构来源

    def _on_import(self) -> None:
        """「导入」：按选中的结构来源读一份字段结构进来（整表替换）。"""
        if self.dataset_radio.isChecked():
            self._import_from_dataset()
        else:
            self._import_from_template()

    def _import_from_template(self) -> None:
        path = open_file_dir(
            self,
            title="选择字段结构模板",
            init_path=self._default_dir,
            filter_=TEMPLATE_READ_FILTER,
        )
        if not path:
            return
        try:
            fields = read_template(path)
        except FieldTemplateError as exc:
            logger.warning("导入字段结构模板失败：%s", exc)
            self.statusMessage.emit(str(exc))
            return
        self.set_fields(fields)
        self._source_dicts.clear()
        self.statusMessage.emit(f"已从模板导入 {len(fields)} 个字段：{path}")

    def _import_from_dataset(self) -> None:
        dialog = ExternalDatasetDialog(self, vector_layers=self._vector_layers)
        accepted = dialog.exec()
        layer, error = dialog.resolve() if accepted else (None, "")
        # 每次「导入」都新建一个弹窗：用完交出去回收，别挂在 page 上越攒越多
        dialog.deleteLater()
        if error:
            logger.warning("外部数据集读取失败：%s", error)
            self.statusMessage.emit(error)
            return
        if layer is None:
            return
        fields = fields_from_layer(layer)
        self._source_dicts = source_dict_tables(layer, fields)
        self.set_fields(fields)
        extra = (
            f"，并识别到 {len(self._source_dicts)} 个源库字典表"
            if self._source_dicts
            else ""
        )
        self.statusMessage.emit(
            f"已从「{layer.name()}」复制 {len(fields)} 个字段的结构{extra}"
        )

    def _on_generate_template(self) -> None:
        """「生成空模板文件」：导出一份只有表头的 .xlsx（用户填完再导入回来）。"""
        suggested = f"字段结构模板{TEMPLATE_EXTENSION}"
        # os.path.join 不写死分隔符（与 default_output_dir 的约定一致，跨平台）
        init_path = os.path.join(self._default_dir, suggested) if self._default_dir else suggested
        path = save_file(
            self,
            title="生成空模板文件",
            init_path=init_path,
            filter_=TEMPLATE_FILTER,
        )
        if not path:
            return
        if not path.lower().endswith(TEMPLATE_EXTENSION):
            path = f"{path}{TEMPLATE_EXTENSION}"
        try:
            write_template(path)
        except FieldTemplateError as exc:
            logger.warning("生成字段结构模板失败：%s", exc)
            self.statusMessage.emit(str(exc))
            return
        self.statusMessage.emit(f"已生成空模板文件：{path}")


class ExternalDatasetDialog(BaseDialog):
    """「选择数据源」：从工程里的图层、或本地一份文件里挑一个图层。

    两种数据源：

    - **工程里的图层**：工程里已经加载的矢量图层（老行为）；
    - **本地文件**：直接在电脑上挑一份数据。容器型数据源（``.gdb`` 目录 / ``.gpkg`` /
      ``.sqlite``）挑完会**把里面的图层全列出来**让你再选一个 —— 以前只能选工程里的图层，
      拿 GDB 里的某一层就得先把它加进工程，现在不用了。

    **按几何类型筛**：传了 ``geometry_kind``（``point`` / ``line`` / ``polygon``）就只列
    同类型的图层 —— 「追加数据」往面图层里追加点数据没有意义（还会被库拒掉）。

    GDB 是**目录**，文件对话框选不了，所以「浏览」分两个按钮（文件 / GDB 目录）。
    读文件、列图层、打开图层都在 :mod:`app.core.util.sublayers`（核心层），这里只摆界面。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        vector_layers: Sequence[object] = (),
        geometry_kind: str = "",
        multi: bool = False,
        allow_replace: bool = False,
    ) -> None:
        """
        Args:
            parent: 父窗口。
            vector_layers: 工程里已加载的矢量图层（注入，不自己去问 QgsProject）。
            geometry_kind: 只要这个几何类别的图层（``point`` / ``line`` / ``polygon``）；
                空串 = 不筛。
            multi: ``True`` = 可以一次选多个图层（「导入数据」用）。
            allow_replace: ``True`` = 底部显示「导入方式：追加 / 替换」单选
                （「导入数据」用；其他调用方只取图层，不关心这条）。
        """
        super().__init__(parent, "选择外部数据集")
        self._geometry_kind = str(geometry_kind or "")
        self._multi = bool(multi)
        self._allow_replace = bool(allow_replace)
        self._layers = [
            layer for layer in vector_layers
            if not self._geometry_kind or geometry_kind_of_layer(layer) == self._geometry_kind
        ]
        #: 本地文件模式下列出来的子图层
        self._sublayers: list[Sublayer] = []
        #: 本地文件模式当前选中的数据文件 / GDB 目录
        self._source_path = ""
        #: 取图层时遇到的失败原因（见 :meth:`selected_layers`）
        self.last_error = ""
        self.setMinimumSize(0, 0)
        self.resize(560, 440)
        self._build()

    # --------------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        sources = QHBoxLayout()
        sources.addWidget(QLabel("数据源："))
        self.project_radio = QRadioButton("工程里的图层")
        self.project_radio.setChecked(True)
        self.project_radio.toggled.connect(self._on_source_changed)
        sources.addWidget(self.project_radio)
        self.file_radio = QRadioButton("本地文件")
        self.file_radio.toggled.connect(self._on_source_changed)
        sources.addWidget(self.file_radio)
        sources.addStretch(1)
        layout.addLayout(sources)

        self.source_stack = QStackedWidget()
        self.source_stack.addWidget(self._build_project_page())
        self.source_stack.addWidget(self._build_file_page())
        layout.addWidget(self.source_stack, 1)

        # 导入方式（追加 / 替换）：「导入数据」才显示。**默认追加**（保守，不动原有数据）
        self.append_radio = QRadioButton("追加（保留原有要素）")
        self.append_radio.setChecked(True)
        self.replace_radio = QRadioButton("替换（清空后写入）")
        if self._allow_replace:
            mode_row = QHBoxLayout()
            mode_row.addWidget(QLabel("导入方式："))
            mode_row.addWidget(self.append_radio)
            mode_row.addWidget(self.replace_radio)
            mode_row.addStretch(1)
            layout.addLayout(mode_row)

        note = QLabel("复制所选图层的字段名 / 别名 / 类型 / 长度 / 小数位数 / 是否可为空 / 是否可见。")
        note.setObjectName("PropHint")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        # **两组单选必须分开**：Qt 默认把同一个父控件下的 QRadioButton 自动归成一组。
        # 数据源（工程 / 文件）与导入方式（追加 / 替换）同在一个对话框里，不分组的话
        # 勾「替换」会把「工程里的图层」取消勾选，数据源的选中也被连带清掉（用户报的）。
        self._source_group = QButtonGroup(self)
        self._source_group.addButton(self.project_radio)
        self._source_group.addButton(self.file_radio)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.append_radio)
        self._mode_group.addButton(self.replace_radio)
        self._refresh_ok()

    def _build_project_page(self) -> QWidget:
        """工程里的图层：一个列表（单选或多选，见 ``multi``）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.layer_list = QListWidget()
        #: QSS 作用域锚点：选中色由主题里 #ExternalLayerList 规则加深
        self.layer_list.setObjectName("ExternalLayerList")
        self.layer_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if self._multi
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        if not self._layers:
            hint_text = (
                f"（工程里没有同类型的矢量图层：{_KIND_LABELS.get(self._geometry_kind, '')}）"
                if self._geometry_kind
                else "（工程里没有矢量图层）"
            )
            placeholder = QListWidgetItem(hint_text)
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.layer_list.addItem(placeholder)
        else:
            # 与导入对话框同一个排序：按图层名升序，同名保持原顺序
            for _index, layer in sorted(
                enumerate(self._layers), key=lambda pair: (pair[1].name() or "", pair[0])
            ):
                item = QListWidgetItem(layer.name())
                item.setData(Qt.ItemDataRole.UserRole, layer)
                self.layer_list.addItem(item)
            self.layer_list.setCurrentRow(0)
        self.layer_list.itemSelectionChanged.connect(self._refresh_ok)
        layout.addWidget(self.layer_list, 1)

        hint = QLabel(
            "工程里已加载的图层"
            + ("（可按住 Ctrl / Shift 多选）" if self._multi else "")
            + "。想用电脑上的数据文件（含 GDB / 库）请选「本地文件」。"
        )
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return page

    def _build_file_page(self) -> QWidget:
        """本地文件：挑文件 → 列出里面的图层 → 再选一个。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("选一份数据文件（GDB 是文件夹，用右边的按钮选）")
        row.addWidget(self.path_edit, 1)
        browse_file = QPushButton("浏览文件…")
        browse_file.setToolTip("Shapefile / GeoPackage / SQLite / GeoJSON / GML / KML …")
        browse_file.clicked.connect(self._on_browse_file)
        row.addWidget(browse_file)
        browse_gdb = QPushButton("浏览 GDB…")
        browse_gdb.setToolTip("File Geodatabase 是一个文件夹，所以单独一个按钮")
        browse_gdb.clicked.connect(self._on_browse_gdb)
        row.addWidget(browse_gdb)
        layout.addLayout(row)

        layout.addWidget(QLabel("图层（容器数据源里可能有多个，选一个" + ("或按住 Ctrl / Shift 多选" if self._multi else "") + "）"))
        self.sublayer_list = QListWidget()
        #: QSS 作用域锚点：选中色由主题里 #ExternalSublayerList 规则加深
        self.sublayer_list.setObjectName("ExternalSublayerList")
        self.sublayer_list.setMinimumHeight(140)
        self.sublayer_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if self._multi
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        self.sublayer_list.currentRowChanged.connect(self._refresh_ok)
        self.sublayer_list.itemSelectionChanged.connect(self._refresh_ok)
        self.sublayer_list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.sublayer_list, 1)

        self.file_status = QLabel("还没选择数据源")
        self.file_status.setObjectName("PropHint")
        self.file_status.setWordWrap(True)
        layout.addWidget(self.file_status)
        return page

    # --------------------------------------------------------------- 交互

    def _on_source_changed(self, *_args: object) -> None:
        self.source_stack.setCurrentIndex(1 if self.file_radio.isChecked() else 0)
        self._refresh_ok()

    def _on_browse_file(self) -> None:
        chosen = open_file_dir(self, title="选择数据文件", filter_=VECTOR_FILE_FILTER)
        if chosen:
            self._load_source(chosen)

    def _on_browse_gdb(self) -> None:
        chosen = open_file_dir(self, is_dir=True, title="选择 GDB 文件夹")
        if chosen:
            self._load_source(chosen)

    def _load_source(self, path: str) -> None:
        """记下数据源并列出里面的图层（单图层文件就一项，直接选中）。

        传了 ``geometry_kind`` 时**只列同类型的** —— 往面图层里追加点数据没有意义。
        """
        self._source_path = path
        self.path_edit.setText(path)
        everything = list_sublayers(path)
        if self._geometry_kind:
            self._sublayers = [
                sub for sub in everything if sub.geometry_kind == self._geometry_kind
            ]
        else:
            self._sublayers = everything
        self.sublayer_list.clear()
        for sublayer in self._sublayers:
            text = sublayer.name if sublayer.has_geometry else f"{sublayer.name}（无几何）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, sublayer)
            self.sublayer_list.addItem(item)
        if not self._sublayers:
            kind_label = _KIND_LABELS.get(self._geometry_kind, "")
            if everything and kind_label:
                self.file_status.setText(
                    f"这份数据里没有与目标图层同类型（{kind_label}）的图层：{path}"
                )
            else:
                self.file_status.setText(
                    f"这份数据里没有可用的图层（或者打不开）：{path}"
                    "\nGDB 请选到「.gdb 文件夹」本身；也确认它没被别的程序占用。"
                )
        else:
            self.sublayer_list.setCurrentRow(0)
            self.file_status.setText(f"共 {len(self._sublayers)} 个图层，选一个即可。")
        self._refresh_ok()

    def _refresh_ok(self, *_args: object) -> None:
        """没有可复制的东西时「确定」就该是灰的（省得点了才发现没选）。

        **只看"有没有选中"，不打开任何文件**：文件模式下的图层要真按路径打开，本来
        就慢（GDB / 库尤其），以前每次选中变化都去打开一遍 —— 勾几个图层就会卡死
        （用户报的"先选本地数据再选工程图层加载不进来/卡死"）。打开只在点「确定」
        时由 :meth:`selected_layers` 做一次。
        """
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._has_selection()
        )

    def _has_selection(self) -> bool:
        """当前是否选中了至少一项（**不打开文件**）。"""
        if self.project_radio.isChecked():
            return any(
                item.data(Qt.ItemDataRole.UserRole) is not None
                for item in self.layer_list.selectedItems()
            )
        return bool(self.sublayer_list.selectedItems())

    # --------------------------------------------------------------- 取值

    def selected_sublayers(self) -> list[Sublayer]:
        """本地文件模式下选中的子图层（可能多个）。"""
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.sublayer_list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole) is not None
        ]

    def selected_sublayer(self) -> Sublayer | None:
        """本地文件模式下选中的那个子图层（没选返回 ``None``）。"""
        selected = self.selected_sublayers()
        return selected[0] if selected else None

    def selected_layers(self) -> list[object]:
        """当前选中的**图层对象**（工程模式直接给；文件模式要打开，失败的不计入）。

        打开文件里的图层是 IO，只有核心层能做（:func:`open_sublayer`）——
        本弹窗在用户点「确定」时才调它，失败原因记在 :attr:`last_error`。
        """
        self.last_error = ""
        if self.project_radio.isChecked():
            layers = [
                item.data(Qt.ItemDataRole.UserRole)
                for item in self.layer_list.selectedItems()
                if item.data(Qt.ItemDataRole.UserRole) is not None
            ]
            if not layers:
                self.last_error = "工程里没有可用的矢量图层"
            return layers
        selected = self.selected_sublayers()
        if not selected:
            self.last_error = "请先选择数据文件，再选里面的图层"
            return []
        layers = []
        errors = []
        for sublayer in selected:
            layer, error = open_sublayer(self._source_path, sublayer)
            if layer is None:
                errors.append(f"{sublayer.name}：{error}")
            else:
                layers.append(layer)
        if errors:
            self.last_error = "；".join(errors)
        return layers

    def resolve_all(self) -> tuple[list[object], str]:
        """要使用的**全部**图层，返回 ``(图层列表, 失败原因)``。

        **部分失败也会把原因带回来**（打开 5 个、3 个成功时分不清"少了哪两个"就是
        "加载不进来"）：调用方拿到图层就继续用，同时把原因提示出去。
        """
        layers = self.selected_layers()
        return layers, self.last_error

    def resolve(self) -> tuple[object | None, str]:
        """取**第一个**选中的图层，返回 ``(图层, 失败原因)``（单选调用方的便捷入口）。"""
        layers, error = self.resolve_all()
        if not layers:
            return None, error or "没有选择任何图层"
        return layers[0], ""

    def mode(self) -> str:
        """导入方式：``append``（默认）或 ``replace``。"""
        return "replace" if self._allow_replace and self.replace_radio.isChecked() else "append"

