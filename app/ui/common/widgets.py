"""UI 通用 widgets 集合。"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QEvent, QRect, Qt
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
        if not (has_check and not opt.text):
            # **有文字**（或本来就没有勾选框）的格子：交回基类 —— 勾选框与文字都按
            # 默认画（勾选框在文字左边）。
            # 早先的实现无论有没有文字都把 HasCheckIndicator 剥掉，却只在"无文字"
            # 时补画居中那个 —— 于是「文字 + 勾选框」的格子勾选框整个不见了
            # （用户报「组织列打开后没看到勾选框」，那正是文字+勾选框的列表）。
            super().paint(painter, option, index)
            return
        # 无文字的勾选格：基类会把勾选框画在内容区最左边，这里改成居中自绘
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        self._draw_check(painter, opt.rect, index)

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802 —— Qt 命名
        """勾选格的点击判定：**整格都算命中**。

        基类用的是样式给的**左对齐**复选框矩形，而本委托把框画在了列中线 ——
        于是"看着点在框里、实际不算"（用户报：复选框命中偏左、往右一点就点不中）。
        这里不去复刻那个居中矩形（两处几何各算各的迟早再错位），直接把整个勾选格
        都当命中区：这类列本来就是"整格即复选框"的语义。
        """
        if not index.isValid():
            return False
        if not (index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return False
        self.initStyleOption(option, index)
        if not (option.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator):
            return False
        if option.text:
            # 有文字的格子：勾选框由基类按"文字左侧"画（见 paint 分支），命中几何与
            # 绘制一致，交回基类处理（点勾选框即切换）。这里若直接 return False，
            # 列表项（如「组织列」）就永远点不动——只能靠全选/全不选按钮。
            return super().editorEvent(event, model, option, index)
        if event.type() not in (
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if not option.rect.contains(event.pos()):
            return False
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        new_state = Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked
        return bool(model.setData(index, new_state, Qt.ItemDataRole.CheckStateRole))

    def _check_rect(self, rect) -> QRect:
        """自绘复选框在单元格里**居中**后占的矩形（绘制与命中判定共用一份）。"""
        size = self.BOX
        return QRect(
            rect.x() + (rect.width() - size) // 2,
            rect.y() + (rect.height() - size) // 2,
            size,
            size,
        )

    def _draw_check(self, painter, rect, index) -> None:
        """自绘勾选框（在 ``rect`` 里居中）。"""
        from qgis.PyQt.QtGui import QBrush, QPainter, QPen, QPixmap

        from app.ui.common.theme import ThemeManager, _check_url

        theme = ThemeManager.instance().current()
        box = self._check_rect(rect)
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
