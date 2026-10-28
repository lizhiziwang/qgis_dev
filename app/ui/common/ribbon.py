"""Ribbon 风格工具条基类：按钮行下方绘制「组名」。

对标设计原型（``D:\\work\\code01``）的顶部工具条：同类型按钮成组，组间用竖线分隔，
**组名以 :data:`~app.ui.common.theme.RIBBON_FONT_PX` 灰色小字居中显示在每组按钮下方**。
Qt 的 :class:`QToolBar` 布局只会
横向排列按钮、不会在下方留白，因此在子类里：

- 用 ``set_groups()`` 记录 ``[(组名, [该组的 QAction, ...]), ...]``；
- 重写 :meth:`sizeHint` 把按钮行高度加上 ``2 * GROUP_BAND``，让按钮下方空出一条带子
  （QSS ``padding-bottom`` 对 QToolBar 布局无效，见 :func:`app.ui.common.theme.toolbar_qss`）；
- 在 :meth:`paintEvent` 末尾按每个 QAction 的按钮几何算出组的水平范围，把组名
  画在按钮下方。

这样不改变工具条的 action 结构（``actions()`` 仍是全部工具 QAction），
现有按 ``text()`` 查 action 的代码与测试都不受影响。

另外提供 :class:`AlignedToolBar`：修正 QToolBar 收缩时自带的扩展按钮（三个点）
与普通按钮的垂直对齐问题（Ribbon 加高后扩展按钮仍贴顶，见该类 docstring）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, QRect, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QAction, QColor, QPainter
from qgis.PyQt.QtWidgets import QToolBar, QToolButton

from app.ui.common.theme import RIBBON_FONT_PX, TOOLBAR_ITEM_GAP

#: 按钮行下方留给组名的高度（px）
GROUP_BAND = 15

#: 工具条**底边**额外留白（px）：给组名文字下方留呼吸空间。
#: Qt 把 QToolBar 内容垂直居中，QSS ``padding-bottom`` 对布局无效（只会均分到上下），
#: 因此用 ``setContentsMargins(0, 0, 0, BOTTOM_PAD)`` 只在底边加高：内容位置不变，
#: 工具条只向下长高。仅含组名的 Ribbon 工具条（通用 / 编辑）需要。
BOTTOM_PAD = 5

#: 所有 ribbon 按钮统一宽度（对标原型 ``.core-tool-group button { width: 62px }``）。
#: 按钮宽度交给代码固定，而非 QSS：QSS ``min-width`` 会与文字宽度**相加**，
#: 结果是文字越长按钮越宽（2 字 49px / 4 字 69px），无法统一。
#: 字号是 :data:`~app.ui.common.theme.RIBBON_FONT_PX` = 11px，最长的 4 字按钮
#: （取消选择 / 在线底图 …）文字约 44px，扣掉边框 + 内边距后需 ≥ 48px 才不裁字。
BUTTON_WIDTH = 54


class AlignedToolBar(QToolBar):
    """扩展按钮（三个点）与普通工具按钮垂直对齐的 :class:`QToolBar`。

    QToolBar 收缩放不下时会出现自带的扩展按钮（``qt_toolbar_ext_button``）。它由
    ``QToolBarLayout`` 直接贴顶摆放，而普通按钮在内容区里垂直居中；Ribbon 工具条
    为了给组名留白又额外加高了 ``2 * GROUP_BAND``，于是扩展按钮会比普通按钮高出
    一截（实测 15px），看着像没对齐。

    ``QToolBarLayout`` 会在 resize / LayoutRequest 之后重新摆扩展按钮的几何，所以
    这里不能在 ``resizeEvent`` 里立即改（会被随后的一次布局覆盖），而是排一个
    0ms 定时器，等布局跑完再按第一个可见 action 按钮的 ``y`` / 高度对齐。

    参照物必须是普通工具按钮（``QToolButton``），而不是 ``addWidget`` 放进去的
    自定义控件：编辑工具条最左侧是「当前编辑图层」下拉框，它比图标+文字的按钮矮、
    在内容区里垂直居中，拿它当基准会把扩展按钮对到按钮行偏下的位置（用户报过
    「三个点位置太靠下」）。没有按钮时才退回用任意可见控件。
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, parent)
        # 定时器挂在工具条自己身上：工具条销毁时 Qt 会一并销毁，不会留下
        # 已删对象的回调（静态 QTimer.singleShot 绑定方法做不到这一点）。
        self._ext_align_timer = QTimer(self)
        self._ext_align_timer.setSingleShot(True)
        self._ext_align_timer.setInterval(0)
        self._ext_align_timer.timeout.connect(self._align_extension_button)

    def event(self, event) -> bool:
        handled = super().event(event)
        if event.type() in (
            QEvent.Type.LayoutRequest,
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self._schedule_extension_alignment()
        return handled

    def _schedule_extension_alignment(self) -> None:
        if not self._ext_align_timer.isActive():
            self._ext_align_timer.start()

    def _align_extension_button(self) -> None:
        if self.orientation() != Qt.Orientation.Horizontal:
            return
        ext = self.findChild(QToolButton, "qt_toolbar_ext_button")
        if ext is None or not ext.isVisible():
            return
        reference = None
        fallback = None
        for action in self.actions():
            if action.isSeparator():
                continue
            widget = self.widgetForAction(action)
            if widget is None or not widget.isVisible():
                continue
            if isinstance(widget, QToolButton):
                reference = widget
                break
            if fallback is None:
                fallback = widget
        if reference is None:
            reference = fallback
        if reference is None:
            return
        geometry = ext.geometry()
        if geometry.y() == reference.y() and geometry.height() == reference.height():
            return
        ext.setGeometry(geometry.x(), reference.y(), geometry.width(), reference.height())


class RibbonToolBar(AlignedToolBar):
    """带「组名」的 Ribbon 工具条，见模块 docstring。"""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, parent)
        self._groups: list[tuple[str, list[QAction]]] = []
        self._group_color = QColor("#66727c")
        # 让 toolbar_qss 能按 [ribbon="true"] 选择器放大本工具条的按钮
        self.setProperty("ribbon", True)
        # 底边额外留白：QSS padding-bottom 对 QToolBar 布局无效，用 contentsMargins
        # 只在底边加高（内容垂直居中，位置不变）
        self.setContentsMargins(0, 0, 0, BOTTOM_PAD)
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
        """把所有**按钮**固定成同一宽度，避免文字长短导致按钮宽窄不一。

        只处理 ``QToolButton``：工具条里也可能用 ``addWidget`` 放下拉框等自定义
        控件（如「当前编辑图层」），它们有自己的宽度，不能被压成 54px。
        """
        for act in self.actions():
            if act.isSeparator():
                continue
            widget = self.widgetForAction(act)
            if isinstance(widget, QToolButton):
                widget.setFixedWidth(BUTTON_WIDTH)

    def showEvent(self, event) -> None:
        # 工具条可能在 set_groups 之后才被 show（tab 切换），补一次宽度
        self._apply_button_widths()
        super().showEvent(event)

    def set_group_color(self, color: str) -> None:
        """主题切换时刷新组名颜色。"""
        self._group_color = QColor(color)
        self.update()

    def set_group_name(self, actions: list[QAction], name: str) -> None:
        """把某一组（按 ``set_groups`` 里同一动作列表对象识别）的组名换成 ``name``。

        用于组名跟随状态变化的场景（如「当前编辑图层（编辑中）」）。动作列表
        必须是 ``set_groups`` 时传入的**同一个 list 对象**，避免靠动作相等性猜。
        """
        for index, (_old_name, group_actions) in enumerate(self._groups):
            if group_actions is not actions:
                continue
            if _old_name != name:
                self._groups[index] = (name, group_actions)
                self.update()
            return

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._groups:
            return
        # 组名共用同一条基线：组里可能放比按钮矮的自定义控件（如下拉框），
        # 若按各组自身底边画，下拉框那组的组名会比其他组高出一截。统一取
        # 本行最低的可见控件底边，保证「当前编辑图层」和「编辑 / 几何 / 分割」
        # 在同一水平线上。
        baseline = None
        for act in self.actions():
            if act.isSeparator():
                continue
            widget = self.widgetForAction(act)
            if widget is not None and widget.isVisible():
                bottom = widget.geometry().bottom()
                baseline = bottom if baseline is None else max(baseline, bottom)
        if baseline is None:
            return
        painter = QPainter(self)
        painter.setPen(self._group_color)
        font = painter.font()
        font.setPixelSize(RIBBON_FONT_PX)
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
            # 组名紧贴在按钮行下方（下拉框本身在行内垂直居中，不影响基线）
            band = QRect(left, baseline + 2, right - left + 1, GROUP_BAND)
            painter.drawText(
                band,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                name,
            )
        painter.end()


__all__ = [
    "BOTTOM_PAD",
    "BUTTON_WIDTH",
    "GROUP_BAND",
    "AlignedToolBar",
    "RibbonToolBar",
]
