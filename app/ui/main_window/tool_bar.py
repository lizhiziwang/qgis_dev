"""工具条。

按 :class:`MainWindow` 注入的 callback 列表组装工具按钮。
工具条用 :class:`app.ui.common.ribbon.RibbonToolBar`：按钮成组，组间竖线分隔，
组名以小字画在每组按钮下方（对标设计原型 ribbon 工具条）。
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QMainWindow, QToolBar, QToolButton

from app.ui.common import tool_icons
from app.ui.common.ribbon import RibbonToolBar
from app.ui.common.theme import ThemeManager, Theme, toolbar_qss

#: 工具图标逻辑边长（与 ``setIconSize`` 一致，SVG 按此尺寸原生渲染避免二次缩放）
_ICON = 20

#: 工具分组：(组名, [(按钮文案, key), ...])，组名画在每组按钮下方
_TOOL_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "视图",
        [
            ("平移", "pan"),
            ("放大", "zoom_in"),
            ("缩小", "zoom_out"),
            ("全图", "zoom_full"),
            ("刷新", "refresh"),
        ],
    ),
    (
        "选择",
        [
            ("点选", "select_point"),
            ("框选", "select_box"),
            ("取消选择", "clear_selection"),
        ],
    ),
    ("查询", [("属性", "identify")]),
    # 质检按钮已按用户要求从「通用」页签隐藏（质检入口保留在「质检」页签）
    ("分析", [("分析", "analyse")]),
    ("对比", [("卷帘对比", "swipe"), ("地图对比", "qmap_compare")]),
    (
        "数据",
        [
            ("在线底图", "online_basemap"),
            ("字典管理", "dict_manage"),
            ("创建数据", "create_data"),
            ("数据管理", "data_manage"),
            ("离线下载", "offline_download"),
        ],
    ),
]


class ToolBar:
    """主工具条构建器。

    两类状态按钮：

    - 地图工具（平移/缩放/选择/识别/卷帘）：单选高亮，跟随画布当前激活的
      :class:`QgsMapTool`（``MainWindow`` 监听 ``mapToolSet`` 后调用
      :meth:`set_active`）；
    - 会话型工具（地图对比）：独立开关，会话存活期间保持高亮
      （:meth:`set_session_active`），不受地图工具切换影响。

    一次性动作（全图/刷新/质检/分析/取消选择）不参与高亮（曾经的「测试」按钮已
    按用户要求从工具条移除——见 :data:`_TOOL_GROUPS`）。
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
            - data_manage (数据管理 Dock)
            - dict_manage (字典管理 dialog)
            - refresh
            - test
        """
        tb = RibbonToolBar("地图工具", self.main_window)
        self.main_window.addToolBar(tb)
        tb.setObjectName("MapToolBar")
        # Ribbon 风格：图标在上 + 小字标签在下（对标设计原型工具栏）
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(_ICON, _ICON))
        self.toolbar = tb

        # 每个工具用 SVG/字体图标画的现代化图标（随主题强调色，对标原型蓝色线性图标）
        # 同类型工具成组，组间加竖分隔线；组名稍后 set_groups() 交给工具条绘制
        theme = ThemeManager.instance().current()
        color = theme.input_focus
        groups: list[tuple[str, list[QAction]]] = []
        for idx, (group_name, items) in enumerate(_TOOL_GROUPS):
            acts = [self._add_action(tb, text, key, callbacks, color) for text, key in items]
            groups.append((group_name, acts))
            if idx != len(_TOOL_GROUPS) - 1:
                tb.addSeparator()
        tb.set_groups(groups, theme.text_secondary)

        tb.setStyleSheet(toolbar_qss(theme))
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
        act.setIcon(tool_icons.icon(key, color, _ICON))
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
        color = ThemeManager.instance().current().input_focus
        for q_type, label, key in [
            ("point", "点查询", "space_query_point"),
            ("line", "线查询", "space_query_line"),
            ("polygon", "面查询", "space_query_polygon"),
        ]:
            btn = QToolButton()
            btn.setIcon(tool_icons.icon(key, color, _ICON))
            btn.setIconSize(self.toolbar.iconSize())
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setText(label)
            btn.setToolTip(label)
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            btn.clicked.connect(callbacks.get(f"space_query_{q_type}", lambda: None))
            self.toolbar.addWidget(btn)
            self._icon_items.append((btn, key))

    def apply_theme(self, theme: Theme) -> None:
        """主题切换时刷新工具按钮图标颜色与组名颜色。"""
        if self.toolbar is None:
            return
        color = theme.input_focus
        for widget, key in self._icon_items:
            widget.setIcon(tool_icons.icon(key, color, _ICON))
        if isinstance(self.toolbar, RibbonToolBar):
            self.toolbar.set_group_color(theme.text_secondary)
