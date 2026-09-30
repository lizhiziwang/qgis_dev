"""「复制到」Dialog：选目标图层 + 逐字段指定「源字段 → 目标字段」映射。

对齐 B/S 端 ``CopyToModal.vue`` 的版式与逻辑：

- 头部：「源图层(XX图层，选中要素 N 个)」；
- **目标图层**下拉：只列**几何类型与源相同**、且不是源图层自己的矢量图层；
- 「复制要素的同时复制指定字段属性」勾选框（**默认不勾**）——不勾时只搬几何，
  目标字段全部走自动赋值规则；
- 勾上后才出现**字段映射表**：一行一个目标字段，每行选一个源字段；
  **全选**复选框放在表格第一列的**表头**里（同 B/S，在 headerCell 里渲染）；
  行内做类型兼容校验，不兼容的红字提示并禁用「确定」；
- 「**自动匹配同名字段**」：按「字段名相同（**忽略大小写**）+ 类型兼容」自动配上，
  匹配成功的行**排到前面**；一个都没匹配上就提示「未找到可匹配的字段」。

**性能**：源图层动辄 300+ 字段、映射表 180+ 行，若给每个下拉都预先灌满源字段，
实测构造要 **1.68 秒**（62,645 个下拉项）。所以源字段下拉是
:class:`_SourceFieldCombo`——**首次展开/聚焦时才填充**，并且做成了可输入过滤。

只做展示与收集，不碰图层写入（那是 :mod:`app.ui.tools.copy_to_layer` 的事）。
主题由全局 QSS 下发，本类不写 setStyleSheet。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import make_table
from app.ui.tools.copy_to_layer import (
    mappable_source_fields,
    mappable_target_fields,
    types_compatible,
)
from app.ui.tools.feature_attrs import field_alias

logger = get_logger(__name__)

#: 源字段下拉里「不复制该字段」的选项（``currentData()`` 为 None）
_NONE_TEXT = "（不复制）"

#: 表格列：勾选 / 目标字段 / 源字段 / 校验说明
_COL_CHECK, _COL_TARGET, _COL_SOURCE, _COL_ERROR = range(4)


class _SourceFieldCombo(QComboBox):
    """源字段下拉：首次展开 / 聚焦时才灌入全部源字段，并支持**输入过滤**。

    惰性填充是性能刚需：源图层 300+ 字段、映射表 180+ 行，构造时全塞满就是
    6 万多个下拉项、1.68 秒才弹得出来。改成"用到哪个填哪个"后，单次填充只有
    300 项，毫秒级。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, str]] = []
        self._filled = False
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItem(_NONE_TEXT, None)

    def set_items(self, items: Sequence[tuple[str, str]]) -> None:
        """登记候选项 ``[(显示文本, 字段名), ...]``；真正写进下拉是**惰性**的。"""
        self._items = list(items)

    def _ensure_filled(self) -> None:
        if self._filled or not self._items:
            return
        self._filled = True
        # 加项会改动文本，先记下来再还原（用户可能已经敲了字才触发填充）
        text = self.currentText()
        # 已经单独补过的候选项（见 select_source）不要重复加
        present = {str(self.itemData(i) or "").lower() for i in range(self.count())}
        for label, name in self._items:
            if name.lower() in present:
                continue
            self.addItem(label, name)
        if text and text != _NONE_TEXT:
            self.setEditText(text)
        # 输入过滤：包含匹配 + 忽略大小写（对齐 B/S 的 show-search + filter-option）
        completer = QCompleter(self.model(), self)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setMaxVisibleItems(12)
        self.setCompleter(completer)

    def select_source(self, source_name: str, label: str = "") -> None:
        """把某个源字段设成当前选中项，**不触发全量填充**。

        「自动匹配」要给可能上百行回填选中值；若为此把每个下拉都灌满，6 万项又
        回来了。这里只在未填充时补**这一个**候选项，其余等用户真去展开时再填。

        :param label: 该项的显示文本（"别名（字段名）"）；为空时退化成字段名。
        """
        for i in range(self.count()):
            if str(self.itemData(i) or "").lower() == source_name.lower():
                self.setCurrentIndex(i)
                return
        self.addItem(label or source_name, source_name)
        self.setCurrentIndex(self.count() - 1)

    def showPopup(self) -> None:  # noqa: N802 —— Qt 命名
        self._ensure_filled()
        super().showPopup()

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._ensure_filled()
        super().focusInEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """滚轮**不切换选中项**，把事件让给外层表格去滚动。

        下拉处在可滚动的映射表里，鼠标划过时随手一滚就把某行的源字段改掉了，
        很容易误操作（"聚焦在源字段下拉框时滚轮不要自动切换内容"）。展开的弹出
        列表是独立的视图，它自己的滚动不受这里影响。
        """
        event.ignore()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        self._ensure_filled()
        super().keyPressEvent(event)


class CopyToLayerDialog(BaseDialog):
    """把选中要素复制到另一个图层，并可逐字段搬运属性。"""

    def __init__(
        self,
        source_layer: QgsVectorLayer,
        target_layers: Sequence[QgsVectorLayer],
        feature_count: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, title="复制到")
        self.setMinimumSize(0, 0)
        self.resize(680, 560)
        self.setModal(True)
        self._source = source_layer
        self._target_layers = list(target_layers)
        self._feature_count = int(feature_count)
        #: 目标字段名 → 该行的源字段下拉
        self._combos: dict[str, QComboBox] = {}
        #: 表格当前的行顺序（目标字段名）
        self._rows: list[str] = []
        self._source_fields: list = []
        #: 源字段下拉项 ``[(显示文本, 字段名), ...]`` —— 每个源字段只格式化一次
        self._source_items: list[tuple[str, str]] = []
        self._target_field_by_name: dict = {}
        #: 勾上「复制字段属性」时的窗口高度（收起映射表时缩到内容高度，勾回来时还原）
        self._expanded_height = 560
        self._build_ui()

    # ============================================================== UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        head = QLabel(f"源图层（{self._source.name()}图层，选中要素 {self._feature_count} 个）")
        head.setObjectName("SectionLabel")
        head.setWordWrap(True)
        layout.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("目标图层："))
        self.target_combo = QComboBox(self)
        for layer in self._target_layers:
            self.target_combo.addItem(layer.name(), layer.id())
        self.target_combo.setMinimumWidth(260)
        self.target_combo.currentIndexChanged.connect(lambda _i: self._rebuild_table())
        row.addWidget(self.target_combo)
        row.addStretch(1)
        layout.addLayout(row)

        attr_row = QHBoxLayout()
        self.copy_attributes = QCheckBox("复制要素的同时复制指定字段属性")
        self.copy_attributes.toggled.connect(self._on_copy_attributes_toggled)
        attr_row.addWidget(self.copy_attributes)
        attr_row.addStretch(1)
        self.auto_match_btn = QPushButton("自动匹配同名字段")
        self.auto_match_btn.clicked.connect(self._on_auto_match)
        attr_row.addWidget(self.auto_match_btn)
        layout.addLayout(attr_row)

        self.table = make_table(["", "目标字段", "源字段", "校验"], parent=self)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_TARGET, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_SOURCE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_ERROR, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(_COL_CHECK, 34)
        layout.addWidget(self.table, 1)

        # 「全选」放进表格**第一列的表头**（对齐 B/S 把复选框渲染在 headerCell 里）
        self.select_all = QCheckBox(header)
        self.select_all.setTristate(True)
        self.select_all.setToolTip("全选 / 取消全选（只作用于已选好源字段的行）")
        self.select_all.stateChanged.connect(self._on_select_all)
        header.sectionResized.connect(lambda *_a: self._position_select_all())
        header.geometriesChanged.connect(self._position_select_all)
        self.table.horizontalScrollBar().valueChanged.connect(
            lambda *_a: self._position_select_all()
        )

        self.status = QLabel("")
        self.status.setObjectName("PropHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)

        self.setLayout(layout)
        # 显式建一次表：target_combo 的信号是在 addItem **之后**才连的，所以初始那
        # 一轮 addItem 不会触发 currentIndexChanged，不手动调就一直是空表。
        self._rebuild_table()
        self._on_copy_attributes_toggled(self.copy_attributes.isChecked())

    def _position_select_all(self) -> None:
        """把「全选」摆到第一列表头正中（跟随列宽与横向滚动）。"""
        header = self.table.horizontalHeader()
        if header is None or not self.select_all.isVisible():
            return
        x = header.sectionViewportPosition(_COL_CHECK)
        x += (header.sectionSize(_COL_CHECK) - self.select_all.width()) // 2
        y = (header.height() - self.select_all.height()) // 2
        self.select_all.move(max(x, 0), max(y, 0))

    def showEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self.select_all.show()
        self._position_select_all()

    # ============================================================== 目标图层 / 表格

    def target_layer(self) -> QgsVectorLayer | None:
        """当前选中的目标图层。"""
        layer_id = self.target_combo.currentData()
        if layer_id is None:
            return None
        return next((lay for lay in self._target_layers if lay.id() == layer_id), None)

    def _rebuild_table(self, order: Optional[list[str]] = None,
                       selections: Optional[dict[str, str]] = None) -> None:
        """（重）建映射表：目标字段来自当前目标图层，可选按 ``order`` 排序。

        「自动匹配」会把匹配上的行排到前面，重排走**整表重建**而不是搬 widget——
        ``QTableWidget`` 挪 cellWidget 的所有权容易踩坑，表很小，重建更稳。
        """
        target = self.target_layer()
        self._combos.clear()
        self._rows.clear()
        self.table.setRowCount(0)
        if target is None:
            self._refresh_select_all_state()
            self._refresh_ok_state()
            return

        self._source_fields = mappable_source_fields(self._source)
        # 每个源字段的显示文本只格式化**一次**（原来在每个下拉的每一项上重复算，
        # 300 字段 × 180 行 = 6 万多次 f-string + 别名还原，是打开慢的主因之一）
        self._source_items = [
            (f"{field_alias(fld) or fld.name()}（{fld.name()}）", fld.name())
            for fld in self._source_fields
        ]
        target_fields = mappable_target_fields(target)
        self._target_field_by_name = {fld.name(): fld for fld in target_fields}
        names = [fld.name() for fld in target_fields]
        if order:
            known = [n for n in order if n in names]
            names = known + [n for n in names if n not in known]

        self.table.setRowCount(len(names))
        for row, name in enumerate(names):
            fld = self._target_field_by_name[name]

            check_item = QTableWidgetItem()
            check_item.setFlags(
                (check_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            check_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, _COL_CHECK, check_item)

            name_item = QTableWidgetItem(f"{field_alias(fld) or fld.name()}（{fld.name()}）")
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, _COL_TARGET, name_item)

            combo = _SourceFieldCombo(self.table)
            combo.set_items(self._source_items)
            combo.currentIndexChanged.connect(
                lambda _i, c=combo, n=name: self._on_source_changed(c, n)
            )
            # 可编辑下拉：手打过滤后不走 currentIndexChanged，textChanged 也要接
            combo.editTextChanged.connect(
                lambda _t, c=combo, n=name: self._on_source_changed(c, n)
            )
            self.table.setCellWidget(row, _COL_SOURCE, combo)

            err_item = QTableWidgetItem("")
            err_item.setFlags(err_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, _COL_ERROR, err_item)

            self._combos[name] = combo
            self._rows.append(name)
            if selections and name in selections:
                combo.select_source(
                    selections[name], self._label_of_source(selections[name])
                )
                self._set_row_checked(name, self._source_of(combo) is not None)
                self._validate_row(name)

        self._refresh_select_all_state()
        self._refresh_ok_state()

    # ============================================================== 取值 / 回填

    def _label_of_source(self, source_name: str) -> str:
        """源字段名 → 下拉里的显示文本（"别名（字段名）"）；找不到退化成字段名。"""
        for label, name in self._source_items:
            if name.lower() == str(source_name).lower():
                return label
        return str(source_name)

    @staticmethod
    def _source_of(combo: QComboBox) -> Optional[str]:
        """下拉当前代表的**源字段名**；没选 / 输入的不是字段返回 ``None``。

        可编辑下拉的 ``currentData()`` 只在"从弹出列表点选"时才可靠——用户手打的
        文字不会产生 index。所以统一按文本反查，并且**忽略大小写**。
        """
        text = combo.currentText().strip()
        if not text or text == _NONE_TEXT:
            return None
        idx = combo.findText(text)
        if idx >= 0:
            return combo.itemData(idx)
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data is not None and str(data).lower() == text.lower():
                return data
        return None

    def _on_source_changed(self, combo: QComboBox, target_name: str) -> None:
        """选了源字段 → 自动勾上该行并做类型校验（手打过滤也走这里）。"""
        self._set_row_checked(target_name, self._source_of(combo) is not None)
        self._validate_row(target_name)
        # 单行改动也要让「全选」复选框跟上（全勾/半勾/未勾），否则它会停在旧状态
        self._refresh_select_all_state()
        self._refresh_ok_state()

    def _row_of(self, target_name: str) -> int:
        try:
            return self._rows.index(target_name)
        except ValueError:
            return -1

    def _set_row_checked(self, target_name: str, checked: bool) -> None:
        row = self._row_of(target_name)
        if row < 0:
            return
        item = self.table.item(row, _COL_CHECK)
        if item is not None:
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _refresh_select_all_state(self) -> None:
        """让表头的「全选」跟上表格现状（全勾/半勾/未勾）。

        **按「所有行」算，不按「已映射的行」算** —— 否则自动匹配之后：匹配上的行
        都勾着、没匹配上的行没勾，按"已映射行"算就变成"全都勾了"→ 显示对勾，但
        肉眼明明看得见还有没勾的行（用户就是这么反馈的）。B/S 的 ``allSelected``
        也是 ``fieldMappings.every(m => m.selected)``，对全部行取与。

        表格被整表重建（换目标图层、自动匹配重排）之后，行的勾选态是我们代码改的，
        复选框不会自己知道——不同步的话它会停在旧状态，「再点一次」因为状态没变化
        连 ``stateChanged`` 都不发，看着就像按钮失灵。
        """
        total = len(self._rows)
        checked = sum(
            1 for name in self._rows
            if self.table.item(self._row_of(name), _COL_CHECK) is not None
            and self.table.item(self._row_of(name), _COL_CHECK).checkState()
            == Qt.CheckState.Checked
        )
        if total == 0 or checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        blocked = self.select_all.blockSignals(True)
        self.select_all.setCheckState(state)
        self.select_all.blockSignals(blocked)

    # ============================================================== 勾选

    def _on_copy_attributes_toggled(self, checked: bool) -> None:
        """不勾「复制字段属性」时整张映射表收起（对齐 B/S 的 v-if）。

        收起后窗口要**跟着缩到内容高度**：映射表是布局里唯一能撑开的项，它一隐藏，
        剩下的控件会被拉开摊满整个窗口，看起来"内容很散、间距很大"。缩高度之后
        内容自然往上靠。
        """
        self.table.setVisible(checked)
        self.select_all.setVisible(checked)
        self.auto_match_btn.setEnabled(checked)
        self.status.setText(
            "" if checked else "不勾选时只复制几何，目标字段按目标图层的自动规则填充。"
        )
        if checked:
            self.resize(self.width(), max(self._expanded_height, self.sizeHint().height()))
            self._position_select_all()
        else:
            self._expanded_height = max(self._expanded_height, self.height())
            self.resize(self.width(), self.sizeHint().height())
        self._refresh_ok_state()

    def _on_select_all(self, state: int) -> None:
        """全选：勾选 / 取消**所有行**（对齐 B/S 的 ``handleSelectAll``）。

        注意它勾的是"是否复制这一行"，**不负责替用户挑源字段**——没选源字段的行
        即使勾上也不会被复制（``field_mappings`` 只收"勾选且配了源字段"的行）。
        所以这里在状态栏把"有多少勾上但没配源字段"讲清楚，免得用户以为白勾了。
        """
        if not self.copy_attributes.isChecked():
            return
        checked = state != Qt.CheckState.Unchecked.value
        for name in self._rows:
            self._set_row_checked(name, checked)
        if not self._rows:
            self.status.setText("")
        else:
            missing = sum(
                1 for name in self._rows
                if self._combos.get(name) is not None
                and self._source_of(self._combos[name]) is None
            )
            text = f"已{'勾选' if checked else '取消勾选'} {len(self._rows)} 行"
            if checked and missing:
                text += f"（其中 {missing} 行未选源字段，不会被复制）"
            self.status.setText(text)
        self._refresh_ok_state()

    def _on_auto_match(self) -> None:
        """自动匹配同名字段：字段名相同（**忽略大小写**）+ 类型兼容，匹配的排前面。"""
        if not self.copy_attributes.isChecked():
            return
        # 忽略大小写：GDLX 与 gdlx 是同一个字段，不该因为大小写不同就配不上
        source_by_key = {}
        for fld in self._source_fields:
            key = fld.name().lower()
            if key in source_by_key:
                logger.warning("复制到：源图层有只差大小写的同名字段 %s，按先出现的算", fld.name())
                continue
            source_by_key[key] = fld
        matched: list[str] = []
        unmatched: list[str] = []
        selections: dict[str, str] = {}
        for name in self._rows:
            src = source_by_key.get(name.lower())
            tgt = self._target_field_by_name.get(name)
            if src is not None and tgt is not None and types_compatible(src, tgt):
                matched.append(name)
                selections[name] = src.name()
            else:
                unmatched.append(name)
        self._rebuild_table(order=matched + unmatched, selections=selections)
        self.status.setText(
            f"已自动匹配 {len(matched)} 个同名字段。" if matched else "未找到可匹配的字段。"
        )

    # ============================================================== 校验

    def _validate_row(self, target_name: str) -> None:
        """行内类型校验：不兼容时红字提示（对齐 B/S 的「字段类型不一致」）。"""
        row = self._row_of(target_name)
        combo = self._combos.get(target_name)
        if row < 0 or combo is None:
            return
        err_item = self.table.item(row, _COL_ERROR)
        if err_item is None:
            return
        source_name = self._source_of(combo)
        if source_name is None:
            err_item.setText("")
            return
        src = next(
            (f for f in self._source_fields if f.name().lower() == source_name.lower()), None
        )
        tgt = self._target_field_by_name.get(target_name)
        if src is None or tgt is None or types_compatible(src, tgt):
            err_item.setText("")
            return
        err_item.setText("字段类型不一致")

    def _row_errors(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for row, name in enumerate(self._rows):
            item = self.table.item(row, _COL_ERROR)
            if item is not None and item.text():
                out[name] = item.text()
        return out

    def _refresh_ok_state(self) -> None:
        """确定按钮：有字段映射错误 / 没有目标图层时禁用（对齐 B/S 的 okButtonProps）。"""
        blocked = bool(self._row_errors()) or self.target_layer() is None
        self.ok_button.setEnabled(not blocked)

    # ============================================================== 取值

    def copy_attributes_checked(self) -> bool:
        return self.copy_attributes.isChecked()

    def field_mappings(self) -> list[tuple[str, str]]:
        """``[(源字段名, 目标字段名), ...]`` —— 只含**勾选且配了源字段**的行。"""
        if not self.copy_attributes.isChecked():
            return []
        out: list[tuple[str, str]] = []
        for row, name in enumerate(self._rows):
            item = self.table.item(row, _COL_CHECK)
            combo = self._combos.get(name)
            if item is None or combo is None:
                continue
            if item.checkState() != Qt.CheckState.Checked:
                continue
            source_name = self._source_of(combo)
            if source_name is None:
                continue
            out.append((source_name, name))
        return out


__all__ = ["CopyToLayerDialog"]
