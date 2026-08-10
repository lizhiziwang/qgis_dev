"""工具条。

按 :class:`MainWindow` 注入的 callback 列表组装工具按钮。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtWidgets import QAction, QMainWindow, QToolBar, QToolButton

from app.ui.common import tool_icons
from app.ui.common.theme import ThemeManager, Theme, toolbar_qss


class ToolBar:
    """主工具条构建器。"""

    def __init__(self, main_window: QMainWindow) -> None:
        self.main_window = main_window
        self.toolbar: Optional[QToolBar] = None
        self.draw_manager = None  # 由 MainWindow 注入
        #: 已创建的按钮 + 图标 key，主题切换时用它重刷图标颜色
        self._icon_items: list[tuple[QAction | QToolButton, str]] = []

    def build(self, callbacks: dict[str, Callable[[], None]]) -> QToolBar:
        """构建工具条。

        :param callbacks: 回调字典，包括
            - pan / zoom_in / zoom_out
            - identify
            - quality (质检 dialog)
            - analyse (分析)
            - refresh
            - test
        """
        tb = QMainWindow.addToolBar(self.main_window, "地图工具")
        # 图标优先展示，文字不出现在按钮上；悬浮时以 Tooltip 提示文本
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.setIconSize(QSize(25, 25))
        self.toolbar = tb

        # 每个工具用 QPainter 画的现代化图标（随主题变色）
        color = ThemeManager.instance().current().toolbar_text
        for text, key in [
            ("平移", "pan"),
            ("属性", "identify"),
            ("放大", "zoom_in"),
            ("缩小", "zoom_out"),
            ("质检", "quality"),
            ("分析", "analyse"),
            ("全图", "zoom_full"),
            ("刷新", "refresh"),
            ("测试", "test"),
        ]:
            self._add_action(tb, text, key, callbacks, color)

        tb.setStyleSheet(toolbar_qss(ThemeManager.instance().current()))
        return tb

    def _add_action(
        self,
        tb: QToolBar,
        text: str,
        key: str,
        callbacks: dict[str, Callable[[], None]],
        color: str,
    ) -> QAction:
        """建一个图标按钮：图标按 key 绘制，悬浮 Tooltip 显示文本。"""
        act = QAction(text, self.main_window)
        act.setIcon(tool_icons.icon(key, color))
        act.setToolTip(text)
        act.triggered.connect(callbacks.get(key, lambda: None))
        tb.addAction(act)
        self._icon_items.append((act, key))
        return act

    def add_space_query_buttons(self, callbacks: dict[str, Callable[[], None]]) -> None:
        """追加空间查询工具条（点 / 线 / 面 / 圆 / 框）。"""
        if self.toolbar is None:
            return
        color = ThemeManager.instance().current().toolbar_text
        for q_type, label, key in [
            ("point", "点查询", "space_query_point"),
            ("line", "线查询", "space_query_line"),
            ("polygon", "面查询", "space_query_polygon"),
        ]:
            btn = QToolButton()
            btn.setIcon(tool_icons.icon(key, color))
            btn.setIconSize(self.toolbar.iconSize())
            btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            btn.setText(label)
            btn.setToolTip(label)
            btn.setPopupMode(QToolButton.InstantPopup)
            btn.clicked.connect(callbacks.get(f"space_query_{q_type}", lambda: None))
            self.toolbar.addWidget(btn)
            self._icon_items.append((btn, key))

    def apply_theme(self, theme: Theme) -> None:
        """主题切换时刷新工具按钮图标颜色。"""
        if self.toolbar is None:
            return
        color = theme.toolbar_text
        for widget, key in self._icon_items:
            widget.setIcon(tool_icons.icon(key, color))
