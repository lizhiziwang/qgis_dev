"""图层属性各页共用的自绘小控件。

目前只有颜色按钮：「标注」页的文本/描边颜色与「渲染」页的填充/描边/线/点颜色
共用同一颗，避免两处各画一份。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QColorDialog, QPushButton


class ColorButton(QPushButton):
    """自绘颜色选择按钮（跟随本应用主题，不用 QgsColorButton 的原生 chrome）。

    颜色变化发 :attr:`color_changed`：``clicked`` 在用户选完色**之前**就发了，
    拿它当变化通知会读到旧颜色，所以需要刷预览的页面接这个信号。
    """

    #: 颜色被改变（程序设置也算）
    color_changed = pyqtSignal(QColor)

    def __init__(self, color=None, parent=None) -> None:
        super().__init__(parent)
        self._color = QColor(color) if color is not None else QColor(0, 0, 0)
        self.setObjectName("PropColorBtn")
        self.setFixedSize(34, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击选择颜色")
        self.clicked.connect(self._pick)
        self._sync()

    def color(self) -> QColor:
        return QColor(self._color)

    def set_color(self, color, emit: bool = True) -> None:
        if color is None:
            return
        c = QColor(color)
        if not c.isValid():
            return
        self._color = c
        self._sync()
        if emit:
            self.color_changed.emit(QColor(c))

    def open_picker(self) -> None:
        """打开取色框（给"点预览图也能取色"这类外部入口用，等价于点自己）。"""
        self._pick()

    def _sync(self) -> None:
        # 内联样式只作用于这一颗按钮：色块背景 + 中性描边。
        # 背景用 rgba，带透明度的颜色能直接看出来（不再只是纯色块）
        c = self._color
        bg = f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"
        self.setStyleSheet(
            "QPushButton#PropColorBtn {"
            f" background-color: {bg};"
            " border: 1px solid palette(mid); border-radius: 3px; }"
            "QPushButton#PropColorBtn:hover { border-color: palette(highlight); }"
        )
        alpha = round(c.alpha() / 255 * 100)
        self.setToolTip(f"点击选择颜色（透明度 {alpha}%）")

    def _pick(self) -> None:
        # 回到早期的 Qt 取色窗（基本颜色 / 自定义颜色 / HSV/RGB/HTML），
        # 额外打开透明度通道；强制非原生对话框，否则 Windows 原生取色框
        # 会忽略 ShowAlphaChannel（看不到透明度）。
        options = (
            QColorDialog.ColorDialogOption.ShowAlphaChannel
            | QColorDialog.ColorDialogOption.DontUseNativeDialog
        )
        chosen = QColorDialog.getColor(self._color, self, "选择颜色", options)
        if chosen.isValid():
            self.set_color(chosen)
