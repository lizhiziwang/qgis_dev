"""UI 通用 widgets 集合。"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui.common.icons import Icons


def open_file_dir(
    parent,
    is_dir: bool = False,
    title: str = "选择路径",
    init_path: str = "",
    filter_: str = "所有文件 (*)",
) -> str:
    """打开文件/目录选择对话框。"""
    if is_dir:
        return QFileDialog.getExistingDirectory(
            parent, title, init_path, QFileDialog.Option.ShowDirsOnly
        )
    file_dia = QFileDialog(parent, title, init_path, filter_)
    file_dia.setModal(True)
    file_dia.setFileMode(QFileDialog.FileMode.ExistingFile)
    if file_dia.exec() == QFileDialog.DialogCode.Accepted:
        files = file_dia.selectedFiles()
        return files[0] if files else ""
    return ""


def save_file(
    parent,
    title: str = "保存文件",
    init_path: str = "",
    filter_: str = "所有文件 (*)",
) -> str:
    """打开文件保存对话框，返回路径（取消返回空串）。"""
    path, _ = QFileDialog.getSaveFileName(parent, title, init_path, filter_)
    return path


def make_table(
    headers: list[str],
    *,
    select_rows: bool = True,
    stretch_last: bool = True,
    parent: Optional[QWidget] = None,
) -> QTableWidget:
    """构造常用 :class:`QTableWidget`。"""
    table = QTableWidget(parent)
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    if select_rows:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    if stretch_last:
        table.horizontalHeader().setStretchLastSection(True)
    return table


def make_refresh_button(parent: Optional[QWidget] = None, tooltip: str = "刷新") -> QPushButton:
    """构造"刷新"图标按钮（透明扁平样式，hover/按下走 app_qss 的 FlatButton 规则）。"""
    btn = QPushButton(parent)
    btn.setObjectName("FlatButton")
    btn.setIcon(Icons.refresh())
    btn.setToolTip(tooltip)
    btn.setFixedSize(26, 26)
    return btn


__all__ = [
    "open_file_dir",
    "save_file",
    "make_table",
    "make_refresh_button",
]
