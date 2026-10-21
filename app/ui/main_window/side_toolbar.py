"""画布左侧竖排工具条。

位置：悬浮盖在地图画布**左上角**（left 4px / top 3px，由
``MainWindow._attach_side_toolbar_overlay`` 把本工具条 ``setParent`` 到画布的
**父容器**（中央控件）并摆到「画布左上角 + (4, 3)」——与画布同级，坐标相对父
容器，画布缩放时由 ``MainWindow._place_side_toolbar`` 重算；不占中央布局，
也不挤占画布空间）。

层级：因为与画布**同级**（而非画布子控件），工具条 ``raise_()`` 后永远盖在
画布**及其所有子覆盖层**（悬浮消息条 / 卷帘 / 对比分隔条……）之上；由
``MainWindow._keep_side_toolbar_on_top``（接 ``mapCanvasRefreshed`` /
``extentsChanged``）持续重新置顶。

背景：容器是 :class:`app.ui.common.frosted.FrostedPanel`——把工具条**背后**
的地图做高斯模糊当底图，再叠一层 ``主题底色 @ 60%`` 的半透明，配圆角细边框，
得到跟随主题的毛玻璃效果（浅色主题浅灰、深色主题深灰；底图算法与刷新时机见
:mod:`app.ui.common.frosted`）。

工具清单（自上而下，与设计稿一致）::

    选择 / 框选 / 漫游 / 缩放 / 全图 / 清除
    属性 / 条件查询 / 空间查询 / 选择屏蔽 / 取消屏蔽 / 分屏 / 定位
    线测量 / 面测量 / 范围裁剪

行为约定：

- 项目**已实现**的工具直接接主窗口既有回调（与顶部工具条同一入口）：
  选择 / 框选 / 漫游 / 缩放 / 全图 / 清除 / 条件查询 / 空间查询 / 分屏 / 定位 /
  线测量 / 面测量 / 选择屏蔽 / 取消屏蔽 / 范围裁剪；
- 「属性」与顶部「查询」组的「属性」是同一个入口（``identify``），内部调用
  :class:`~app.ui.tools.i_query.IQueryTool`：点要素后弹出 ArcMap 风格的
  「I查询」浮动面板，按图层列出全部命中要素；两边按钮共用 ``identify`` key，
  单选高亮由 ``MainWindow._on_map_tool_set`` 统一同步；
- 「缩放」用 :class:`~app.ui.tools.pan_zoom.ZoomClickTool`：**左键单击放大、
  按住 Shift 单击缩小**（以点击点为缩放中心），与顶部工具条上独立的「放大 /
  缩小」按钮（拉框缩放）不是同一个入口；
- 「分屏」复用 :mod:`app.ui.tools.qmap_compare` 的「地图对比」入口
  （``MainWindow._on_qmap_compare``：镜像分屏 / 垂直分割 / 水平分割 /
  放大镜），与顶部工具条「地图对比」同一实现，不再用旧的卷帘覆盖层；
- 「定位」点击后弹出 :class:`~app.ui.main_window.coord_locate_overlay.CoordLocateOverlay`
  气泡（浮在地图**右上角**），由主窗口
  :meth:`~app.ui.main_window.mixins.shell.WindowShellMixin._on_coord_locate`
  接管：再点同按钮或点气泡上的 ``×`` 都能关闭，按钮随气泡显隐独立常亮
  （会话型，与分屏同高亮组 :data:`_SESSION_KEYS`）；
- 「线测量」「面测量」接 :mod:`app.ui.tools.measure`：激活后连续左键落点，
  实时在画布底部读数条显示折线长度 / 多边形面积（右键结束、``Esc`` 清除），
  属地图工具（单选高亮，见 :data:`_TOOL_KEYS`）；
- 其余工具为占位：点击时经消息条提示「该工具正在开发中」（气泡悬浮在
  地图上，不挤压画布）；
- 地图工具（选择 / 框选 / 漫游 / 缩放 / 线测量 / 面测量）为单选高亮：跟随画布
  激活的 :class:`QgsMapTool`，由 ``MainWindow._on_map_tool_set`` 调
  :meth:`set_active` 与顶部工具条同步；会话型工具「分屏」独立常亮
  （:meth:`set_session_active`）；其余按钮为一次性动作，不高亮。

高度不足时自动收起：本工具条贴着画布左上角向下排，窗口太矮 / 画布被压扁时
底部按钮会超出画布可视区域被界面盖住。主窗口每次摆放侧栏都会把可用高度传给
:meth:`fit_to_height`，放不下的按钮隐藏，底部留一个向下小三角（``caret_down``）
的「更多」按钮；点它弹出**单列**面板列出被收起的工具，按钮沿用侧栏样式
（图标在上、文字在下），点击与点原按钮等价（检查态 / 图标 / 提示都沿用原按钮）；
面板优先显示在小三角下方，下方空间不足时贴着小三角上沿向上展开。

图标用 Font Awesome（见 :mod:`app.ui.common.tool_icons`），随主题变色
（:meth:`apply_theme`）。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import Qgis
from qgis.PyQt.QtCore import QPoint, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QFrame,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from app.ui.common import tool_icons
from app.ui.common.frosted import FrostedPanel
from app.ui.common.theme import TOOLBAR_BUTTON_RADIUS, Theme, ThemeManager

#: 地图工具 key（单选高亮组）。多数与顶部工具条同 key 便于同步；「缩放」用独立
#: 的 ``side_zoom``，避免与顶部「放大」联动高亮。
_TOOL_KEYS = frozenset({
    "select_point",
    "select_box",
    "pan",
    "side_zoom",
    "identify",
    "measure_line",
    "measure_area",
})
#: 图标 key 别名：按钮的**高亮 key** 与顶部工具条区分（``side_zoom`` 独立高亮），
#: 但图标复用 ``zoom_in`` 的那张，外观不变。
_ICON_ALIAS = {"side_zoom": "zoom_in", "spatial_query": "space_query_polygon"}
#: 按钮提示文案覆盖（默认用按钮文字）；「缩放」说明单击放大 / Shift 单击缩小。
_TOOLTIPS = {"side_zoom": "点击地图放大，按住shift键点击地图缩小"}
#: 会话型工具 key（独立常亮，不受地图工具切换影响；与顶部工具条同 key）。
#: 「定位」也归这里：点开气泡 → 按钮保持高亮表示"气泡在显示"，
#: 关闭气泡（再点同按钮或气泡上的 ×）→ 按钮弹起，与分屏会话同一套高亮逻辑。
#: 「范围裁剪」同理：裁剪生效期间按钮保持高亮，再点一次即取消并弹起。
_SESSION_KEYS = frozenset({"qmap_compare", "coord_locate", "extent_clip"})
#: 侧栏图标逻辑边长（原型左侧工具列 44×36 按钮 / 17px 图标）
_ICON = 17
#: 「更多」按钮高度：只放一个向下小三角，比普通按钮矮，像工具条的折叠条
_MORE_HEIGHT = 22
#: 「更多」菜单的向下小三角（Font Awesome caret-down）
_MORE_ICON = "caret_down"


class SideToolBar:
    """画布左侧竖排工具条构建器。

    :param main_window: 主窗口（取 messageBar 提示 / 主题）
    :param callbacks: 工具 key → 回调；**未提供的 key 即占位工具**，点击提示
        「该工具正在开发中」
    """

    def __init__(self, main_window, callbacks: dict[str, Callable[[], None]]) -> None:
        self.main_window = main_window
        self.widget: QWidget | None = None
        #: 工具 key → 按钮（高亮 / 主题刷新用）
        self._actions: dict[str, QToolButton] = {}
        self._icon_items: list[tuple[QToolButton, str]] = []
        #: 按视觉顺序排列的按钮 / 分隔线（高度不足时按前缀收起）
        self._entries: list[QWidget] = []
        #: 底部「更多」三角按钮（只在有按钮放不下时显示）
        self._more_btn: QToolButton | None = None
        #: 上次 fit_to_height 的 (可用高度, 控件是否可见)；避免画布重绘时反复重排
        self._fit_key: tuple[int, bool] | None = None
        self.build(callbacks)

    # ------------------------------------------------------------ 装配

    def build(self, callbacks: dict[str, Callable[[], None]]) -> QWidget:
        """构建竖排工具条控件。

        :param callbacks: 已实现工具的回调；缺 key 的工具按占位处理
        """
        # 毛玻璃容器：背后地图高斯模糊 + 主题底色 60% 半透明（跟随主题）
        canvas = getattr(self.main_window, "canvas", None)
        widget = FrostedPanel(canvas)
        widget.setObjectName("SideToolBar")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)

        color = ThemeManager.instance().current().input_focus
        self._entries = []
        self._fit_key = None
        # (文本, 图标 key, 分组后是否加分隔线)；callbacks 缺 key → 占位工具
        for text, key, separator_after in [
            ("选择", "select_point", False),
            ("框选", "select_box", False),
            ("漫游", "pan", False),
            ("缩放", "side_zoom", False),
            ("全图", "zoom_full", False),
            ("前视图", "view_prev", False),
            ("后视图", "view_next", False),
            ("清除", "clear_selection", True),
            ("属性", "identify", False),
            ("条件查询", "condition_query", False),
            ("空间查询", "spatial_query", False),
            ("选择屏蔽", "select_mask", False),
            ("取消屏蔽", "unmask", False),
            ("分屏", "qmap_compare", False),
            ("定位", "coord_locate", True),
            ("线测量", "measure_line", False),
            ("面测量", "measure_area", False),
            ("范围裁剪", "extent_clip", False),
        ]:
            # 固定统一宽度 + 水平居中：文字短的按钮不再整体靠左
            btn = self._make_button(text, key, callbacks, color)
            btn.setFixedWidth(45)
            layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
            self._entries.append(btn)
            if separator_after:
                separator = self._separator()
                layout.addWidget(separator)
                self._entries.append(separator)

        more = self._make_more_button(color)
        layout.addWidget(more, 0, Qt.AlignmentFlag.AlignHCenter)
        more.hide()
        self._more_btn = more

        layout.addStretch(1)
        # 窄条 + 两行文案会顶高按钮：压掉 app_qss 给 QToolButton 的 3px 上下内边距
        # （每个按钮省 6px；挂在本面板上只作用于 15 个侧栏按钮，不影响其它按钮）
        # 圆角跟主/编辑工具条保持一致
        widget.setStyleSheet(f"QToolButton {{ padding: 0px; border-radius: {TOOLBAR_BUTTON_RADIUS}px; }}")
        widget.setFixedWidth(52)
        widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.widget = widget
        return widget

    def _make_button(
        self,
        text: str,
        key: str,
        callbacks: dict[str, Callable[[], None]],
        color: str,
    ) -> QToolButton:
        """建一个竖排按钮：图标在上文字在下；占位工具点击提示开发中。"""
        btn = QToolButton()
        btn.setObjectName(f"SideTool_{key}")
        # 文案排版：2~3 字单行（≤ 33px，45px 按钮放得下）；4 字折两行（44px 会顶到边）。
        # 注意 len <= 3 的文案**不能**加 "\n"：尾随换行会多出一个空行，白白高一行
        # （实测 40px → 52px，15 个按钮共 +180px）。
        btn.setText(text if len(text) <= 3 else f"{text[:2]}\n{text[2:]}")

        font = QFont()
        font.setPointSize(8)  # 这里改字号，pt单位；或者 setPixelSize(10) 像素单位
        btn.setFont(font)

        btn.setToolTip(_TOOLTIPS.get(key, text))
        icon_key = _ICON_ALIAS.get(key, key)
        btn.setIcon(tool_icons.icon(icon_key, color, _ICON))
        btn.setIconSize(QSize(_ICON, _ICON))
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        btn.setAutoRaise(True)
        callback = callbacks.get(key)
        if key in _TOOL_KEYS or key in _SESSION_KEYS:
            # 地图工具：单选高亮；会话型工具（分屏）：独立常亮
            btn.setCheckable(True)
            self._actions[key] = btn
        btn.clicked.connect(
            callback if callback is not None else (lambda checked=False, t=text: self._not_implemented(t))
        )
        self._icon_items.append((btn, icon_key))
        return btn

    def _make_more_button(self, color: str) -> QToolButton:
        """建底部「更多」按钮：只有一个小三角，点开列出被收起的工具。"""
        btn = QToolButton()
        btn.setObjectName("SideTool_More")
        btn.setToolTip("更多工具")
        btn.setIcon(tool_icons.icon(_MORE_ICON, color, _ICON))
        btn.setIconSize(QSize(_ICON, _ICON))
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        btn.setAutoRaise(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedWidth(45)
        btn.setFixedHeight(_MORE_HEIGHT)
        btn.clicked.connect(self._show_more_menu)
        self._icon_items.append((btn, _MORE_ICON))
        return btn

    # ------------------------------------------------------------ 高度自适应

    def fit_to_height(self, available_height: int) -> None:
        """按可用高度收起放不下的按钮，底部留一个「更多」小三角。

        本工具条从画布左上角向下排列，画布被压矮时底部按钮会被界面遮挡。
        这里只保留放得进 ``available_height`` 的前缀，其余隐藏；有隐藏项时
        显示「更多」按钮，点击弹出菜单列出被收起的工具。

        :param available_height: 工具条可用的竖直空间（逻辑像素）
        """
        widget = self.widget
        more = self._more_btn
        if widget is None or more is None:
            return
        available = max(0, int(available_height))
        # 画布重绘 / 范围变化会高频触发摆放；高度没变且已显示过就不必重排。
        # 可见状态也纳入 key：首次在 show() 前算出的尺寸在真正显示后再校准一次。
        key = (available, widget.isVisible())
        if key == self._fit_key:
            return
        self._fit_key = key

        layout = widget.layout()
        if layout is None:
            return
        margins = layout.contentsMargins()
        spacing = layout.spacing()
        heights = [entry.sizeHint().height() for entry in self._entries]
        top = margins.top()
        bottom = margins.bottom()
        more_height = more.sizeHint().height()

        def content_height(count: int) -> int:
            """前 ``count`` 项的高度（含项间距）。"""
            if count <= 0:
                return 0
            return sum(heights[:count]) + spacing * (count - 1)

        natural = top + bottom + content_height(len(heights))
        if natural <= available:
            for entry in self._entries:
                entry.show()
            more.hide()
            widget.setFixedHeight(natural)
            return

        # 给「更多」按钮和它与最后一个可见按钮的间距留出空间。
        budget = available - top - bottom - more_height - spacing
        visible_count = 0
        for index in range(len(heights)):
            if content_height(index + 1) > budget:
                break
            visible_count = index + 1
        # 收起后末尾不能留下一条孤零零的分隔线。
        while visible_count > 0 and isinstance(self._entries[visible_count - 1], QFrame):
            visible_count -= 1
        for index, entry in enumerate(self._entries):
            entry.setVisible(index < visible_count)
        more.show()
        height = top + bottom + content_height(visible_count) + more_height
        if visible_count > 0:
            height += spacing
        widget.setFixedHeight(height)

    def _show_more_menu(self) -> None:
        """弹出「更多」面板：用与侧栏一致的按钮列出被收起的工具。"""
        widget = self.widget
        more = self._more_btn
        if widget is None or more is None:
            return
        hidden = [entry for entry in self._entries if not entry.isVisible()]
        if not any(isinstance(entry, QToolButton) for entry in hidden):
            return
        menu = QMenu(widget)
        menu.setObjectName("SideToolBarMoreMenu")
        # QMenu 只当弹出容器用：去掉它自己的底 / 边框，让里面的 FrostedPanel 透出
        # 背后地图（与侧栏同一套毛玻璃底）；否则会盖上一层不透明的菜单底色。
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        menu.setWindowFlags(
            menu.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setStyleSheet(
            "QMenu { background: transparent; border: none; padding: 0px; }"
            "QMenu::item { padding: 0px; background: transparent; }"
        )
        canvas = getattr(self.main_window, "canvas", None)
        panel = FrostedPanel(canvas, parent=menu)
        panel.setObjectName("SideToolBarMorePanel")
        panel.setFixedWidth(52)
        # 与侧栏按钮同一套内边距 / 圆角；配色与悬停态继续走全局 QSS。
        panel.setStyleSheet(f"QToolButton {{ padding: 0px; border-radius: {TOOLBAR_BUTTON_RADIUS}px; }}")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)

        added = False
        separator_pending = False
        for entry in hidden:
            if isinstance(entry, QFrame):
                # 分组线只在两个被收起按钮之间保留，避免面板首尾出现空分隔。
                separator_pending = added
                continue
            if not isinstance(entry, QToolButton):
                continue
            if separator_pending:
                layout.addWidget(self._separator())
                separator_pending = False
            layout.addWidget(self._make_more_panel_button(entry, menu), 0, Qt.AlignmentFlag.AlignHCenter)
            added = True

        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        menu.ensurePolished()
        menu.adjustSize()
        menu.exec(self._more_panel_position(menu, more))
        # 每次点击都新建菜单，用完交给事件循环回收（挂在本控件上不会自己消失）。
        menu.deleteLater()

    @staticmethod
    def _more_panel_position(menu: QMenu, more: QToolButton) -> QPoint:
        """「更多」面板位置：优先显示在小三角下方，下方空间不足时翻到上方。"""
        origin = more.mapToGlobal(QPoint(0, 0))
        size = menu.sizeHint()
        screen = more.screen()
        if screen is not None:
            available = screen.availableGeometry()
            left, top = available.left(), available.top()
            right, bottom = left + available.width(), top + available.height()
        else:
            left = top = 0
            right = bottom = 1 << 30

        x = origin.x() + more.width() + 2
        panel_bottom = origin.y() + more.height()
        if bottom - panel_bottom >= size.height() + 2:
            y = panel_bottom + 2
        else:
            # 下方放不下：面板底边贴着小三角上沿向上展开。
            y = origin.y() - 2 - size.height()
        x = max(left, min(x, right - size.width()))
        y = max(top, min(y, bottom - size.height()))
        return QPoint(x, y)

    def _make_more_panel_button(self, source: QToolButton, menu: QMenu) -> QToolButton:
        """按侧栏按钮的样式复制一个「更多」面板项，点击后执行原按钮的功能。"""
        button = QToolButton()
        button.setObjectName(f"SideMore_{source.objectName()}")
        button.setText(source.text())
        button.setFont(source.font())
        button.setToolTip(source.toolTip())
        button.setIcon(source.icon())
        button.setIconSize(source.iconSize())
        button.setToolButtonStyle(source.toolButtonStyle())
        button.setAutoRaise(True)
        button.setFixedWidth(45)
        if source.isCheckable():
            button.setCheckable(True)
            button.setChecked(source.isChecked())
        # 先收起面板，再触发原按钮：原回调可能弹模态对话框，避免在菜单事件里重入。
        button.clicked.connect(
            lambda _checked=False, original=source, popup=menu: self._activate_more_panel_item(original, popup)
        )
        return button

    @staticmethod
    def _activate_more_panel_item(source: QToolButton, menu: QMenu) -> None:
        """「更多」面板项点击：关闭面板后执行原按钮的动作。"""
        menu.close()
        QTimer.singleShot(0, source.click)

    @staticmethod
    def _separator() -> QFrame:
        """按钮分组分隔线。"""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ------------------------------------------------------------ 状态

    def set_active(self, key: str | None) -> None:
        """地图工具单选高亮：仅 ``key`` 对应按钮勾选，其余弹起。

        会话型工具（分屏）不受影响，由 :meth:`set_session_active` 独立控制。
        """
        for k, btn in self._actions.items():
            if k in _SESSION_KEYS:
                continue
            btn.setChecked(k == key)

    def set_session_active(self, key: str, active: bool) -> None:
        """会话型工具独立开关：会话存活期间按钮保持高亮（分屏）。"""
        if key not in _SESSION_KEYS:
            return
        btn = self._actions.get(key)
        if btn is not None:
            btn.setChecked(active)

    def _not_implemented(self, text: str) -> None:
        """占位工具点击：气泡提示开发中（悬浮在地图上，不挤压画布）。"""
        bar = getattr(self.main_window, "messageBar", None)
        if bar is None:
            return
        bar.pushMessage(
            f"「{text}」工具正在开发中，敬请期待",
            level=Qgis.MessageLevel.Info,
            duration=2,
        )

    # ------------------------------------------------------------ 主题

    def apply_theme(self, theme: Theme) -> None:
        """主题切换时刷新按钮图标颜色。"""
        if self.widget is None:
            return
        color = theme.input_focus
        for btn, key in self._icon_items:
            btn.setIcon(tool_icons.icon(key, color, _ICON))
