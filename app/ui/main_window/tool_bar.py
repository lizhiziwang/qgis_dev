"""工具条。

按 :class:`MainWindow` 注入的 callback 列表组装工具按钮。
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QMainWindow, QToolBar, QToolButton

from app.ui.common import tool_icons
from app.ui.common.theme import ThemeManager, Theme, toolbar_qss


class ToolBar:
    """主工具条构建器。

    两类状态按钮：

    - 地图工具（平移/缩放/选择/识别/卷帘）：单选高亮，跟随画布当前激活的
      :class:`QgsMapTool`（``MainWindow`` 监听 ``mapToolSet`` 后调用
      :meth:`set_active`）；
    - 会话型工具（地图对比）：独立开关，会话存活期间保持高亮
      （:meth:`set_session_active`），不受地图工具切换影响。

    一次性动作（全图/刷新/测试/质检/分析/取消选择）不参与高亮。
    """

    #: 地图工具 key → 按钮可勾选（单选组，跟随激活的地图工具）
    _TOOL_KEYS = frozenset({
        "pan",
        "select_point",
        "select_box",
        "identify",
        "zoom_in",
        "zoom_out",
        "swipe",
    })
    #: 会话型工具 key → 按钮独立开关（会话存活期常亮）
    _SESSION_KEYS = frozenset({"qmap_compare"})

    def __init__(self, main_window: QMainWindow) -> None:
        self.main_window = main_window
        self.toolbar: Optional[QToolBar] = None
        self.draw_manager = None  # 由 MainWindow 注入
        #: 已创建的按钮 + 图标 key，主题切换时用它重刷图标颜色
        self._icon_items: list[tuple[QAction | QToolButton, str]] = []
        #: 工具 key → 可勾选按钮（地图工具 + 会话工具）
        self._tool_actions: dict[str, QAction] = {}
        #: 当前高亮的工具 key（单选组）
        self._active_key: Optional[str] = None

    def build(self, callbacks: dict[str, Callable[[], None]]) -> QToolBar:
        """构建工具条。

        :param callbacks: 回调字典，包括
            - pan / zoom_in / zoom_out
            - identify
            - quality (质检 dialog)
            - analyse (分析)
            - swipe (卷帘对比)
            - refresh
            - test
        """
        tb = QMainWindow.addToolBar(self.main_window, "地图工具")
        # 图标优先展示，文字不出现在按钮上；悬浮时以 Tooltip 提示文本
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        tb.setIconSize(QSize(25, 25))
        self.toolbar = tb

        # 每个工具用 QPainter 画的现代化图标（随主题变色）
        color = ThemeManager.instance().current().toolbar_text
        for text, key in [
            ("平移", "pan"),
            ("点选", "select_point"),
            ("框选", "select_box"),
            ("取消选择", "clear_selection"),
            ("属性", "identify"),
            ("放大", "zoom_in"),
            ("缩小", "zoom_out"),
            ("质检", "quality"),
            ("分析", "analyse"),
            ("卷帘对比", "swipe"),
            ("地图对比", "qmap_compare"),
            ("全图", "zoom_full"),
            ("刷新", "refresh"),
            ("测试", "test"),
        ]:
            self._add_action(tb, text, key, callbacks, color)

        tb.setStyleSheet(toolbar_qss(ThemeManager.instance().current()))
        return tb

    # ------------------------------------------------------------ 状态高亮

    def set_active(self, key: Optional[str]) -> None:
        """地图工具单选：高亮 ``key`` 对应按钮，其余地图工具按钮取消高亮。

        会话型工具（地图对比）不受影响。
        """
        self._active_key = key
        for k, act in self._tool_actions.items():
            if k in self._SESSION_KEYS:
                continue
            act.setChecked(k == key)

    def set_session_active(self, key: str, active: bool) -> None:
        """会话型工具独立开关：会话存活期间按钮保持高亮。"""
        if key not in self._SESSION_KEYS:
            return
        act = self._tool_actions.get(key)
        if act is not None:
            act.setChecked(active)

    def active_key(self) -> Optional[str]:
        """当前高亮的地图工具 key（测试用）。"""
        return self._active_key

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
        if key in self._TOOL_KEYS or key in self._SESSION_KEYS:
            act.setCheckable(True)
            self._tool_actions[key] = act
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
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setText(label)
            btn.setToolTip(label)
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
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
