"""UI 通用 widgets 集合。"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QRect, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,

    QStyledItemDelegate,
    QStyleOptionViewItem,
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


class CenteredCheckDelegate(QStyledItemDelegate):
    """勾选框格的绘制：**去掉单元格焦点框**，并把勾选框画在列中线。

    **焦点框**：主题的 QSS 里没有 ``::item:focus`` 规则，Qt 就按原生样式在「当前
    单元格」上画一圈方框 —— 点一下勾选框旁边的空白就冒出一圈黑边，很难看。这里把
    ``State_HasFocus`` 从样式选项里摘掉，别的绘制照旧。

    **居中**：Qt 默认把勾选框画在内容区最左边。勾选框所在的那一列照例没有文字，
    所以这里自己算一个居中矩形重画；**有文字的行仍按默认左对齐**——勾选框跟文字
    挤在一起会互相看不清。

    **为什么是自绘、而不是 ``drawPrimitive(PE_IndicatorCheckBox)``**：主题开着 QSS 时
    那个 primitive 走 ``QStyleSheetStyle``，它照 ``::indicator`` 规则自己算位置、
    **忽略调用方给的 rect**（实测：把 rect 往中间挪，实际绘制只动了不到 2px，等于挪不动）。
    所以这里照主题的同款画法自己画：圆角方框 + 复用主题生成的对勾图片，位置完全可控。

    用法：给带勾选框的表 / 树装一次即可::

        table.setItemDelegate(CenteredCheckDelegate(table))

    ``QStyledItemDelegate.paint`` 内部会**重新**按 index 推导样式选项，所以清掉
    ``HasCheckIndicator`` 之后必须自己调 ``drawControl``，不能回调 ``super().paint``
    （那样勾选框会被重新加回来，等于白清）。
    """

    #: 自绘勾选框的边长（与主题 QSS 里 ``::indicator`` 的 14px 一致）
    BOX = 14

    def initStyleOption(self, option, index) -> None:  # noqa: N802 —— Qt 命名
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus

    def paint(self, painter, option, index) -> None:  # noqa: N802 —— Qt 命名
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        has_check = bool(
            opt.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        )
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()

        # 让基类别画勾选框（清掉特性后**必须**自己 drawControl：调 super().paint 会
        # 重新按 index 推导选项，把勾选框又加回来）
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        if has_check and not opt.text:
            # 只有「没文字的勾选格」才居中重画；有文字的保持基类的默认左对齐
            self._draw_check(painter, opt.rect, index)

    def _draw_check(self, painter, rect, index) -> None:
        """自绘勾选框（在 ``rect`` 里居中）。"""
        from qgis.PyQt.QtGui import QBrush, QPainter, QPen, QPixmap

        from app.ui.common.theme import ThemeManager, _check_url

        theme = ThemeManager.instance().current()
        size = self.BOX
        box = QRect(
            rect.x() + (rect.width() - size) // 2,
            rect.y() + (rect.height() - size) // 2,
            size,
            size,
        )
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(theme.input_focus if checked else theme.input_border)))
        painter.setBrush(QBrush(QColor(theme.input_bg)))
        painter.drawRoundedRect(box, 2, 2)
        if checked:
            tick = QPixmap(_check_url(theme.input_focus))
            if not tick.isNull():
                painter.drawPixmap(box, tick)   # 缩放着画进小方框
        painter.restore()


def centered_check_delegate(view):
    """给 ``view`` 装上 :class:`CenteredCheckDelegate` 并把它返回（链式用）。"""
    delegate = CenteredCheckDelegate(view)
    view.setItemDelegate(delegate)
    return delegate


__all__ = [
    "open_file_dir",
    "save_file",
    "make_table",
    "make_refresh_button",
    "CenteredCheckDelegate",
    "centered_check_delegate",
]
