"""「快捷键」面板：列出系统全部全局快捷键，可就地编辑、保存、恢复默认。

一行一个功能：左边功能名（只读），右边 :class:`QKeySequenceEdit` 直接按键录制。
底部三个按钮：

- **恢复默认** —— 把表格里所有项填回默认值（仍要点「保存」才落盘，避免误点即生效）；
- **取消** —— 丢弃本次改动；
- **保存** —— 校验（重复绑定 / 冲突）后写回 ``config/shortcuts.yaml`` 并立即生效。

只做展示与收集，不碰 QAction / QShortcut——取值走 :meth:`ShortcutsDialog.sequences`，
由主窗口套到实际控件上（见 ``MainWindow.apply_shortcut_panel``）。
主题由全局 QSS 下发，本类不写 setStyleSheet。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.infra.shortcuts import DEFAULT_SHORTCUTS, EDITABLE_SHORTCUTS, ShortcutSpec, find_conflicts
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.widgets import make_table

#: 录制框最多收几个按键的组合（1 = 单组合键，与系统里既有快捷键一致）
_MAX_SEQUENCE_KEYS = 1


class ShortcutsDialog(BaseDialog):
    """编辑全系统全局快捷键。"""

    def __init__(self, sequences: dict[str, str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, title="快捷键")
        self.setMinimumSize(0, 0)
        self.resize(560, 520)
        self.setModal(True)
        self._editors: dict[str, QKeySequenceEdit] = {}
        self._build_ui(sequences)

    # ============================================================== UI

    def _build_ui(self, sequences: dict[str, str]) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        head = QLabel(
            "点击右侧输入框后直接按下要用的组合键即可修改；点输入框里的清除按钮"
            "可取消绑定。改完点「保存」生效。"
        )
        head.setWordWrap(True)
        head.setObjectName("PropHint")
        layout.addWidget(head)

        self.table = make_table(["功能", "快捷键"], parent=self)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setRowCount(len(DEFAULT_SHORTCUTS))
        for row, spec in enumerate(DEFAULT_SHORTCUTS):
            name_item = QTableWidgetItem(spec.label)
            name_item.setData(Qt.ItemDataRole.UserRole, spec.key)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name_item)
            if not spec.editable:
                # 鼠标修饰键：单独一个修饰键不是合法的 QKeySequence（Qt 要求至少一个
                # 真实按键），录不进去，这里只做展示
                text_item = QTableWidgetItem(spec.default)
                text_item.setFlags(text_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                text_item.setToolTip("鼠标 + 修饰键，固定行为，不可改")
                self.table.setItem(row, 1, text_item)
                continue
            editor = self._make_editor(spec, sequences.get(spec.key, spec.default))
            self.table.setCellWidget(row, 1, editor)
            self._editors[spec.key] = editor
        layout.addWidget(self.table, 1)

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
        """表格当前值 ``{key: 键序列字符串}``（空串 = 不绑定）。"""
        out: dict[str, str] = {}
        for key, editor in self._editors.items():
            out[key] = editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        return out

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
        """恢复默认：只填回表格，仍要点「保存」才落盘。"""
        for spec in EDITABLE_SHORTCUTS:
            editor = self._editors.get(spec.key)
            if editor is None:
                continue
            editor.setKeySequence(QKeySequence(spec.default) if spec.default else QKeySequence())
        self._refresh_status()
        self.status.setText("已填回默认快捷键，点「保存」后生效。")

    def _on_ok(self) -> None:
        conflicts = self.current_conflicts()
        if conflicts:
            seq, a, b = conflicts[0]
            QMessageBox.warning(
                self, "快捷键重复", f"「{seq}」被「{a}」与「{b}」同时占用，请改成不同的组合。"
            )
            return
        self.accept()


__all__ = ["ShortcutsDialog"]
