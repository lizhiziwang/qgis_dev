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

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.create_data import available_drivers
from app.core.domain.export_layer import (
    EXPORT_EXTENSIONS,
    SHAPEFILE_MAX_FIELDS,
    ExportSpec,
    existing_target_description,
    shapefile_field_names,
)
from app.core.util.dict_values import layer_dict_field_names
from app.core.util.vector_formats import VECTOR_FORMATS, VectorFormat, resolve_format
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import open_file_dir, save_file
from app.ui.dialogs.organize_columns import OrganizeColumnsDialog
from app.ui.dialogs.query_builder import QueryBuilderDialog

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

logger = get_logger(__name__)

#: 下拉顺序（按用户点名要的顺序；GeoPackage 排最后但作为默认 —— 它不丢字段名 / 别名）
_EXPORT_ORDER: tuple[str, ...] = (".shp", ".gdb", ".gpkg", ".sqlite")

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


def _export_formats() -> list[VectorFormat]:
    """本机真能写出的四种导出格式（顺序按 :data:`_EXPORT_ORDER`）。"""
    writers = available_drivers()
    found: dict[str, VectorFormat] = {}
    for fmt in VECTOR_FORMATS:
        if fmt.extension not in EXPORT_EXTENSIONS:
            continue
        resolved = resolve_format(fmt, writers)
        if resolved is not None:
            found[resolved.extension] = resolved
    return [found[ext] for ext in _EXPORT_ORDER if ext in found]


class ExportLayerDialog(BaseDialog):
    """导出参数表单：格式 / 保存位置 / 图层名 / 条件。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        layer: QgsVectorLayer,
        default_dir: str = "",
    ) -> None:
        super().__init__(parent, "导出数据")
        self._layer = layer
        self._default_dir = default_dir
        #: 当前筛选条件（QGIS 表达式；空串 = 全部要素）
        self._expression = ""
        #: Shapefile 要导出的字段名（按图层顺序）；``None`` = 全部字段。
        #: 图层字段超过 :data:`SHAPEFILE_MAX_FIELDS` 时切到 Shapefile 会默认裁到前
        #: 255 个（用户可点「选择字段…」改），否则 ArcMap 打不开（用户报过）。
        self._shapefile_fields: list[str] | None = None
        #: 挂了字典的字段名（GDB 写属性域 / GPKG、SQLite 附带字典表用）
        self._dict_field_names = layer_dict_field_names(layer)
        self._formats = _export_formats()
        if not self._formats:
            logger.warning("导出：本机没有可用的矢量写驱动")
        self.setMinimumSize(0, 0)
        self.resize(620, 380)
        self._build()
        self._apply_format_defaults()

    # --------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QLabel()
        count = self._layer.featureCount()
        count_text = f"当前 {count} 个要素" if count >= 0 else "要素数未知"
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

        # 导出字段（Shapefile 专用）：字段数必须 ≤255，否则 ArcMap 打不开
        field_row = QHBoxLayout()
        field_row.setContentsMargins(0, 0, 0, 0)
        field_row.setSpacing(6)
        self.field_label = QLabel()
        self.field_label.setWordWrap(True)
        field_row.addWidget(self.field_label, 1)
        self.field_btn = QPushButton("选择字段…")
        self.field_btn.setToolTip(
            f"挑要写进 Shapefile 的字段（最多 {SHAPEFILE_MAX_FIELDS} 个；"
            "超过上限的文件 ArcMap 打不开）"
        )
        self.field_btn.clicked.connect(self._on_pick_fields)
        field_row.addWidget(self.field_btn)
        form.addRow("导出字段", field_row)

        # 字典转换：Shapefile 没有承载字典的位置 → 禁用；GDB 写成属性域，
        # GPKG / SQLite 附带字典表（并写字段元数据）。
        self.dict_check = QCheckBox("转换字典")
        self.dict_check.setToolTip(
            "把字段挂接的字典一起导出：FileGDB 写成属性域（coded value domain）；"
            "GeoPackage / SQLite 把字典表写进目标库。Shapefile 不支持。"
        )
        form.addRow("字典", self.dict_check)

        condition_row = QHBoxLayout()
        condition_row.setContentsMargins(0, 0, 0, 0)
        condition_row.setSpacing(6)
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
        form.addRow("筛选条件", condition_row)

        root.addLayout(form)

        self.hint = QLabel()
        self.hint.setObjectName("PropHint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        root.addStretch(1)

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
        # 字段选择只对 Shapefile 有意义：DBF 最多 255 列，超了 ArcMap 打不开
        is_shapefile = fmt.extension == ".shp"
        self.field_btn.setEnabled(is_shapefile)
        if is_shapefile:
            self._ensure_shapefile_fields()
        self._update_field_label(is_shapefile)
        # 字典转换：只对挂了字典的图层、且目标格式能承载字典时可用
        supports_dict = fmt.extension != ".shp"
        dict_ok = bool(self._dict_field_names) and supports_dict
        self.dict_check.setEnabled(dict_ok)
        # 有字典、目标格式也能承载时**默认勾选**（用户仍可手动取消）
        self.dict_check.setChecked(dict_ok)
        if not self._dict_field_names:
            self.dict_check.setText("转换字典（图层没有挂接字典）")
        elif not supports_dict:
            self.dict_check.setText("转换字典（Shapefile 不支持）")
        else:
            self.dict_check.setText(
                f"转换字典（{len(self._dict_field_names)} 个字段）"
            )
        hint = (
            "导出后可直接用本软件打开；带条件的导出只写符合条件的要素（图层本身的筛选不受影响）。"
            if is_container
            else self._shapefile_hint()
        )
        if dict_ok:
            hint += " 勾选「转换字典」后，GDB 会写入属性域，GeoPackage / SQLite 会附带字典表。"
        self.hint.setText(hint)

    def _shapefile_field_list(self) -> list[str]:
        """本次要写进 Shapefile 的字段名；``None`` 状态 = 全部字段。"""
        if self._shapefile_fields is not None:
            return list(self._shapefile_fields)
        return [field.name() for field in self._layer.fields()]

    def _ensure_shapefile_fields(self) -> None:
        """字段超过 DBF 上限时默认裁到前 255 个（用户可再手改）。"""
        if self._shapefile_fields is not None:
            return
        names = [field.name() for field in self._layer.fields()]
        if len(names) > SHAPEFILE_MAX_FIELDS:
            self._shapefile_fields = names[:SHAPEFILE_MAX_FIELDS]

    def _update_field_label(self, is_shapefile: bool) -> None:
        """「导出字段」行文案：全部 / 已选多少 / 是否超上限。"""
        total = len(self._layer.fields())
        if not is_shapefile:
            self.field_label.setText("（仅 Shapefile 需要选择字段；其他格式写入全部字段）")
            return
        selected = len(self._shapefile_field_list())
        text = f"全部 {total} 个字段" if selected == total else f"已选 {selected} / {total} 个字段"
        if selected > SHAPEFILE_MAX_FIELDS:
            text += f"（超过 Shapefile 上限 {SHAPEFILE_MAX_FIELDS} 个，无法导出）"
        elif total > SHAPEFILE_MAX_FIELDS:
            text += f"（Shapefile 最多 {SHAPEFILE_MAX_FIELDS} 个，其余字段不会写入）"
        self.field_label.setText(text)

    def _on_pick_fields(self) -> None:
        """「选择字段…」：勾选要写进 Shapefile 的字段。"""
        dialog = OrganizeColumnsDialog(
            self._layer,
            self._shapefile_field_list(),
            self,
            title=f"导出字段 - {self._layer.name()}",
            hint=(
                f"勾选要写进 Shapefile 的字段（最多 {SHAPEFILE_MAX_FIELDS} 个；"
                "超过上限的文件 ArcMap 打不开）："
            ),
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self._shapefile_fields = dialog.checked_field_names()
        dialog.deleteLater()
        if accepted:
            self._update_field_label(True)
            self.hint.setText(self._shapefile_hint())

    def _shapefile_hint(self) -> str:
        """Shapefile 的提示：字段数上限 + 字段名会按 DBF 规则改写（中文名截到 10 字节）。"""
        total = len(self._layer.fields())
        selected = len(self._shapefile_field_list())
        parts = ["Shapefile 只存单图层"]
        parts.append(
            f"将写入 {selected} / {total} 个字段"
            if selected != total
            else f"共 {total} 个字段"
        )
        if selected > SHAPEFILE_MAX_FIELDS:
            parts.append(
                f"超过 {SHAPEFILE_MAX_FIELDS} 个字段的文件 ArcMap 打不开，请减少字段"
            )
        selected_names = set(self._shapefile_field_list())
        renamed = {
            old: new
            for old, new in shapefile_field_names(
                [field for field in self._layer.fields() if field.name() in selected_names]
            ).items()
            if old != new
        }
        if renamed:
            preview = "、".join(f"{old}→{new}" for old, new in list(renamed.items())[:3])
            extra = f" 等 {len(renamed)} 个" if len(renamed) > 3 else ""
            parts.append(f"字段名 ≤10 字节，以下会被改写：{preview}{extra}")
        return "；".join(parts) + "。"

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
        dialog = QueryBuilderDialog(self._layer, self, apply_to_layer=False)
        dialog.sqlConfirmed.connect(self._set_condition)
        dialog.exec()
        dialog.deleteLater()

    def _set_condition(self, condition: str) -> None:
        self._expression = (condition or "").strip()
        self.condition_edit.setText(self._expression)

    def _on_clear_condition(self) -> None:
        self._expression = ""
        self.condition_edit.clear()

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
            ret = QMessageBox.question(
                self,
                "替换已存在的数据",
                f"{existing}。\n\n确定替换？\n（只覆盖这一份；库里的其他图层不受影响）"
                if spec.is_container
                else f"{existing}。\n\n确定覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        # Shapefile 会裁掉字段：明确问一句，别让用户以为全部字段都写进去了
        if spec.driver == "ESRI Shapefile":
            total = len(self._layer.fields())
            selected = len(self._shapefile_field_list())
            if selected < total:
                ret = QMessageBox.question(
                    self,
                    "导出字段",
                    f"本次只导出 {selected} 个字段，其余 {total - selected} 个字段"
                    "不会写进 Shapefile。\n\n确定继续？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
        self.accept()

    def _validation_error(self) -> str:
        fmt = self._current_format()
        if fmt is None:
            return "本机没有可用的矢量写驱动，导不出去"
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
        if fmt.extension == ".shp":
            selected = len(self._shapefile_field_list())
            if selected > SHAPEFILE_MAX_FIELDS:
                return (
                    f"Shapefile 最多支持 {SHAPEFILE_MAX_FIELDS} 个字段，当前已选 "
                    f"{selected} 个。请点「选择字段…」减少字段，或改导出为 "
                    "FileGDB / GeoPackage。"
                )
        return ""

    def spec(self) -> ExportSpec | None:
        """导出参数；表单不合法返回 ``None``（调用方先 ``exec`` 再取）。"""
        fmt = self._current_format()
        if fmt is None or self._validation_error():
            return None
        target = self.path_edit.text().strip()
        # Shapefile 只在用户改过字段时传子集；没改（且不超上限）保持零拷贝
        field_names = (
            tuple(self._shapefile_fields)
            if fmt.extension == ".shp" and self._shapefile_fields is not None
            else None
        )
        return ExportSpec(
            path=os.path.dirname(target) or ".",
            file_name=os.path.basename(target),
            driver=fmt.driver,
            extension=fmt.extension,
            layer_name=self.layer_name_edit.text().strip() or os.path.splitext(os.path.basename(target))[0],
            expression=self._expression,
            field_names=field_names,
            convert_dict=bool(self.dict_check.isChecked()),
        )


__all__ = ["ExportLayerDialog"]
