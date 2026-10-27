"""「编辑设置」弹窗：两页 —— 常规 / 快捷键（页面参照原型 ``cs端/code01``）。

- **常规**：选择集筛选框（框选后是否弹「选择集」弹窗 + 弹在哪）、长度和面积
  （默认计算方法、是否自动算系统长度/面积字段）；
- **快捷键**：编辑工具栏下的工具快捷键（见
  :data:`app.infra.shortcuts.EDIT_TOOL_SHORTCUTS`）+ 底部「系统设置」组
  （默认长度/面积单位、滚轮向前滚动的方向）。原型就把这组放在这一页底部，
  这里照搬。

工程 / 打开 / 退出 / 日志等应用级快捷键**不在这页展示**，但它们仍然生效、也不受
本页保存影响（见 :meth:`EditSettingsDialog.sequences`）。

快捷键一行一个功能：左边功能名（只读），右边 :class:`QKeySequenceEdit` 直接按键录制。
底部三个按钮：

- **恢复默认** —— 把**本页**的项填回默认值（仍要点「保存」才落盘，避免误点即生效）；
- **取消** —— 丢弃本次改动；
- **保存** —— 校验（重复绑定 / 冲突）后写回 ``config/shortcuts.yaml`` 与
  ``config/edit_settings.yaml`` 并立即生效。

只做展示与收集，不碰 QAction / QShortcut / 画布——取值走
:meth:`EditSettingsDialog.sequences` 与 :meth:`EditSettingsDialog.settings`，
由主窗口套到实际控件上（见 ``MainWindow.apply_edit_settings_panel``）。
主题由全局 QSS 下发，本类不写 setStyleSheet。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QEvent, QRect, Qt
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.infra.edit_settings import (
    AREA_METHODS,
    AREA_UNITS,
    LENGTH_METHODS,
    LENGTH_UNITS,
    SELECT_FILTER_POSITIONS,
    WHEEL_MODES,
    EditSettings,
)
from app.infra.shortcuts import (
    EDIT_TOOL_SHORTCUTS,
    ShortcutSpec,
    default_map,
    find_conflicts,
)
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.message_box import MessageBox
from app.ui.common.widgets import make_table

#: 录制框最多收几个按键的组合（1 = 单组合键，与系统里既有快捷键一致）
_MAX_SEQUENCE_KEYS = 1

#: 常规页里标签列的宽度（对齐原型的 ``.ep-lb{width:132px}``）
_LABEL_WIDTH = 132

#: 「快捷键」页在 QTabWidget 里的下标（「恢复默认」按它区分当前页）
_TAB_KEYS = 1

#: item 的自定义角色：存该行的补充说明（由 :class:`_InfoItemDelegate` 取用）
_INFO_ROLE = Qt.ItemDataRole.UserRole + 1

#: ⓘ 图标绘制边长
_INFO_ICON = 14


class _InfoItemDelegate(QStyledItemDelegate):
    """功能列：在文字**右侧**补画一个 ⓘ，并接管悬停提示。

    为什么不加一列 / 不用 cellWidget：

    - 单独加一列 ⓘ —— 表里凭空多一条空列，看相很怪；
    - 把「文字 + ⓘ」做成 cellWidget —— item 自己的文字与控件会**叠着画**（用户
      看到"撤销"两个字重叠）；而把 item 文字清空又会在整表渲染时画不出控件里的
      QLabel（实测只剩图标）。

    所以改成同一个格子里自绘：基类先正常画文字，再按文字实际宽度把 ⓘ 摆在它右边；
    悬停提示走 :meth:`helpEvent`（视图在需要 tooltip 时会问委托）。
    """

    def paint(self, painter, option, index) -> None:  # noqa: N802 —— Qt 命名
        super().paint(painter, option, index)
        tip = index.data(_INFO_ROLE)
        if not tip:
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = option.widget.style() if option.widget is not None else QApplication.style()
        # 文字实际占的矩形（交给样式算，别自己猜边距），ⓘ 紧跟在它右边
        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, option.widget
        )
        x = text_rect.left() + opt.fontMetrics.horizontalAdvance(opt.text) + 6
        y = option.rect.center().y() - _INFO_ICON // 2
        icon = style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        icon.paint(painter, QRect(x, y, _INFO_ICON, _INFO_ICON))

    def helpEvent(self, event, view, option, index) -> bool:  # noqa: N802 —— Qt 命名
        tip = index.data(_INFO_ROLE)
        if tip and event.type() == QEvent.Type.ToolTip:
            QToolTip.showText(event.globalPos(), tip, view, option.rect)
            return True
        return super().helpEvent(event, view, option, index)


class EditSettingsDialog(BaseDialog):
    """编辑设置：常规 / 快捷键两页。"""

    def __init__(
        self,
        sequences: dict[str, str],
        settings: EditSettings,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, title="编辑设置")
        self.setMinimumSize(0, 0)
        self.resize(620, 620)
        self.setModal(True)
        #: 传进来的**全量**键序列（含本页不展示的工程 / 退出 / 日志等）。收尾时原样
        #: 带回去：本页只覆盖自己有编辑框的项，否则 ``save_overrides`` 会把用户
        #: 在别处设的覆盖值（比如改过的 Ctrl+S）一并冲掉。只留有**真实绑定**的项——
        #: 「鼠标 + 修饰键」那两个是纯展示、没有绑定，带回去只会污染落盘差分。
        self._all = {k: v for k, v in sequences.items() if k in default_map()}
        self._editors: dict[str, QKeySequenceEdit] = {}
        #: 常规页 / 系统设置组的控件：字段名 → 控件
        self._fields: dict[str, QWidget] = {}
        self._build_ui(sequences, settings)

    # ============================================================== UI

    def _build_ui(self, sequences: dict[str, str], settings: EditSettings) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_general_page(settings), "常规")
        self._tabs.addTab(self._build_keys_page(sequences, settings), "快捷键")
        layout.addWidget(self._tabs, 1)

        self.status = QLabel("")
        self.status.setObjectName("PropHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        reset = QPushButton("恢复默认")
        buttons.addButton(reset, QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._on_reset)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    # ---- 通用行构造：左标签（右对齐、定宽）+ 右控件，对齐原型 .ep-row ----

    @staticmethod
    def _row(label: str, control: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        lb = QLabel(label)
        lb.setFixedWidth(_LABEL_WIDTH)
        lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lb)
        row.addWidget(control)
        row.addStretch(1)
        return row

    def _combo(self, field: str, options: tuple[str, ...], current: str) -> QComboBox:
        box = QComboBox()
        box.addItems(list(options))
        if current in options:
            box.setCurrentText(current)
        self._fields[field] = box
        return box

    def _checkbox(self, field: str, text: str, checked: bool) -> QCheckBox:
        box = QCheckBox(text)
        box.setChecked(bool(checked))
        self._fields[field] = box
        return box

    def _build_general_page(self, settings: EditSettings) -> QWidget:
        """「常规」页：选择集筛选框 / 长度和面积。"""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(12)

        sel = QGroupBox("选择集筛选框")
        sel_box = QVBoxLayout(sel)
        sel_box.setSpacing(8)
        sel_box.addLayout(self._row(
            "是否开启：",
            self._checkbox("sel_filter_on", "", settings.sel_filter_on),
        ))
        sel_box.addLayout(self._row(
            "显示位置：",
            self._combo("sel_filter_pos", SELECT_FILTER_POSITIONS, settings.sel_filter_pos),
        ))
        hint = QLabel("开启后，框选结束会弹出「选择集」弹窗；关掉则只改选中集、不弹窗。")
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        sel_box.addWidget(hint)
        box.addWidget(sel)

        meas = QGroupBox("长度和面积")
        meas_box = QVBoxLayout(meas)
        meas_box.setSpacing(8)
        meas_box.addLayout(self._row(
            "默认长度计算方法：",
            self._combo("len_method", LENGTH_METHODS, settings.len_method),
        ))
        meas_box.addLayout(self._row(
            "默认面积计算方法：",
            self._combo("area_method", AREA_METHODS, settings.area_method),
        ))
        auto = self._checkbox(
            "auto_calc", "应用于系统长度与面积字段值的自动计算", settings.auto_calc
        )
        auto_row = QHBoxLayout()
        auto_row.addSpacing(_LABEL_WIDTH + 8)
        auto_row.addWidget(auto)
        auto_row.addStretch(1)
        meas_box.addLayout(auto_row)
        box.addWidget(meas)

        system = QGroupBox("系统设置")
        sys_box = QVBoxLayout(system)
        sys_box.setSpacing(8)
        sys_box.addLayout(self._row(
            "默认长度单位：", self._combo("len_unit", LENGTH_UNITS, settings.len_unit)
        ))
        sys_box.addLayout(self._row(
            "默认面积单位：", self._combo("area_unit", AREA_UNITS, settings.area_unit)
        ))
        sys_box.addLayout(self._row(
            "滚轮向前滚动：", self._combo("wheel", WHEEL_MODES, settings.wheel)
        ))
        box.addWidget(system)

        box.addStretch(1)
        return page

    def _build_keys_page(self, sequences: dict[str, str], settings: EditSettings) -> QWidget:
        """「快捷键」页：编辑工具栏下的工具快捷键表。"""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(8)

        head = QLabel(
            "这里只列编辑工具栏下的工具快捷键。点击右侧输入框后直接按下要用的"
            "组合键即可修改；点输入框里的清除按钮可取消绑定。改完点「保存」生效。"
        )
        head.setWordWrap(True)
        head.setObjectName("PropHint")
        box.addWidget(head)

        self.table = make_table(["功能", "快捷键"], parent=page)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setItemDelegateForColumn(0, _InfoItemDelegate(self.table))
        self.table.setRowCount(len(EDIT_TOOL_SHORTCUTS))
        for row, spec in enumerate(EDIT_TOOL_SHORTCUTS):
            self._fill_row(row, spec, sequences)
        box.addWidget(self.table, 1)
        return page

    def _fill_row(self, row: int, spec: ShortcutSpec, sequences: dict[str, str]) -> None:
        name_item = QTableWidgetItem(spec.label)
        name_item.setData(Qt.ItemDataRole.UserRole, spec.key)
        if spec.info:
            # 提示文字塞进 item 的自定义角色，由 _InfoItemDelegate 在文字右侧画 ⓘ
            # 并接管悬停 —— 既不用加一列，也不用 cellWidget（那两种前者难看、后者
            # 与 item 文字会叠着画）。
            name_item.setData(_INFO_ROLE, spec.info)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 0, name_item)
        if not spec.editable:
            # 鼠标修饰键：单独一个修饰键不是合法的 QKeySequence（Qt 要求至少一个
            # 真实按键），录不进去，这里只做展示
            text_item = QTableWidgetItem(spec.default)
            text_item.setFlags(text_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            text_item.setToolTip("鼠标 + 修饰键，固定行为，不可改")
            self.table.setItem(row, 1, text_item)
            return
        editor = self._make_editor(spec, sequences.get(spec.key, spec.default))
        self.table.setCellWidget(row, 1, editor)
        self._editors[spec.key] = editor

    def _make_editor(self, spec: ShortcutSpec, sequence: str) -> QKeySequenceEdit:
        editor = QKeySequenceEdit(self.table)
        editor.setMaximumSequenceLength(_MAX_SEQUENCE_KEYS)
        editor.setClearButtonEnabled(True)
        editor.setKeySequence(QKeySequence(sequence) if sequence else QKeySequence())
        editor.setToolTip(f"默认：{spec.default or '（未绑定）'}")
        editor.keySequenceChanged.connect(lambda *_a: self._refresh_status())
        return editor

    # ============================================================== 取值

    def sequences(self) -> dict[str, str]:
        """**全量**键序列 ``{key: 键序列字符串}``（空串 = 不绑定）。

        本页有编辑框的项取控件当前值；其余项原样带回——这样
        ``diff_from_defaults`` + ``save_overrides`` 不会把本页看不到的覆盖值冲掉。
        """
        out = dict(self._all)
        for key, editor in self._editors.items():
            out[key] = editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        return out

    def settings(self) -> EditSettings:
        """常规页 / 系统设置组当前值。"""
        def text(field: str, fallback: str) -> str:
            w = self._fields.get(field)
            return w.currentText() if isinstance(w, QComboBox) else fallback

        def flag(field: str, fallback: bool) -> bool:
            w = self._fields.get(field)
            return w.isChecked() if isinstance(w, QCheckBox) else fallback

        d = EditSettings()
        return EditSettings(
            sel_filter_on=flag("sel_filter_on", d.sel_filter_on),
            sel_filter_pos=text("sel_filter_pos", d.sel_filter_pos),
            len_method=text("len_method", d.len_method),
            area_method=text("area_method", d.area_method),
            auto_calc=flag("auto_calc", d.auto_calc),
            len_unit=text("len_unit", d.len_unit),
            area_unit=text("area_unit", d.area_unit),
            wheel=text("wheel", d.wheel),
        )

    def current_conflicts(self) -> list[tuple[str, str, str]]:
        return find_conflicts(self.sequences())

    def _refresh_status(self) -> None:
        conflicts = self.current_conflicts()
        if conflicts:
            seq, a, b = conflicts[0]
            extra = f"（共 {len(conflicts)} 处）" if len(conflicts) > 1 else ""
            self.status.setText(f"「{seq}」被「{a}」与「{b}」同时占用，请改成不同的组合{extra}")
        else:
            self.status.setText("")

    def _on_reset(self) -> None:
        """恢复默认：**只恢复当前这一页**的项（仍要点「保存」才落盘）。

        常规页 → 那页的设置项；快捷键页 → 那页列出的键位。用户在哪一页点的
        「恢复默认」，就只期望那一页被重置，不该顺手把另一页也清了。
        """
        if self._tabs.currentIndex() == _TAB_KEYS:
            self._reset_keys()
            text = "已填回默认快捷键，点「保存」后生效。"
        else:
            self._reset_settings()
            text = "已填回默认设置，点「保存」后生效。"
        self._refresh_status()
        self.status.setText(text)

    def _reset_keys(self) -> None:
        for spec in EDIT_TOOL_SHORTCUTS:
            editor = self._editors.get(spec.key)
            if editor is not None:
                editor.setKeySequence(
                    QKeySequence(spec.default) if spec.default else QKeySequence()
                )

    def _reset_settings(self) -> None:
        for field, value in self._defaults().items():
            widget = self._fields.get(field)
            if isinstance(widget, QComboBox):
                widget.setCurrentText(value)  # type: ignore[arg-type]
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))

    @staticmethod
    def _defaults() -> dict:
        d = EditSettings()
        return {
            "sel_filter_on": d.sel_filter_on,
            "sel_filter_pos": d.sel_filter_pos,
            "len_method": d.len_method,
            "area_method": d.area_method,
            "auto_calc": d.auto_calc,
            "len_unit": d.len_unit,
            "area_unit": d.area_unit,
            "wheel": d.wheel,
        }

    def _on_ok(self) -> None:
        conflicts = self.current_conflicts()
        if conflicts:
            seq, a, b = conflicts[0]
            MessageBox.warning(
                self, "快捷键重复", f"「{seq}」被「{a}」与「{b}」同时占用，请改成不同的组合。"
            )
            return
        self.accept()


__all__ = ["EditSettingsDialog"]
