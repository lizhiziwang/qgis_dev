"""「导出数据」对话框：把数据管理里的一个图层导出成文件，可选条件筛选。

支持四种目标（用户点名要的）：**Shapefile / File Geodatabase / GeoPackage / SQLite**。
本机装不出某格式的写驱动时，下拉里直接不出现（与创建数据对话框同一条规则，
见 ``create_data_dialog._make_format_combo``）。

**条件筛选**复用属性查询器（:class:`~app.ui.dialogs.query_builder.QueryBuilderDialog`），
但以「只取条件」模式打开：**不改图层的定义查询** —— 这里的条件只用于本次导出，
不该顺手把地图上的图层也筛一遍（那是属性表「筛选」的语义）。

**Shapefile 字段选择**：DBF 硬上限 255 个字段（超过 ArcMap 打不开）。切到 Shapefile
时若图层字段超限，默认只勾前 255 个，并可通过「选择字段…」重新勾选；导出前会再
确认一次"其余字段不会写入"。

本对话框只收集参数（:meth:`ExportLayerDialog.spec`），落盘是
:func:`~app.core.domain.export_layer.export_vector_layer` 的事。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.create_data import available_drivers
from app.core.domain.export_layer import (
    SHAPEFILE_MAX_FIELDS,
    ExportLayerError,
    ExportSpec,
    existing_target_description,
    export_vector_layer,
    sanitize_shapefile_field_name,
)
from app.core.util.dict_values import layer_dict_field_names
from app.core.util.ogr_fields import provider_id_field_indices
from app.core.util.vector_formats import VECTOR_FORMATS, VectorFormat, resolve_format
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.common.widgets import open_file_dir, save_file
from app.ui.dialogs.query_builder import QueryBuilderDialog
from app.ui.tools.feature_attrs import field_alias

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)

#: 数据管理导出：四种本地库 / 文件格式
_EXPORT_ORDER: tuple[str, ...] = (".shp", ".gdb", ".gpkg", ".sqlite")

#: 图层右键「导出」额外支持的格式（AutoCAD / KML / Excel 属性表 / GeoJSON）
_EXTRA_EXPORT_ORDER: tuple[str, ...] = (".dxf", ".kml", ".xlsx", ".geojson")

#: Excel 属性表不在通用格式表里（创建数据不该出现它），导出对话框单独加
_XLSX_FORMAT = VectorFormat(
    "Excel 属性表", "XLSX", ".xlsx", "只写属性表，不写几何"
)

#: 默认格式：GeoPackage（字段名 / 别名 / 类型都不丢；Shapefile 会截字段名）
_DEFAULT_EXTENSION = ".gpkg"

#: 认得出的数据后缀（拼默认文件名时剥掉，免得出现 ``result_small.shp.gdb`` 这种）
_KNOWN_SUFFIXES = frozenset(
    {".shp", ".gpkg", ".sqlite", ".sqlite3", ".db", ".gdb", ".geojson", ".json",
     ".gml", ".kml", ".tab", ".fgb", ".dxf", ".csv", ".gpx"}
)


def _clean_stem(name: str) -> str:
    """文件名 / 图层名 → 干净的主名（把认得出的数据后缀一律剥掉）。

    图层名常常是 ``result_small.shp`` 这种带后缀的（打开文件时按文件名取的），
    直接接 ``.gdb`` 会拼出 ``result_small.shp.gdb`` —— 用户报过。
    """
    stem = os.path.basename(str(name or "").strip())
    while True:
        root, ext = os.path.splitext(stem)
        if root and ext.lower() in _KNOWN_SUFFIXES:
            stem = root
        else:
            return stem or "导出"


def _export_formats(include_extra: bool = False) -> list[VectorFormat]:
    """本机真能写出的导出格式（数据管理四种；图层导出再加四种）。"""
    writers = available_drivers()
    wanted = set(_EXPORT_ORDER) | (set(_EXTRA_EXPORT_ORDER) if include_extra else set())
    found: dict[str, VectorFormat] = {}
    for fmt in VECTOR_FORMATS:
        if fmt.extension not in wanted:
            continue
        resolved = resolve_format(fmt, writers)
        if resolved is not None:
            found[resolved.extension] = resolved
    if include_extra:
        resolved = resolve_format(_XLSX_FORMAT, writers)
        if resolved is not None:
            found[resolved.extension] = resolved
    order = _EXPORT_ORDER + (_EXTRA_EXPORT_ORDER if include_extra else ())
    return [found[ext] for ext in order if ext in found]


#: 数据类型显示名（与 OGR / ArcGIS 的叫法一致，比 QVariant 的 "string" 好认）
_TYPE_LABELS = {
    QVariant.Int: "Integer",
    QVariant.LongLong: "Integer64",
    QVariant.UInt: "Integer",
    QVariant.ULongLong: "Integer64",
    QVariant.Double: "Real",
    QVariant.String: "String",
    QVariant.Date: "Date",
    QVariant.DateTime: "DateTime",
    QVariant.Bool: "Boolean",
    QVariant.ByteArray: "Binary",
}


def _type_label(field) -> str:
    return _TYPE_LABELS.get(field.type(), field.typeName() or "")


class ExportLayerDialog(BaseDialog):
    """导出参数表单：格式 / 保存位置 / 图层名 / 条件。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        layer: QgsVectorLayer,
        default_dir: str = "",
        extra_formats: bool = False,
        filter_modes: tuple[str, ...] | None = None,
    ) -> None:
        """``filter_modes``：

        - ``None``（默认，数据管理入口）——保持原来的「筛选条件」行：空 = 全部要素；
        - ``("all", "selected", "expression")``（图层列表右键入口）——显示
          「筛选数据」下拉：导出全部 / 选中数据 / 自定义条件。
        """
        super().__init__(parent, "导出数据")
        self._layer = layer
        self._default_dir = default_dir
        #: None = 老版"筛选条件" UI；否则是"筛选数据"下拉允许的模式
        self._filter_modes = (
            None
            if filter_modes is None
            else tuple(
                mode for mode in filter_modes if mode in ("all", "selected", "expression")
            )
            or ("all",)
        )
        #: 当前筛选条件（QGIS 表达式；空串 = 全部要素）
        self._expression = ""
        #: 挂了字典的字段名（GDB 写属性域 / GPKG、SQLite 附带字典表用）
        self._dict_field_names = layer_dict_field_names(layer)
        self._formats = _export_formats(extra_formats)
        #: 重填字段表期间忽略 itemChanged（否则每加一行就刷一次提示）
        self._loading_fields = False
        if not self._formats:
            logger.warning("导出：本机没有可用的矢量写驱动")
        self.setMinimumSize(0, 0)
        self.resize(760, 640)
        self._build()
        self._apply_format_defaults()
        # 弹窗是**非模态**的：用户可能边开着导出框边改选中集。选中数提示
        # （「选中数据」的占位 / tooltip / 底部提示）要跟着变；筛选方式本身
        # 不自动改，避免用户已经选好的「导出全部」被悄悄切走。
        # 只有「筛选数据」模式（图层列表右键）才有"选中数据"，老版条件行不用连。
        if self._filter_modes is not None:
            try:
                layer.selectionChanged.connect(self._on_layer_selection_changed)
            except Exception:  # noqa: BLE001 —— 图层销毁等收尾场景
                logger.exception("导出：连接图层 selectionChanged 失败")

    # --------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QLabel()
        count = self._layer.featureCount()
        count_text = f"当前 {count} 个要素" if count >= 0 else "要素数未知"
        selected_count = self._selected_feature_count()
        if selected_count:
            count_text += f"，已选中 {selected_count} 个"
        head.setText(f"导出图层：{self._layer.name()}（{count_text}）")
        head.setObjectName("SectionLabel")
        head.setWordWrap(True)
        root.addWidget(head)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setSpacing(8)

        self.format_combo = QComboBox()
        for fmt in self._formats:
            self.format_combo.addItem(fmt.label, fmt)
            if fmt.note:
                self.format_combo.setItemData(
                    self.format_combo.count() - 1, fmt.note, Qt.ItemDataRole.ToolTipRole
                )
        # 默认 GeoPackage：字段名 / 别名 / 类型都不丢（Shapefile 会截字段名）
        default_index = next(
            (i for i, fmt in enumerate(self._formats) if fmt.extension == _DEFAULT_EXTENSION), 0
        )
        self.format_combo.setCurrentIndex(default_index)
        self.format_combo.currentIndexChanged.connect(self._apply_format_defaults)
        form.addRow("数据格式", self.format_combo)

        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        self.path_edit = QLineEdit()
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse)
        path_row.addWidget(browse)
        form.addRow("保存位置", path_row)

        self.layer_name_edit = QLineEdit()
        self.layer_name_edit.setPlaceholderText("写进库里的图层名")
        form.addRow("图层名", self.layer_name_edit)

        # 导出字段：勾选要写的字段 + 可改导出名（所有格式通用）
        # 先建到独立容器里，等 form 挂到 root 之后再放（表要显示在格式设置下方）
        fields_box = QWidget()
        fields_layout = QVBoxLayout(fields_box)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.setSpacing(4)
        fields_title = QLabel("导出字段")
        fields_title.setObjectName("SectionLabel")
        fields_layout.addWidget(fields_title)
        self.field_table = QTableWidget(0, 3)
        self.field_table.setHorizontalHeaderLabels(["名称", "导出名称", "类型"])
        self.field_table.verticalHeader().setVisible(False)
        self.field_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.field_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.field_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        fh = self.field_table.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        fh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        fh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.field_table.setColumnWidth(0, 220)
        self.field_table.setColumnWidth(2, 110)
        self.field_table.setMinimumHeight(180)
        self._fill_field_table()
        self.field_table.itemChanged.connect(self._on_field_item_changed)
        fields_layout.addWidget(self.field_table, 1)

        field_tools = QHBoxLayout()
        field_tools.setContentsMargins(0, 0, 0, 0)
        field_tools.setSpacing(6)
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setToolTip("勾选全部字段")
        self.select_all_btn.clicked.connect(self._select_all_fields)
        self.select_none_btn = QPushButton("全不选")
        self.select_none_btn.setToolTip("取消勾选全部字段")
        self.select_none_btn.clicked.connect(self._select_no_fields)
        # 与「条件… / 清除」同一布局：按钮靠右，不拉伸占满整行
        field_tools.addStretch(1)
        field_tools.addWidget(self.select_all_btn)
        field_tools.addWidget(self.select_none_btn)
        fields_layout.addLayout(field_tools)
        self.alias_names_check = QCheckBox("为导出名称使用别名")
        self.alias_names_check.setToolTip(
            "勾选后，导出名称默认取字段别名（没有别名时用字段名）；仍可手动改"
        )
        self.alias_names_check.toggled.connect(self._apply_alias_names)
        fields_layout.addWidget(self.alias_names_check)
        self.load_after_check = QCheckBox("导出后加载到地图")
        self.load_after_check.setToolTip("导出完成后把写出的图层加载进当前工程")
        self.load_after_check.setChecked(True)  # 默认勾选
        fields_layout.addWidget(self.load_after_check)
        # Shapefile 字段超限 / 手动选超时的红色提示（平时隐藏）
        self.field_warning = QLabel()
        self.field_warning.setWordWrap(True)
        self.field_warning.setStyleSheet("color: #d93025;")
        self.field_warning.hide()
        fields_layout.addWidget(self.field_warning)

        # 字典转换：Shapefile 没有承载字典的位置 → 禁用；GDB 写成属性域，
        # GPKG / SQLite 附带字典表（并写字段元数据）。
        self.dict_check = QCheckBox("转换字典")
        self.dict_check.setToolTip(
            "把字段挂接的字典一起导出：FileGDB 写成属性域（coded value domain）；"
            "GeoPackage / SQLite 把字典表写进目标库。Shapefile 不支持。"
        )
        form.addRow("字典", self.dict_check)

        if self._filter_modes is None:
            # 数据管理入口：保持原来的「筛选条件」行（空 = 全部要素）
            condition_row = QHBoxLayout()
            condition_row.setContentsMargins(0, 0, 0, 0)
            condition_row.setSpacing(6)
            self.filter_combo = None
            self.condition_edit = QLineEdit()
            self.condition_edit.setReadOnly(True)
            self.condition_edit.setPlaceholderText("全部要素（点「条件…」按属性筛选）")
            condition_row.addWidget(self.condition_edit, 1)
            pick_condition = QPushButton("条件…")
            pick_condition.setToolTip("用属性查询器写一个筛选条件，只影响本次导出")
            pick_condition.clicked.connect(self._on_pick_condition)
            condition_row.addWidget(pick_condition)
            clear_condition = QPushButton("清除")
            clear_condition.clicked.connect(self._on_clear_condition)
            condition_row.addWidget(clear_condition)
            self._pick_condition_btn = pick_condition
            self._clear_condition_btn = clear_condition
            form.addRow("筛选条件", condition_row)
        else:
            # 图层列表右键入口：筛选数据（全部 / 选中 / 自定义条件）。
            # 默认：当前图层有选中要素 → 选中数据；否则导出全部。
            filter_row = QHBoxLayout()
            filter_row.setContentsMargins(0, 0, 0, 0)
            filter_row.setSpacing(6)
            self.filter_combo = QComboBox()
            self.filter_combo.setMinimumWidth(110)
            for mode in self._filter_modes:
                label, tip = {
                    "all": ("导出全部", "导出图层里的全部要素"),
                    "selected": (
                        "选中数据",
                        "只导出当前选中的要素（当前选中 "
                        f"{self._selected_feature_count()} 个）",
                    ),
                    "expression": (
                        "自定义条件",
                        "点「条件…」用属性查询器写筛选条件",
                    ),
                }[mode]
                index = self.filter_combo.count()
                self.filter_combo.addItem(label, mode)
                self.filter_combo.setItemData(
                    index, tip, Qt.ItemDataRole.ToolTipRole
                )
            filter_row.addWidget(self.filter_combo)

            self.condition_edit = QLineEdit()
            self.condition_edit.setReadOnly(True)
            self.condition_edit.setPlaceholderText("全部要素")
            filter_row.addWidget(self.condition_edit, 1)
            pick_condition = QPushButton("条件…")
            pick_condition.setToolTip("用属性查询器写一个筛选条件，只影响本次导出")
            pick_condition.clicked.connect(self._on_pick_condition)
            filter_row.addWidget(pick_condition)
            clear_condition = QPushButton("清除")
            clear_condition.setToolTip("清除自定义条件")
            clear_condition.clicked.connect(self._on_clear_condition)
            filter_row.addWidget(clear_condition)
            self._pick_condition_btn = pick_condition
            self._clear_condition_btn = clear_condition
            if "expression" not in self._filter_modes:
                # 这个入口不提供自定义条件：整组条件控件不显示
                self.condition_edit.hide()
                pick_condition.hide()
                clear_condition.hide()
            form.addRow("筛选数据", filter_row)

            default_mode = (
                "selected"
                if "selected" in self._filter_modes and self._selected_feature_count()
                else "all"
                if "all" in self._filter_modes
                else self._filter_modes[0]
            )
            default_index = self.filter_combo.findData(default_mode)
            self.filter_combo.setCurrentIndex(
                default_index if default_index >= 0 else 0
            )
            self.filter_combo.currentIndexChanged.connect(
                self._on_filter_mode_changed
            )
            self._on_filter_mode_changed()

        root.addLayout(form)
        root.addWidget(fields_box, 1)

        self.hint = QLabel()
        self.hint.setObjectName("PropHint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导出")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._on_ok)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.setLayout(root)

    # --------------------------------------------------------------- 交互

    def _current_format(self) -> VectorFormat | None:
        fmt = self.format_combo.currentData()
        return fmt if isinstance(fmt, VectorFormat) else None

    # ------------------------------------------------------- 筛选数据

    def _selected_feature_count(self) -> int:
        """当前图层选中要素数（图层销毁时按 0）。"""
        try:
            return len(self._layer.selectedFeatureIds())
        except RuntimeError:
            return 0

    def _filter_mode(self) -> str:
        """``all`` / ``selected`` / ``expression``；老版条件行按 expression 处理。"""
        if self.filter_combo is None:
            return "expression"
        data = self.filter_combo.currentData()
        return data if isinstance(data, str) else "all"

    def _on_filter_mode_changed(self, *_args: object) -> None:
        """切筛选方式：只有"自定义条件"才启用条件框 / 条件按钮。"""
        if self.filter_combo is None:
            return
        mode = self._filter_mode()
        custom = mode == "expression"
        self.condition_edit.setEnabled(custom)
        self._pick_condition_btn.setEnabled(custom)
        self._clear_condition_btn.setEnabled(custom)
        if custom:
            self.condition_edit.setText(self._expression)
            self.condition_edit.setPlaceholderText("点「条件…」按属性筛选")
        else:
            self.condition_edit.clear()
            if mode == "selected":
                count = self._selected_feature_count()
                self.condition_edit.setPlaceholderText(
                    f"已选中 {count} 个要素" if count else "当前图层没有选中要素"
                )
            else:
                self.condition_edit.setPlaceholderText("全部要素")
        # 选中数可能变化：更新「选中数据」的 tooltip
        selected_index = self.filter_combo.findData("selected")
        if selected_index >= 0:
            self.filter_combo.setItemData(
                selected_index,
                f"只导出当前选中的要素（当前选中 {self._selected_feature_count()} 个）",
                Qt.ItemDataRole.ToolTipRole,
            )
        if hasattr(self, "hint"):
            self._update_hint()

    def _on_layer_selection_changed(self) -> None:
        """图层选中集变化：刷新选中数提示（不自动切换筛选方式）。"""
        try:
            self._on_filter_mode_changed()
        except RuntimeError:
            pass  # 对话框关闭 / 图层销毁

    def _apply_format_defaults(self, *_args: object) -> None:
        """切格式：保存位置的后缀跟着换；容器格式才需要图层名。"""
        fmt = self._current_format()
        if fmt is None:
            self.hint.setText("本机没有可用的矢量写驱动，导不出去")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        # 图层名只对容器有意义（shp 的图层名就是文件名）
        is_container = fmt.container
        self.layer_name_edit.setEnabled(is_container)
        if is_container and not self.layer_name_edit.text().strip():
            self.layer_name_edit.setText(self._layer.name())
        current = self.path_edit.text().strip()
        stem = _clean_stem(os.path.basename(current)) if current else _clean_stem(self._layer.name())
        base_dir = os.path.dirname(current) if current else self._default_dir
        if not base_dir:
            base_dir = self._default_dir
        self.path_edit.setText(os.path.join(base_dir, f"{stem}{fmt.extension}") if base_dir else f"{stem}{fmt.extension}")
        # 字段选择 / 改名对所有格式通用；DXF 没有属性表，整块禁用
        is_dxf = fmt.extension == ".dxf"
        for widget in (
            self.field_table,
            self.select_all_btn,
            self.select_none_btn,
            self.alias_names_check,
        ):
            widget.setEnabled(not is_dxf)
        # Excel 只写属性表，加载到地图没有意义
        self.load_after_check.setEnabled(fmt.extension != ".xlsx")
        if fmt.extension == ".shp":
            # Shapefile 字段上限：自动勾前 255 个（并给红色提示）
            self._auto_limit_shapefile_fields()
        # 字典：GDB / GPKG / SQLite 能落字典表 / 属性域；其它能写属性的格式
        # （Excel / SHP / GeoJSON / KML）勾选后把代码值转成字典名称。
        native_dict = fmt.extension in (".gdb", ".gpkg", ".sqlite", ".sqlite3", ".db")
        value_dict = fmt.extension in (".xlsx", ".shp", ".geojson", ".kml")
        supports_dict = native_dict or value_dict
        dict_ok = bool(self._dict_field_names) and supports_dict
        self.dict_check.setEnabled(dict_ok)
        # 默认值：GDB / GPKG / SQLite 有字典时默认勾选（附带字典表 / 属性域）；
        # Excel / SHP / GeoJSON / KML 是「值转名称」，默认**不勾选**（保留原始代码）。
        self.dict_check.setChecked(dict_ok and native_dict)
        if not self._dict_field_names:
            label = "导出字典名称" if value_dict else "转换字典"
            self.dict_check.setText(f"{label}（图层没有挂接字典）")
            self.dict_check.setToolTip("当前图层没有挂接字典")
        elif not supports_dict:
            label = "导出字典名称" if value_dict else "转换字典"
            self.dict_check.setText(f"{label}（当前格式不支持）")
            self.dict_check.setToolTip("DXF 没有属性表，无法转换字典")
        elif value_dict:
            self.dict_check.setText(
                f"导出字典名称（{len(self._dict_field_names)} 个字段）"
            )
            self.dict_check.setToolTip(
                "勾选：字典字段导出字典名称（显示值）；不勾选：导出原始代码值。"
                "当前格式不附带字典表。"
            )
        else:
            self.dict_check.setText(
                f"转换字典（{len(self._dict_field_names)} 个字段）"
            )
            self.dict_check.setToolTip(
                "把字段挂接的字典一起导出：FileGDB 写成属性域（coded value domain）；"
                "GeoPackage / SQLite 把字典表写进目标库。"
            )
        self._update_hint()

    def _update_hint(self) -> None:
        """按当前格式 / 勾选 / 导出名刷新底部提示。"""
        fmt = self._current_format()
        if fmt is None:
            return
        total = self._exportable_field_count()
        selected = len(self._selected_field_names())
        parts: list[str] = []
        # 筛选数据（只有「筛选数据」下拉模式才有；数据管理的老版条件行保持原样）
        if self._filter_modes is not None:
            mode = self._filter_mode()
            if mode == "selected":
                count = self._selected_feature_count()
                parts.append(
                    f"只导出选中要素（当前选中 {count} 个）"
                    if count
                    else "只导出选中要素：当前图层没有选中要素"
                )
            elif mode == "expression":
                parts.append(
                    f"按条件筛选：{self._expression}"
                    if self._expression
                    else "自定义条件：尚未填写条件"
                )
            else:
                parts.append("导出全部要素")
        if fmt.extension == ".dxf":
            parts.append("DXF 无属性表，只写几何；字段选择不适用")
        else:
            if selected == total:
                parts.append(f"导出全部 {total} 个字段")
            else:
                parts.append(f"将导出 {selected} / {total} 个字段")
            if fmt.extension == ".shp":
                if selected > SHAPEFILE_MAX_FIELDS:
                    parts.append(
                        f"超过 Shapefile 上限 {SHAPEFILE_MAX_FIELDS} 个，"
                        "请减少字段或改用 FileGDB / GeoPackage"
                    )
                renamed = self._shapefile_renames()
                if renamed:
                    preview = "、".join(
                        f"{old}→{new}" for old, new in list(renamed.items())[:3]
                    )
                    extra = f" 等 {len(renamed)} 个" if len(renamed) > 3 else ""
                    parts.append(f"字段名 ≤10 字节，会被改写：{preview}{extra}")
            else:
                export_names = self._export_name_map()
                selected_set = set(self._selected_field_names())
                changed = {
                    original: name
                    for original, name in export_names.items()
                    if original in selected_set and name != original
                }
                if changed:
                    preview = "、".join(
                        f"{old}→{new}" for old, new in list(changed.items())[:3]
                    )
                    extra = f" 等 {len(changed)} 个" if len(changed) > 3 else ""
                    parts.append(f"导出字段将改名：{preview}{extra}")
        if fmt.extension in (".kml", ".geojson"):
            parts.append("只认 WGS 84，投影坐标系图层会自动转换到 EPSG:4326")
        elif fmt.extension == ".xlsx":
            parts.append("Excel 只写属性表，不写几何")
        if self.dict_check.isChecked() and fmt.extension in (
            ".gdb", ".gpkg", ".sqlite", ".sqlite3", ".db"
        ):
            parts.append("勾选「转换字典」后，GDB 会写入属性域，GeoPackage / SQLite 会附带字典表")
        elif self.dict_check.isChecked() and fmt.extension in (
            ".xlsx", ".shp", ".geojson", ".kml"
        ):
            parts.append("字典字段将写入字典名称（不勾选则写原始代码值）")
        self.hint.setText("；".join(parts) + "。")
        self._update_field_warning(fmt, selected)

    def _auto_limit_shapefile_fields(self) -> None:
        """切到 Shapefile 时字段超过上限：自动只勾前 255 个（跳过不可勾选的 fid）。"""
        total = self.field_table.rowCount()
        checkable = [
            row
            for row in range(total)
            if (item := self.field_table.item(row, 0)) is not None
            and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        ]
        if len(checkable) <= SHAPEFILE_MAX_FIELDS:
            return
        self._loading_fields = True
        try:
            for position, row in enumerate(checkable):
                item = self.field_table.item(row, 0)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if position < SHAPEFILE_MAX_FIELDS
                        else Qt.CheckState.Unchecked
                    )
        finally:
            self._loading_fields = False

    def _update_field_warning(self, fmt: VectorFormat | None, selected: int) -> None:
        """Shapefile 字段上限的红色提示（其它格式 / 未超限时隐藏）。"""
        text = ""
        if fmt is not None and fmt.extension == ".shp":
            total = self._exportable_field_count()
            if selected > SHAPEFILE_MAX_FIELDS:
                text = (
                    f"已选 {selected} 个字段，超过 Shapefile 上限 "
                    f"{SHAPEFILE_MAX_FIELDS} 个；请减少字段，其余字段不会导出。"
                )
            elif total > SHAPEFILE_MAX_FIELDS and selected == SHAPEFILE_MAX_FIELDS:
                text = (
                    f"Shapefile 最多 {SHAPEFILE_MAX_FIELDS} 个字段，已自动选择"
                    f"前 {SHAPEFILE_MAX_FIELDS} 个；其余字段不会导出。"
                )
        self.field_warning.setText(text)
        self.field_warning.setVisible(bool(text))

    def load_after_export(self) -> bool:
        """用户是否勾选了「导出后加载到地图」（Excel 不适用）。"""
        return bool(
            self.load_after_check.isEnabled() and self.load_after_check.isChecked()
        )

    # ---------------------------------------------------------- 导出字段表

    def _fill_field_table(self) -> None:
        """按图层字段填表：名称（勾选）/ 导出名称（可改）/ 类型。

        数据源自己维护的 ``fid`` / ``ogc_fid`` **不参与导出**（目标文件会分配
        自己的 FID；把它当普通属性写出去会报 ``Wrong field type for fid``）：
        这一行置灰、不可勾选、默认不选。
        """
        fields = self._layer.fields()
        provider_ids = provider_id_field_indices(self._layer)
        self._loading_fields = True
        try:
            self.field_table.setRowCount(fields.size())
            for row in range(fields.size()):
                field = fields.at(row)
                name_item = QTableWidgetItem(field.name())
                if row in provider_ids:
                    name_item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    )
                    name_item.setCheckState(Qt.CheckState.Unchecked)
                    name_item.setToolTip(
                        "数据源维护的 FID，导出时由目标文件自动分配，不作为属性导出"
                    )
                else:
                    name_item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    name_item.setCheckState(Qt.CheckState.Checked)
                name_item.setData(Qt.ItemDataRole.UserRole, field.name())
                self.field_table.setItem(row, 0, name_item)

                # 默认导出名 = 字段名；勾「为导出名称使用别名」后才切到别名
                export_item = QTableWidgetItem(field.name())
                export_item.setToolTip("导出到目标文件时使用的字段名")
                self.field_table.setItem(row, 1, export_item)

                type_item = QTableWidgetItem(_type_label(field))
                type_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.field_table.setItem(row, 2, type_item)
        finally:
            self._loading_fields = False

    def _on_field_item_changed(self, _item) -> None:
        if not self._loading_fields:
            self._update_hint()

    def _select_all_fields(self) -> None:
        self._set_all_checked(True)

    def _select_no_fields(self) -> None:
        self._set_all_checked(False)

    def _set_all_checked(self, checked: bool) -> None:
        self._loading_fields = True
        try:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for row in range(self.field_table.rowCount()):
                item = self.field_table.item(row, 0)
                if item is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(state)
        finally:
            self._loading_fields = False
        self._update_hint()

    def _apply_alias_names(self, checked: bool) -> None:
        """「为导出名称使用别名」：导出名切到别名 / 字段名。"""
        fields = self._layer.fields()
        self._loading_fields = True
        try:
            for row in range(self.field_table.rowCount()):
                item = self.field_table.item(row, 1)
                if item is None:
                    continue
                field = fields.at(row)
                item.setText(
                    (field_alias(field) or field.name()) if checked else field.name()
                )
        finally:
            self._loading_fields = False
        self._update_hint()

    def _selected_field_names(self) -> list[str]:
        """勾选的字段名（按图层字段顺序）。"""
        names: list[str] = []
        for row in range(self.field_table.rowCount()):
            item = self.field_table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                names.append(str(item.data(Qt.ItemDataRole.UserRole) or item.text()))
        return names

    def _exportable_field_count(self) -> int:
        """可导出字段数（图层字段去掉数据源自维护的 fid/ogc_fid）。"""
        return len(self._layer.fields()) - len(provider_id_field_indices(self._layer))

    def _export_name_map(self) -> dict[str, str]:
        """``{原字段名: 导出名称}``（空白的回退原字段名）。"""
        mapping: dict[str, str] = {}
        for row in range(self.field_table.rowCount()):
            name_item = self.field_table.item(row, 0)
            export_item = self.field_table.item(row, 1)
            if name_item is None:
                continue
            original = str(
                name_item.data(Qt.ItemDataRole.UserRole) or name_item.text()
            )
            text = export_item.text().strip() if export_item is not None else ""
            mapping[original] = text or original
        return mapping

    def _shapefile_renames(self) -> dict[str, str]:
        """Shapefile 下「导出名 → DBF 合法名」中被改写的部分（提示用）。"""
        used: set[str] = set()
        out: dict[str, str] = {}
        names = self._export_name_map()
        for original in self._selected_field_names():
            desired = names.get(original, original)
            safe = sanitize_shapefile_field_name(desired, used)
            if safe != desired:
                out[original] = safe
        return out

    def _on_browse(self) -> None:
        """选保存位置：GDB 是目录，用「选文件夹」；其余格式用「另存为」。"""
        fmt = self._current_format()
        if fmt is None:
            return
        if fmt.extension == ".gdb":
            base = os.path.dirname(self.path_edit.text().strip()) or self._default_dir
            chosen = open_file_dir(self, is_dir=True, title="选择或新建 GDB 文件夹", init_path=base)
            if chosen:
                if chosen.lower().endswith(".gdb"):
                    # 用户直接选了一个已有的（或刚新建的）.gdb 文件夹 → **就用它**，
                    # 别在里面再套一层「图层名.gdb」（用户报过）
                    self.path_edit.setText(chosen)
                else:
                    name = _clean_stem(
                        os.path.basename(self.path_edit.text().strip()) or self._layer.name()
                    )
                    self.path_edit.setText(os.path.join(chosen, f"{name}.gdb"))
            return
        suggested = self.path_edit.text().strip() or os.path.join(
            self._default_dir, f"{self._layer.name()}{fmt.extension}"
        )
        chosen = save_file(
            self,
            title="导出为",
            init_path=suggested,
            filter_=f"{fmt.label} (*{fmt.extension});;所有文件 (*)",
        )
        if chosen:
            if not chosen.lower().endswith(fmt.extension):
                chosen = f"{os.path.splitext(chosen)[0]}{fmt.extension}"
            self.path_edit.setText(chosen)

    def _on_pick_condition(self) -> None:
        """「条件…」：属性查询器（只取条件模式，不改图层的定义查询）。"""
        from app.ui.common.modeless import show_modeless

        show_modeless(
            self,
            "export_condition",
            lambda: QueryBuilderDialog(self._layer, self, apply_to_layer=False),
            configure=lambda dlg: dlg.sqlConfirmed.connect(self._set_condition),
            layer=self._layer,
        )

    def _set_condition(self, condition: str) -> None:
        self._expression = (condition or "").strip()
        if self.filter_combo is None:
            self.condition_edit.setText(self._expression)
        else:
            index = self.filter_combo.findData("expression")
            if index >= 0 and self.filter_combo.currentIndex() != index:
                # 选好条件即切到「自定义条件」；change 信号会回填编辑框
                self.filter_combo.setCurrentIndex(index)
            else:
                self.condition_edit.setText(self._expression)
        if hasattr(self, "hint"):
            self._update_hint()

    def _on_clear_condition(self) -> None:
        self._expression = ""
        self.condition_edit.clear()
        if hasattr(self, "hint"):
            self._update_hint()

    # --------------------------------------------------------------- 取值

    def _on_ok(self) -> None:
        error = self._validation_error()
        if error:
            self.hint.setText(error)
            return
        spec = self.spec()
        if spec is None:
            self.hint.setText(self._validation_error())
            return
        # **目标已存在要问一句**：单文件格式 = 文件已存在；容器格式 = 里面有同名图层。
        # 用户确认后才覆盖（容器的替换只动那一个图层，见 export_vector_layer 的说明）。
        existing = existing_target_description(spec)
        if existing:
            ret = MessageBox.question(
                self,
                "替换已存在的数据",
                f"{existing}。\n\n确定替换？\n（只覆盖这一份；库里的其他图层不受影响）"
                if spec.is_container
                else f"{existing}。\n\n确定覆盖？",
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if ret != MessageBox.StandardButton.Yes:
                return
        # 只导出部分字段：明确问一句，别让用户以为全部字段都写进去了
        if spec.driver != "DXF":
            total = self._exportable_field_count()
            selected = len(spec.field_names or ())
            if selected < total:
                ret = MessageBox.question(
                    self,
                    "导出字段",
                    f"本次只导出 {selected} 个字段，其余 {total - selected} 个字段"
                    "不会写入目标文件。\n\n确定继续？",
                    MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                    MessageBox.StandardButton.Yes,
                )
                if ret != MessageBox.StandardButton.Yes:
                    return
        self.accept()

    def _validation_error(self) -> str:
        fmt = self._current_format()
        if fmt is None:
            return "本机没有可用的矢量写驱动，导不出去"
        if self._filter_modes is not None:
            mode = self._filter_mode()
            if mode == "selected" and self._selected_feature_count() == 0:
                return "当前图层没有选中要素：请先选择要素，或把「筛选数据」改为导出全部"
            if mode == "expression" and not self._expression:
                return "请点「条件…」设置筛选条件，或把「筛选数据」改为导出全部"
        target = self.path_edit.text().strip()
        if not target:
            return "请选择保存位置"
        if fmt.extension == ".gdb":
            parent = os.path.dirname(target)
            if not os.path.isdir(parent):
                return f"目录不存在：{parent}"
        else:
            if not os.path.isdir(os.path.dirname(target) or "."):
                return f"目录不存在：{os.path.dirname(target)}"
        if fmt.container and not self.layer_name_edit.text().strip():
            return "写进库/地理数据库时，图层名不能为空"
        if fmt.extension != ".dxf":
            selected = self._selected_field_names()
            if not selected:
                return "请至少勾选一个导出字段（或改用 DXF 只导几何）"
            if fmt.extension == ".shp" and len(selected) > SHAPEFILE_MAX_FIELDS:
                return (
                    f"Shapefile 最多支持 {SHAPEFILE_MAX_FIELDS} 个字段，当前已选 "
                    f"{len(selected)} 个。请减少字段，或改导出为 "
                    "FileGDB / GeoPackage。"
                )
        return ""

    def spec(self) -> ExportSpec | None:
        """导出参数；表单不合法返回 ``None``（调用方先 ``exec`` 再取）。"""
        fmt = self._current_format()
        if fmt is None or self._validation_error():
            return None
        target = self.path_edit.text().strip()
        if fmt.extension == ".dxf":
            # DXF 不写属性表：不带字段子集 / 导出名
            field_names = None
            field_export_names = None
        else:
            selected = self._selected_field_names()
            mapping = self._export_name_map()
            field_names = tuple(selected)
            field_export_names = tuple(
                mapping.get(name, name) for name in selected
            )
        mode = self._filter_mode()
        return ExportSpec(
            path=os.path.dirname(target) or ".",
            file_name=os.path.basename(target),
            driver=fmt.driver,
            extension=fmt.extension,
            layer_name=self.layer_name_edit.text().strip() or os.path.splitext(os.path.basename(target))[0],
            # 只有「自定义条件」才带表达式；「选中数据」交给 selected_only
            expression=self._expression if mode == "expression" else "",
            selected_only=(mode == "selected"),
            field_names=field_names,
            field_export_names=field_export_names,
            convert_dict=bool(self.dict_check.isChecked()),
        )


def load_exported_layer(parent: QWidget | None, target: str, spec: ExportSpec) -> bool:
    """把刚导出的文件加载进当前工程（「导出后加载到地图」用）。"""
    from qgis.core import QgsProject, QgsVectorLayer

    ext = str(spec.extension or os.path.splitext(target)[1]).lower()
    if ext == ".xlsx":
        return False  # Excel 只有属性表，加载到地图没有意义
    layer = None
    error = ""
    if ext in (".gpkg", ".sqlite", ".sqlite3", ".db"):
        from app.core.util.local_layer_loader import open_local_vector_table

        layer, error = open_local_vector_table(target, spec.layer_name)
        if layer is None:
            # 表名大小写 / 驱动改写导致按名打不开时，退到「库里的第一个图层」
            from app.core.util.sublayers import list_sublayers, open_sublayer

            try:
                sublayers = list_sublayers(target)
            except Exception as exc:  # noqa: BLE001
                sublayers = []
                error = f"{error}；{exc}"
            match = next(
                (item for item in sublayers if item.name == spec.layer_name),
                sublayers[0] if sublayers else None,
            )
            if match is not None:
                layer, error = open_sublayer(target, match)
    elif ext == ".gdb":
        from app.core.util.sublayers import list_sublayers, open_sublayer

        try:
            sublayers = list_sublayers(target)
        except Exception as exc:  # noqa: BLE001
            sublayers, error = [], str(exc)
        match = next(
            (item for item in sublayers if item.name == spec.layer_name),
            sublayers[0] if sublayers else None,
        )
        if match is not None:
            layer, error = open_sublayer(target, match)
        else:
            error = error or "导出文件里没有找到图层"
    else:
        name = os.path.splitext(os.path.basename(target))[0]
        layer = QgsVectorLayer(target, name, "ogr")
        if not layer.isValid():
            error = "打不开导出的文件"
    if layer is None or not layer.isValid():
        if layer is not None:
            layer.deleteLater()
        if parent is not None:
            MessageBox.warning(
                parent,
                "加载到地图",
                f"导出成功，但加载到地图失败：\n{target}\n{error}",
            )
        return False
    QgsProject.instance().addMapLayer(layer)
    logger.info("导出后加载到地图：%s", target)
    return True


def export_with_progress(
    parent: QWidget | None,
    layer,
    spec: ExportSpec,
    *,
    load_after: bool = False,
) -> str | None:
    """带进度提示执行导出（数据管理 / 图层右键「导出」共用）。

    导出在 UI 线程里逐要素写盘，每 :data:`~app.core.domain.export_layer.PROGRESS_STEP`
    条回调一次 ``(done, total)``：进度框显示「已导出 N / M（xx%）」，并在回调里
    处理事件，避免大图层导出时窗口一直"未响应"。总条数未知时显示已处理条数。
    成功返回写出的路径；失败弹提示并返回 ``None``。``load_after`` 为真时
    导出成功后把结果加载进当前工程。
    """
    progress = QProgressDialog(parent)
    progress.setWindowTitle("导出数据")
    progress.setLabelText("正在准备导出…")
    progress.setRange(0, 0)  # 条数未知时先转圈，拿到总数后切成确定进度
    progress.setMinimumWidth(420)
    progress.setCancelButton(None)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.setMinimumDuration(0)
    # 导出循环里会 processEvents：用应用级模态挡住"再点一次导出 / 关父窗"，
    # 否则可能在 processEvents 期间重入导出。
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    progress.show()
    QApplication.processEvents()

    def _show_progress(done: int, total: int, text: str) -> None:
        """显示具体数值：有总数就 N / M + 百分比，没有就显示已处理条数。"""
        try:
            done = max(0, int(done))
            total = int(total)
        except (TypeError, ValueError):
            done, total = 0, -1
        if total > 0:
            if progress.maximum() != total:
                progress.setRange(0, total)
            progress.setValue(min(done, total))
            percent = min(100, int(done * 100 / total))
            progress.setLabelText(f"{text}  {done} / {total}（{percent}%）")
        else:
            if progress.maximum() != 0:
                progress.setRange(0, 0)
            progress.setLabelText(text if done <= 0 else f"{text}  已处理 {done} 条")
        QApplication.processEvents()

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    error_message = ""
    target = None
    try:
        try:
            target = export_vector_layer(layer, spec, progress=_show_progress)
        except ExportLayerError as exc:
            logger.warning("导出数据失败：%s", exc)
            error_message = f"导出失败：{exc}"
        except Exception as exc:  # noqa: BLE001 —— 任何意外都要让用户看到
            logger.error("导出数据异常：%s", exc, exc_info=True)
            error_message = f"导出失败（{type(exc).__name__}）：{exc}"
    finally:
        QApplication.restoreOverrideCursor()
        if progress.isVisible():
            progress.close()
        QApplication.processEvents()
    # **先关进度框再弹错误**：反过来的话，错误框是模态的，进度框会一直挂在
    # 后面转到用户点「确定」为止（用户报过"导出进度一直卡住"）。
    if error_message:
        MessageBox.warning(parent, "导出数据", error_message)
        return None
    loaded = False
    if load_after:
        loaded = load_exported_layer(parent, target, spec)
    MessageBox.information(
        parent,
        "导出数据",
        f"已导出{'（含字典）' if spec.convert_dict else ''}：\n{target}"
        + ("\n已加载到地图。" if loaded else ""),
    )
    return target


__all__ = ["ExportLayerDialog", "export_with_progress", "load_exported_layer"]
