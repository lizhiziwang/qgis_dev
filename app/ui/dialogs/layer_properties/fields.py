"""图层属性「字段」页。

仿 QGIS 原生 vector layer properties → Fields：7 列表格显示当前图层的字段，
可改字段别名（双击）、选主显示字段（combo）、加字段。筛选只是视图过滤，不改
图层。对话框级的「确定 / 应用」推迟到 :meth:`FieldsTab.apply`（与「常规」页
的写法一致——只有「取消」才会丢修改）。

最后一列「操作」给每行两个按钮：

- **更新**：把该行的编辑结果立刻写回图层（字段名改名 + 字段别名）。pending
  行则重新打开字段对话框修改其定义。字段类型 / 长度 / 小数位数只读展示——改
  类型涉及数据重算，不在本页处理。
- **删除**：把该字段从图层删除（pending 行只移出行，不落库）。

这两个按钮是**立即生效**的（与延迟 apply 的其它编辑不同）：它们是显式操作，
点完就调用 :func:`_delete_field_from_layer` / :func:`_rename_field_in_layer`。
底层统一走「编辑态用 layer.edit buffer API、非编辑态用 provider API」的分支，
避免图层正在编辑时 ``dataProvider().addAttributes`` 返回 ``False``。

「显示」列 checkbox：**仅视觉呈现**，默认勾上；不联动
``QgsAttributeTableConfig``。
"""

from __future__ import annotations

from qgis.core import QgsField, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.dialogs.layer_properties.add_field_dialog import AddFieldDialog
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.common.field_combo import make_field_combo_searchable
from app.ui.common.modeless import show_modeless
from app.ui.common.widgets import CenteredCheckDelegate
from app.ui.tools.feature_attrs import field_alias, readonly_field_indices

logger = get_logger(__name__)

_HEADERS = ("显示", "字段名", "字段别名", "字段类型", "字段长度", "小数位数", "操作")

#: 「操作」列（更新 / 删除 按钮）
_ACTION_COL = len(_HEADERS) - 1


# --------------------------------------------------------------------- 图层写操作
#
# QgsVectorLayer 的 addAttribute / deleteAttribute / renameAttribute 只在编辑态
# 可用（内部走 edit buffer，需要 commitChanges 落库）；provider 的
# addAttributes / deleteAttributes / renameAttributes 只在非编辑态可用，且对
# 部分 provider（如 OGR）在编辑态会直接返回 False。这里按编辑态分流，两条路都
# 走通，修复「添加字段后 apply 报 addAttributes 返回 False」。


def is_geojson_layer(layer) -> bool:
    """图层是否来自 GeoJSON 文件。

    GeoJSON 是文本交换格式，改字段需要重写整个文件；大图层上操作会长时间
    阻塞主线程。UI 在删除字段前据此提示用户改用 GeoPackage。
    """
    if layer is None:
        return False
    try:
        if layer.providerType() != "ogr":
            return False
        source = layer.source().split("|", 1)[0].split("?", 1)[0].strip().lower()
    except Exception:  # noqa: BLE001 —— 图层销毁 / source 异常时按普通图层处理
        return False
    return source.endswith((".geojson", ".json"))


def _add_field_to_layer(layer, field: QgsField) -> bool:
    """给图层加字段；编辑态走 edit buffer，否则直接落 provider。"""
    if layer.isEditable():
        return bool(layer.addAttribute(field))
    if not layer.dataProvider().addAttributes([field]):
        return False
    layer.updateFields()
    return True


def _delete_field_from_layer(layer, name: str) -> bool:
    """按字段名删字段。"""
    idx = layer.fields().indexOf(name)
    if idx < 0:
        return False
    if layer.isEditable():
        return bool(layer.deleteAttribute(idx))
    if not layer.dataProvider().deleteAttributes([idx]):
        return False
    layer.updateFields()
    return True


def _rename_field_in_layer(layer, old: str, new: str) -> bool:
    """按字段名改字段名；``new`` 与已有字段重名时返回 False。"""
    old_idx = layer.fields().indexOf(old)
    if old_idx < 0:
        return False
    if old == new:
        return True
    if layer.fields().indexOf(new) >= 0:  # 目标名已被别的字段占用
        return False
    if layer.isEditable():
        return bool(layer.renameAttribute(old_idx, new))
    if not layer.dataProvider().renameAttributes({old_idx: new}):
        return False
    layer.updateFields()
    return True


def _set_field_alias_on_layer(layer, name: str, alias: str) -> bool:
    """按字段名写别名（编辑/非编辑态都可用）。"""
    idx = layer.fields().indexOf(name)
    if idx < 0:
        return False
    layer.setFieldAlias(idx, alias)
    return True


class _RowActionButtons(QWidget):
    """行尾「更新 / 删除」按钮组。

    两个按钮都回调回 :class:`FieldsTab` 并带上自身；:class:`FieldsTab` 用
    :meth:`FieldsTab._row_of_actions` 反查所在行——这样删行 / 插行后行号变化
    也不会点错行。
    """

    def __init__(self, fields_tab: "FieldsTab", parent=None, readonly: bool = False,
                 editing: bool = False) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(4)
        # 必须靠上对齐：本控件在 ``__init__`` 时无父级，Qt 会先给它 640x480 的
        # 默认几何，布局据此把「更新 / 删除」摆到 y≈229；随后 ``setCellWidget``
        # 把它塞进 ~29px 高的单元格时，Windows 下布局不会重算，按钮被裁到行外
        # 变成不可见 / 不可点。AlignTop 让按钮永远贴在顶部，与容器高度无关。
        lay.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.update_btn = QPushButton("更新", self)
        self.update_btn.setObjectName("RowActionBtn")
        self.delete_btn = QPushButton("删除", self)
        self.delete_btn.setObjectName("RowActionDanger")
        for btn in (self.update_btn, self.delete_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(22)
            lay.addWidget(btn)
        lay.addStretch(1)
        self.update_btn.clicked.connect(lambda: fields_tab._on_row_update(self))
        self.delete_btn.clicked.connect(lambda: fields_tab._on_row_delete(self))
        # 编辑状态优先于只读：两者都禁止「更新 / 删除」，只是原因不同
        if editing:
            self.update_btn.setEnabled(False)
            self.update_btn.setToolTip("图层处于编辑状态，请先保存 / 停止编辑后再修改字段")
            self.delete_btn.setEnabled(False)
            self.delete_btn.setToolTip("图层处于编辑状态，请先保存 / 停止编辑后再删除字段")
        elif readonly:
            # 系统自维护 / 全局唯一标识字段：不允许改定义、也不允许删除
            self.update_btn.setEnabled(False)
            self.update_btn.setToolTip("系统自维护 / 唯一标识字段，不允许修改")
            self.delete_btn.setEnabled(False)
            self.delete_btn.setToolTip("系统自维护 / 唯一标识字段，不允许删除")


class FieldsTab(LayerPropertyTab):
    """图层属性「字段」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        #: 上次从图层读入时 combo 选中的字段。apply 只在用户改过选择时才写回：
        #: 打开图层属性后什么都不改直接点「确定」不应改动工程内容（否则会多出
        #: 一条“未保存”提示）；原显示表达式不是裸字段名时也不会被首字段覆盖。
        self._initial_display_field = None
        #: 用户是否动过主显示字段下拉（activated 只在用户选择时发，重填不发）
        self._display_touched = False
        self._build()
        if layer is not None:
            self.load(layer)

    # ------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 顶部：筛选 + 添加字段 + 刷新
        top = QHBoxLayout()
        top.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入字段名或别名筛选")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.search_edit, 1)
        self.add_btn = QPushButton("添加字段")
        self.add_btn.clicked.connect(self._on_add_field)
        top.addWidget(self.add_btn)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._on_refresh)
        top.addWidget(self.refresh_btn)
        root.addLayout(top)

        # 中部：字段表
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setItemDelegate(CenteredCheckDelegate(self.table))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        # 整表允许编辑触发；具体哪些列真能改，由 item flags 控制
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        # 表头行为：固定列用 Interactive（用户可拖）+ 默认宽度合理；
        # 「小数位数」这一列设 Stretch 模式，对话框变宽时把剩余空间吃掉——
        # 这样表格始终撑满父容器（与 QGIS 原生字段表观感一致）；「操作」列
        # 固定宽度，放两个小按钮。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        header.setStretchLastSection(False)
        # 各列默认宽度（像素）——「小数位数」列因为是 Stretch，会自动吃掉剩余空间
        for col, width in (
            (0, 50),    # 显示：checkbox
            (1, 110),   # 字段名
            (2, 180),   # 字段别名
            (3, 130),   # 字段类型
            (4, 90),    # 字段长度
            (6, 150),   # 操作：更新 / 删除
        ):
            self.table.setColumnWidth(col, width)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        # 表头整体略高一点，让标题不贴边
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.table, 1)

        # 底部：主显示字段 + 字段个数
        bot = QGroupBox(self)
        bot_layout = QHBoxLayout(bot)
        bot_layout.setContentsMargins(10, 6, 10, 6)
        bot_layout.setSpacing(8)
        lbl = QLabel("主显示字段:", bot)
        lbl.setObjectName("PropHint")
        bot_layout.addWidget(lbl)
        self.display_combo = QComboBox(bot)
        self.display_combo.setMinimumWidth(220)
        make_field_combo_searchable(self.display_combo)
        self.display_combo.activated.connect(self._on_display_field_activated)
        bot_layout.addWidget(self.display_combo, 1)
        self.count_label = QLabel("字段个数: —", bot)
        self.count_label.setObjectName("PropValue")
        bot_layout.addWidget(self.count_label)
        root.addWidget(bot)

    # ------------------------------------------------------------- 读取

    def load(self, layer) -> None:
        """从 ``layer`` 重填控件。「刷新」按钮和构造时都会触发。"""
        self._layer = layer
        if not isinstance(layer, QgsVectorLayer):
            self.table.setRowCount(0)
            self.count_label.setText("字段个数: —")
            self.display_combo.clear()
            self.add_btn.setEnabled(False)
            self._initial_display_field = None
            return
        # 编辑状态下不允许改 schema（添加字段）→ 置灰「添加字段」按钮
        editable = bool(layer.isEditable())
        self.add_btn.setEnabled(not editable)
        self.add_btn.setToolTip(
            "图层处于编辑状态，请先保存 / 停止编辑后再添加字段" if editable else ""
        )
        try:
            fields = layer.fields()
            self._populate_table(fields)
            self._populate_display_combo(fields, layer.displayExpression())
            self.count_label.setText(f"字段个数: {fields.size()}")
            # 重填后再跑一次筛选（保持之前的过滤状态）
            self._apply_filter(self.search_edit.text())
        except Exception as exc:  # noqa: BLE001
            # 探针确认过：本环境下层接口稳定，但留兜底防止 provider 临时失败
            logger.error("读取字段失败: %s", exc, exc_info=True)

    def _populate_table(self, fields) -> None:
        """把 ``QgsFields`` 写进 7 列表格。"""
        n = fields.size()
        self.table.setRowCount(n)
        readonly_names = self._readonly_field_names()
        editing = bool(self._layer is not None and self._layer.isEditable())
        for i in range(n):
            fld = fields.at(i)
            # 只读字段 / 编辑状态下都不允许改名、改别名
            locked = (fld.name() in readonly_names) or editing

            # 列 0：checkbox（默认勾上；不改 attributeTableConfig）
            cb_item = QTableWidgetItem()
            cb_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            cb_item.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, cb_item)

            # 列 1：字段名（可编辑；UserRole 存原始名，apply 时据此判断是否要改名）。
            # 只读字段（系统自维护 / 唯一标识）与编辑状态下都不可改名 → 去编辑标志。
            name_item = QTableWidgetItem(fld.name())
            name_item.setData(Qt.ItemDataRole.UserRole, fld.name())
            if locked:
                name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 1, name_item)

            # 列 2：字段别名（可编辑；只读字段 / 编辑状态下同样不可改）。
            # 用 ``field_alias`` 而不是裸 ``fld.alias()``：某些 GDB（实测 FileGDB 的
            # 中文别名）取回来是「UTF-8 字节被当成 Latin-1」的乱码，直接显示用户
            # 根本读不了。这里显示修好的文本，用户点「确定」时也会把修好的写进图层
            # 字段配置（``setFieldAlias`` 是**图层级**的，不落数据源）。
            alias_item = QTableWidgetItem(field_alias(fld))
            if locked:
                alias_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 2, alias_item)

            # 列 3：类型
            type_item = QTableWidgetItem(fld.typeName() or "")
            type_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(i, 3, type_item)

            # 列 4：长度（>0 才显示，与 QGIS 原生一致）
            length = fld.length()
            length_str = str(length) if length > 0 else ""
            length_item = QTableWidgetItem(length_str)
            length_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(i, 4, length_item)

            # 列 5：小数位数（>0 才显示）
            precision = fld.precision()
            prec_str = str(precision) if precision > 0 else ""
            prec_item = QTableWidgetItem(prec_str)
            prec_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(i, 5, prec_item)

        self._rebuild_action_cells()

    def _append_pending_row(self, spec: dict) -> None:
        """把「添加字段」对话框返回的 spec 追加成一行 pending 状态。

        pending 行在 :meth:`apply` 里通过 :func:`_add_field_to_layer` 真正写进
        图层；用户点「取消」或「刷新」→ 表格丢弃 pending 行（刷新是从图层重读）。
        """
        row = self.table.rowCount()
        self.table.insertRow(row)

        # 列 0：checkbox + 在 UserRole 上挂 pending 标记
        cb_item = QTableWidgetItem()
        cb_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        cb_item.setCheckState(Qt.CheckState.Checked)
        cb_item.setData(Qt.ItemDataRole.UserRole, {"pending": True, "spec": spec})
        self.table.setItem(row, 0, cb_item)

        # 列 1：字段名（只读；pending 改名走「更新」按钮打开对话框）
        name_item = QTableWidgetItem(spec["name"])
        name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        name_item.setData(Qt.ItemDataRole.UserRole, spec["name"])
        self.table.setItem(row, 1, name_item)

        # 列 2：字段别名（可编辑；用户可以再调整）
        alias_item = QTableWidgetItem(spec.get("alias") or "")
        self.table.setItem(row, 2, alias_item)

        # 列 3：类型（只读）
        type_item = QTableWidgetItem(spec["type_name"])
        type_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        self.table.setItem(row, 3, type_item)

        # 列 4：长度（>0 才显示）
        length = spec.get("length", 0)
        length_item = QTableWidgetItem(str(length) if length > 0 else "")
        length_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        self.table.setItem(row, 4, length_item)

        # 列 5：小数位数
        precision = spec.get("precision", 0)
        prec_item = QTableWidgetItem(str(precision) if precision > 0 else "")
        prec_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        self.table.setItem(row, 5, prec_item)

        # 同步进「主显示字段」combo —— 让用户也能把新字段选作主显示
        self.display_combo.addItem(
            f"{spec['name']} · {spec.get('alias') or spec['name']}",
            spec["name"],
        )
        # 字段个数立即反映在标签上（pending 行也算入已加的表行数）
        self.count_label.setText(f"字段个数: {self.table.rowCount()}")
        self._rebuild_action_cells()

    def _readonly_field_names(self) -> set[str]:
        """只读字段名集合（系统自维护主键 + 全局唯一标识）：不可改定义、不可删除。"""
        if self._layer is None:
            return set()
        fields = self._layer.fields()
        return {fields.at(i).name() for i in readonly_field_indices(fields)}

    def _field_name_of_row(self, row: int) -> str:
        """某行的字段名（列 1 文本）；没有该行 / 空返回 ``""``。"""
        item = self.table.item(row, 1)
        return (item.text() or "").strip() if item is not None else ""

    def _rebuild_action_cells(self) -> None:
        """给每行（重新）挂「操作」列按钮。

        插行 / 删行会把行号打乱，这里统整一遍；按钮回调带自身，靠
        :meth:`_row_of_actions` 反查当前行，所以行号漂移也不会点错。
        只读字段（系统自维护 / 唯一标识）**禁用**更新与删除按钮；图层处于编辑
        状态时，已在图层里的字段行也**禁用**更新与删除（pending 新行不受影响，
        因为它的删除只是移出未落库的行）。
        """
        readonly_names = self._readonly_field_names()
        editing = bool(self._layer is not None and self._layer.isEditable())
        for row in range(self.table.rowCount()):
            if self._is_pending_row(row) is not None:
                readonly, editing_row = False, False
            else:
                readonly = self._field_name_of_row(row) in readonly_names
                editing_row = editing
            widget = _RowActionButtons(
                self, self.table.viewport(), readonly=readonly, editing=editing_row
            )
            self.table.setCellWidget(row, _ACTION_COL, widget)
            # 单元格尺寸确定后显式激活一次布局，让按钮按行高 / 列宽落位。
            lay = widget.layout()
            if lay is not None:
                lay.activate()

    def _row_of_actions(self, widget: QWidget) -> int:
        """反查按钮组所在行；找不到返回 -1。"""
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, _ACTION_COL) is widget:
                return row
        return -1

    def _is_pending_row(self, row: int) -> dict | None:
        """row 是否是 pending 添加；是的话返回 spec，否则返回 None。"""
        cb = self.table.item(row, 0)
        if cb is None:
            return None
        data = cb.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict) and data.get("pending"):
            return data.get("spec")
        return None

    def _populate_display_combo(self, fields, current_expr: str) -> None:
        """每个字段填进 combo。itemData 存原始字段名（apply 时直接 setDisplayExpression）。"""
        self.display_combo.clear()
        for i in range(fields.size()):
            name = fields.at(i).name()
            alias = field_alias(fields.at(i)) or name
            self.display_combo.addItem(f"{name} · {alias}", name)
        # 选中 current；current 可能不是裸字段名（如 "concat(a, b)"），找不着就保持 index=0
        if current_expr:
            for i in range(self.display_combo.count()):
                if self.display_combo.itemData(i) == current_expr:
                    self.display_combo.setCurrentIndex(i)
                    break
        # 记下读入时的选择，apply 据此判断用户是否真的改过
        self._initial_display_field = self.display_combo.currentData()
        self._display_touched = False

    def _on_display_field_activated(self, _index: int) -> None:
        """用户在下拉里选过主显示字段；重填控件走 setCurrentIndex，不会触发这里。"""
        self._display_touched = True

    # ------------------------------------------------------------- 交互

    def _apply_filter(self, text: str) -> None:
        """按字段名 / 别名 子串过滤（大小写不敏感）。空文本 = 全显。"""
        needle = (text or "").strip().lower()
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            name_item = self.table.item(row, 1)
            alias_item = self.table.item(row, 2)
            name = (name_item.text() if name_item else "").lower()
            alias = (alias_item.text() if alias_item else "").lower()
            self.table.setRowHidden(
                row, needle not in name and needle not in alias
            )

    def _on_refresh(self) -> None:
        """「刷新」按钮：从图层重读一遍（用户在 QGIS 里改了字段结构也跟上）。

        注意会丢弃所有 pending 添加行——刷新是从图层重读，pending 在图层里
        还不存在。
        """
        if self._layer is None:
            return
        self.load(self._layer)

    def _on_add_field(self) -> None:
        """「添加字段」按钮：弹子对话框拿 spec，append 成 pending 行。

        校验：字段名不能与图层已有字段或已有的 pending 行重名（重名 →
        QMessageBox.warning，不加）。
        """
        if self._layer is None:
            return
        if self._layer.isEditable():
            QMessageBox.information(
                self, "添加字段",
                "图层处于编辑状态，请先保存 / 停止编辑后再添加字段。",
            )
            return
        def _add(dlg) -> None:
            spec = dlg.field_spec()
            if spec is None:
                QMessageBox.warning(self, "添加字段", "字段名不能为空")
                return
            new_name = spec["name"]
            # 重名校验：图层里已有 + 表格里已有 pending
            existing = {self.table.item(r, 1).text() for r in range(self.table.rowCount())
                        if self.table.item(r, 1)}
            if new_name in existing:
                QMessageBox.warning(
                    self, "添加字段",
                    f"字段名「{new_name}」已存在（与图层字段或已添加的 pending 行冲突）",
                )
                return
            self._append_pending_row(spec)

        show_modeless(
            self,
            ("add_field", id(self)),
            lambda: AddFieldDialog(self),
            on_accepted=_add,
        )

    # ------------------------------------------------------------- 行操作

    def _on_row_update(self, widget: QWidget) -> None:
        """「更新」按钮：立即把该行写回图层（改名 + 别名）；pending 行改定义。"""
        row = self._row_of_actions(widget)
        if row < 0 or self.table.item(row, 1) is None:
            return
        spec = self._is_pending_row(row)
        if spec is not None:
            self._edit_pending_row(row, spec)
            return

        if self._layer is None:
            return
        if self._field_name_of_row(row) in self._readonly_field_names():
            QMessageBox.information(self, "更新字段", "系统自维护 / 唯一标识字段，不允许修改。")
            return
        if self._layer.isEditable():
            QMessageBox.information(
                self, "更新字段",
                "图层处于编辑状态，请先保存 / 停止编辑后再修改字段。",
            )
            return
        name_item = self.table.item(row, 1)
        alias_item = self.table.item(row, 2)
        orig = name_item.data(Qt.ItemDataRole.UserRole)
        orig = orig if isinstance(orig, str) and orig else name_item.text()
        new_name = (name_item.text() or "").strip()
        new_alias = (alias_item.text() or "").strip() if alias_item else ""

        if not new_name:
            QMessageBox.warning(self, "更新字段", "字段名不能为空")
            return
        try:
            if new_name != orig and not _rename_field_in_layer(self._layer, orig, new_name):
                QMessageBox.warning(
                    self, "更新字段",
                    f"重名字段「{orig}」→「{new_name}」失败（名称冲突或 provider 不支持）",
                )
                return
            if not _set_field_alias_on_layer(self._layer, new_name, new_alias):
                QMessageBox.warning(self, "更新字段", f"更新字段「{new_name}」的别名失败")
                return
        except Exception as exc:  # noqa: BLE001
            logger.error("更新字段失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "更新字段", f"更新字段失败：{exc}")
            return

        name_item.setData(Qt.ItemDataRole.UserRole, new_name)
        self._rename_combo_item(orig, new_name, new_alias)
        self._refresh_fields_after_change()

    def _on_row_delete(self, widget: QWidget) -> None:
        """「删除」按钮：从图层删除该字段；pending 行只移出行。"""
        row = self._row_of_actions(widget)
        if row < 0 or self.table.item(row, 1) is None:
            return
        name = (self.table.item(row, 1).text() or "").strip()
        spec = self._is_pending_row(row)

        if spec is not None:
            # pending 行：直接丢，不落库
            self._drop_row(row)
            self._drop_combo_item(spec["name"])
            self.count_label.setText(f"字段个数: {self.table.rowCount()}")
            return

        if self._layer is None or not name:
            return
        if name in self._readonly_field_names():
            QMessageBox.information(self, "删除字段", "系统自维护 / 唯一标识字段，不允许删除。")
            return
        if self._layer.isEditable():
            QMessageBox.information(
                self, "删除字段",
                "图层处于编辑状态，请先保存 / 停止编辑后再删除字段。",
            )
            return
        if is_geojson_layer(self._layer):
            answer = QMessageBox.warning(
                self,
                "删除字段",
                f"GeoJSON 是文本交换格式，删除字段会重写整个文件，"
                f"大图层可能长时间无响应。\n\n建议先将图层另存为 GeoPackage 再编辑。\n\n"
                f"仍要删除字段「{name}」吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            answer = QMessageBox.question(
                self, "删除字段", f"确定从图层中删除字段「{name}」？该操作不可撤销。"
            )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            ok = _delete_field_from_layer(self._layer, name)
        except Exception as exc:  # noqa: BLE001
            logger.error("删除字段失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "删除字段", f"删除字段失败：{exc}")
            return
        if not ok:
            QMessageBox.warning(
                self, "删除字段",
                f"删除字段「{name}」失败（provider 不支持或图层只读）",
            )
            return
        self._drop_row(row)
        self._drop_combo_item(name)
        self.count_label.setText(f"字段个数: {self.table.rowCount()}")
        self._refresh_fields_after_change()

    def _edit_pending_row(self, row: int, spec: dict) -> None:
        """pending 行「更新」：打开字段对话框改定义，替换该行。"""
        def _apply(dlg) -> None:
            new_spec = dlg.field_spec()
            if new_spec is None:
                QMessageBox.warning(self, "编辑字段", "字段名不能为空")
                return
            # 无模态窗口开着时表格可能被改：按原字段名重新定位 pending 行
            target_row = row
            item = self.table.item(target_row, 1)
            if item is None or item.text() != spec.get("name"):
                target_row = next(
                    (
                        r
                        for r in range(self.table.rowCount())
                        if self.table.item(r, 1)
                        and self.table.item(r, 1).text() == spec.get("name")
                    ),
                    -1,
                )
            if target_row < 0:
                QMessageBox.information(
                    self, "编辑字段", "该 pending 字段已不存在，修改未应用。"
                )
                return
            others = {
                self.table.item(r, 1).text()
                for r in range(self.table.rowCount())
                if r != target_row and self.table.item(r, 1)
            }
            if new_spec["name"] in others:
                QMessageBox.warning(
                    self, "编辑字段", f"字段名「{new_spec['name']}」已存在"
                )
                return

            cb_item = self.table.item(target_row, 0)
            cb_item.setData(
                Qt.ItemDataRole.UserRole, {"pending": True, "spec": new_spec}
            )
            self.table.item(target_row, 1).setText(new_spec["name"])
            self.table.item(target_row, 1).setData(
                Qt.ItemDataRole.UserRole, new_spec["name"]
            )
            self.table.item(target_row, 2).setText(new_spec.get("alias") or "")
            self.table.item(target_row, 3).setText(new_spec["type_name"])
            length = new_spec.get("length", 0)
            self.table.item(target_row, 4).setText(
                str(length) if length > 0 else ""
            )
            precision = new_spec.get("precision", 0)
            self.table.item(target_row, 5).setText(
                str(precision) if precision > 0 else ""
            )
            self._rename_combo_item(
                spec["name"], new_spec["name"], new_spec.get("alias") or ""
            )
            self._apply_filter(self.search_edit.text())

        show_modeless(
            self,
            ("edit_field", id(self), row, spec.get("name")),
            lambda: AddFieldDialog(self, initial=spec),
            on_accepted=_apply,
        )

    # ------------------------------------------------------------- 小工具

    def _drop_row(self, row: int) -> None:
        """删表行并重挂操作按钮（行号会漂移）。"""
        self.table.removeRow(row)
        self._rebuild_action_cells()

    def _drop_combo_item(self, name: str) -> None:
        """从「主显示字段」combo 里移除 itemData == name 的项。"""
        for i in range(self.display_combo.count()):
            if self.display_combo.itemData(i) == name:
                self.display_combo.removeItem(i)
                return

    def _rename_combo_item(self, old: str, new: str, alias: str) -> None:
        """字段改名 / 改别名后同步 combo 的显示文本与 itemData。"""
        for i in range(self.display_combo.count()):
            if self.display_combo.itemData(i) == old:
                self.display_combo.setItemText(i, f"{new} · {alias or new}")
                self.display_combo.setItemData(i, new)
                return

    def _refresh_fields_after_change(self) -> None:
        """立即操作后轻量刷新：重绘图层，让画布跟上新字段结构。"""
        try:
            self._layer.triggerRepaint()
        except Exception as exc:  # noqa: BLE001
            logger.warning("刷新图层失败: %s", exc)

    # ------------------------------------------------------------- 写回

    def apply(self, layer) -> bool:
        """把控件值写回图层。统一 try/except 兜底——任何字段写失败都弹窗 + 返回 False。

        写回分三步：
        1. 收集所有 pending 行（用户通过「添加字段」按钮加的），逐个
           :func:`_add_field_to_layer` 落到图层（编辑态走 edit buffer，否则
           直接落 provider）；成功后把 pending 标记摘掉（重复点「应用」不会再加）；
        2. 遍历整张表（原始字段 + 新加字段），按 UserRole 里的原始名判断是否改名
           → :func:`_rename_field_in_layer`，再写别名——这样新字段的别名（用户
           在第 2 列手填的）也能跟上去；
        3. 写主显示字段 ``setDisplayExpression``。
        """
        if not isinstance(layer, QgsVectorLayer):
            return True
        try:
            # 1) 逐个添加 pending 字段
            pending: list[tuple[int, dict]] = []
            for row in range(self.table.rowCount()):
                spec = self._is_pending_row(row)
                if spec is not None:
                    pending.append((row, spec))
            for row, s in pending:
                field = QgsField(
                    s["name"], s["qtype"], s["type_name"], s["length"], s["precision"]
                )
                if not _add_field_to_layer(layer, field):
                    raise RuntimeError(
                        "添加字段失败：provider 不允许改 schema（图层可能只读）"
                    )
                # 摘 pending 标记，并让 UserRole 的原始名 = 新名
                cb_item = self.table.item(row, 0)
                if cb_item is not None:
                    cb_item.setData(Qt.ItemDataRole.UserRole, None)
                name_item = self.table.item(row, 1)
                if name_item is not None:
                    name_item.setData(Qt.ItemDataRole.UserRole, s["name"])

            # 2) 写改名 + 别名：按字段名查索引（addAttributes 后 layer.fields() 已含新字段）
            layer_fields = layer.fields()
            readonly_names = {layer_fields.at(i).name() for i in readonly_field_indices(layer_fields)}
            for row in range(self.table.rowCount()):
                name_item = self.table.item(row, 1)
                if name_item is None:
                    continue
                new_name = (name_item.text() or "").strip()
                alias_item = self.table.item(row, 2)
                new_alias = (alias_item.text() or "").strip() if alias_item else ""
                if not new_name:
                    raise RuntimeError("字段名不能为空")
                orig = name_item.data(Qt.ItemDataRole.UserRole)
                orig_name = orig if isinstance(orig, str) and orig else new_name
                if orig_name in readonly_names:
                    continue  # 只读字段：不允许改名 / 改别名
                if isinstance(orig, str) and orig and orig != new_name:
                    if not _rename_field_in_layer(layer, orig, new_name):
                        raise RuntimeError(
                            f"重命名字段「{orig}」→「{new_name}」失败（名称冲突或 provider 不支持）"
                        )
                    name_item.setData(Qt.ItemDataRole.UserRole, new_name)
                _set_field_alias_on_layer(layer, new_name, new_alias)

            # 3) 写主显示字段：combo 的 itemData 存的就是字段名；用户没选过且选择
            #    没变就不动，避免“打开属性页直接确定”也把工程置脏
            name = self.display_combo.currentData()
            if name and (self._display_touched or name != self._initial_display_field):
                layer.setDisplayExpression(name)
                self._initial_display_field = name
                self._display_touched = False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("应用字段属性失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用字段属性失败：{exc}")
            return False
