"""Ribbon 风格工具条基类：按钮行下方绘制「组名」。

对标设计原型（``D:\\work\\code01``）的顶部工具条：同类型按钮成组，组间用竖线分隔，
**组名以 10px 灰色小字居中显示在每组按钮下方**。Qt 的 :class:`QToolBar` 布局只会
横向排列按钮、不会在下方留白，因此在子类里：

- 用 ``set_groups()`` 记录 ``[(组名, [该组的 QAction, ...]), ...]``；
- 重写 :meth:`sizeHint` 把按钮行高度加上 ``2 * GROUP_BAND``，让按钮下方空出一条带子
  （QSS ``padding-bottom`` 对 QToolBar 布局无效，见 :func:`app.ui.common.theme.toolbar_qss`）；
- 在 :meth:`paintEvent` 末尾按每个 QAction 的按钮几何算出组的水平范围，把组名
  画在按钮下方。

这样不改变工具条的 action 结构（``actions()`` 仍是全部工具 QAction），
现有按 ``text()`` 查 action 的代码与测试都不受影响。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QRect, QSize, Qt
from qgis.PyQt.QtGui import QAction, QColor, QPainter
from qgis.PyQt.QtWidgets import QToolBar

from app.ui.common.theme import TOOLBAR_ITEM_GAP

#: 按钮行下方留给组名的高度（px）
GROUP_BAND = 15

#: 所有 ribbon 按钮统一宽度（对标原型 ``.core-tool-group button { width: 62px }``）。
#: 按钮宽度交给代码固定，而非 QSS：QSS ``min-width`` 会与文字宽度**相加**，
#: 结果是文字越长按钮越宽（2 字 49px / 4 字 69px），无法统一。
BUTTON_WIDTH = 51


class RibbonToolBar(QToolBar):
    """带「组名」的 Ribbon 工具条，见模块 docstring。"""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, parent)
        self._groups: list[tuple[str, list[QAction]]] = []
        self._group_color = QColor("#66727c")
        # 让 toolbar_qss 能按 [ribbon="true"] 选择器放大本工具条的按钮
        self.setProperty("ribbon", True)
        # 项间距走 QLayout（QSS ``spacing`` 在首次布局早于样式 polish 时会失效，
        # 表现为按钮彼此贴死；这里程序化兜底，见 theme.TOOLBAR_ITEM_GAP）
        layout = self.layout()
        if layout is not None:
            layout.setSpacing(TOOLBAR_ITEM_GAP)

    # ------------------------------------------------------------ 高度留白

    def sizeHint(self) -> QSize:
        """在按钮行高度之上再加一条组名留白带。

        QSS ``padding-bottom`` 对 QToolBar 布局无效（Qt 把按钮行**垂直居中**），
        所以本类在 ``super().sizeHint()`` 上再加 ``2 * GROUP_BAND``：多出来的
        高度被上下均分，按钮下方自然空出 ≥ :data:`GROUP_BAND` 的带子给组名。
        """
        base = super().sizeHint()
        return QSize(base.width(), base.height() + 2 * GROUP_BAND)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt 命名)
        base = super().minimumSizeHint()
        return QSize(base.width(), base.height() + 2 * GROUP_BAND)

    def set_groups(
        self, groups: list[tuple[str, list[QAction]]], color: str
    ) -> None:
        """记录分组并刷新：``groups`` = ``[(组名, [QAction, ...]), ...]``。"""
        self._groups = list(groups)
        self._group_color = QColor(color)
        self._apply_button_widths()
        self.update()

    def _apply_button_widths(self) -> None:
        """把所有按钮固定成同一宽度，避免文字长短导致按钮宽窄不一。"""
        for act in self.actions():
            if act.isSeparator():
                continue
            widget = self.widgetForAction(act)
            if widget is not None:
                widget.setFixedWidth(BUTTON_WIDTH)

    def showEvent(self, event) -> None:
        # 工具条可能在 set_groups 之后才被 show（tab 切换），补一次宽度
        self._apply_button_widths()
        super().showEvent(event)

    def set_group_color(self, color: str) -> None:
        """主题切换时刷新组名颜色。"""
        self._group_color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._groups:
            return
        painter = QPainter(self)
        painter.setPen(self._group_color)
        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        for name, actions in self._groups:
            rects = []
            for act in actions:
                widget = self.widgetForAction(act)
                if widget is not None and widget.isVisible():
                    rects.append(widget.geometry())
            if not rects:
                continue
            left = min(r.left() for r in rects)
            right = max(r.right() for r in rects)
            # 组名紧贴在按钮下方（按钮由 QToolBar 垂直居中，故按实际几何取底边）
            band = QRect(left, max(r.bottom() for r in rects) + 2, right - left + 1, GROUP_BAND)
            painter.drawText(
                band,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                name,
            )
        painter.end()


__all__ = ["BUTTON_WIDTH", "GROUP_BAND", "RibbonToolBar"]
