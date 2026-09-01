"""自定义标题栏（方案 A：无边框窗口 + 自绘标题栏）。

布局：应用图标 | 窗口标题 …… | 用户信息(头像+用户名) | 主题切换 | 最小化 | 最大化/还原 | 关闭

拖动 / 缩放不在本组件内实现：由 :class:`FramelessMixin.nativeEvent` 拦截
``WM_NCHITTEST``，标题栏空白区返回 ``HTCAPTION``（系统负责拖动/贴边/双击最大化），
窗口边缘返回对应 resize 码（系统负责缩放）。本组件只负责：

- 展示：应用图标、窗口标题、用户信息（头像 + 用户名）
- 三个窗口按钮 → 对应信号
- 主题切换按钮 → 弹出主题菜单，切换后重绘自身配色
- 非 Windows 平台的鼠标拖动兜底（:meth:`startSystemMove` 原生拖动）

配色不写死：颜色来自 :mod:`app.ui.common.theme`，随 :class:`ThemeManager`
切换自动重绘。
"""

from __future__ import annotations

import hashlib
import time

from qgis.PyQt.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QWidget,
)

from app.ui.common.icons import Icons
from app.ui.common.qt_compat import global_pos
from app.ui.common.theme import THEMES, ThemeManager, title_bar_qss

TITLE_BAR_HEIGHT = 34

_AVATAR_COLORS = (
    "#4A90E2",
    "#5B8DEF",
    "#7D6FF0",
    "#E2725B",
    "#3AA78F",
    "#C974A8",
)


def make_avatar_pixmap(name: str, size: int = 22) -> QPixmap:
    """用用户名生成"圆形 + 首字母"占位头像（没有真实头像资源时的兜底）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(_avatar_color(name)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    letter = (name or "?").strip()[0].upper()
    font = QFont("Microsoft YaHei")
    font.setBold(True)
    font.setPixelSize(int(size * 0.55))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return pm


def _avatar_color(name: str) -> str:
    if not name:
        return _AVATAR_COLORS[0]
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return _AVATAR_COLORS[int(digest, 16) % len(_AVATAR_COLORS)]


def _window_icon(kind: str, color: QColor = QColor("#5a5f66")) -> QIcon:
    """用 QPainter 绘制最小化/最大化/还原/关闭图标，避免依赖字体字形。"""
    size = 11
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(1.1)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "min":
        painter.drawLine(1, size // 2, size - 2, size // 2)
    elif kind == "max":
        painter.drawRect(1, 2, size - 3, size - 4)
    elif kind == "restore":
        painter.drawRect(2, 1, size - 4, size - 4)
        painter.drawRect(1, 3, size - 4, size - 4)
    elif kind == "close":
        painter.drawLine(2, 2, size - 3, size - 3)
        painter.drawLine(size - 3, 2, 2, size - 3)
    painter.end()
    return QIcon(pm)


def _theme_icon() -> QIcon:
    """调色盘图标：四色圆点（主题切换按钮）。"""
    size = 12
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colors = ("#4A90E2", "#3AA78F", "#E2725B", "#C974A8")
    radius = 2
    for i, color in enumerate(colors):
        cx = 2 + (i % 2) * 4
        cy = 2 + (i // 2) * 4
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(cx, cy, radius * 2, radius * 2)
    painter.end()
    return QIcon(pm)


#: 任务模式 / 工程模式 — ``TitleBar.mode_switch`` 持有的状态 key。
#: 暴露成模块级常量便于 ``MainWindow`` / 其它模块做相等性比较。
MODE_TASK = "task"
MODE_PROJECT = "project"
_MODE_OPTIONS: list[tuple[str, str]] = [
    # (key, 显示文本) —— 左→右排列
    (MODE_TASK, "任务模式"),
    (MODE_PROJECT, "工程模式"),
]


def _mode_switch_qss(theme) -> str:
    """生成 ``_ModeSwitch`` 在当前主题下的样式表（胶囊形分段控件）。

    视觉：
    - 整体外框：与 ``menu_bg`` 同色的圆角矩形，作为「轨道」
    - 未选中：轨道色（``menu_bg``）底 + ``menu_text`` 普通文字。**不能用
      transparent**：蓝/绿主题的标题栏是主题主色，透明会让标题栏颜色从按钮后
      透出，选中段和未选中段看起来同色，看不出选中态。
    - 选中项：强调色（``input_focus``）底 + 白字。但蓝/绿主题
      ``input_focus == title_bg``（标题栏即主题主色），选中段与标题栏同色会
      看不出选中态，此时改用深色 ``menu_text`` 打底，白字照常 → 选中段始终
      与标题栏形成对比。
    - hover：``menu_hover`` 微调
    """
    sel_bg = theme.input_focus
    if sel_bg == theme.title_bg:
        sel_bg = theme.menu_text
    return (
        # 容器：圆角胶囊，底色 = 菜单背景，营造「轨道」感
        f"QWidget#ModeSwitchContainer {{"
        f"  background-color: {theme.menu_bg};"
        f"  border: 1px solid {theme.border};"
        f"  border-radius: {TITLE_BAR_HEIGHT // 2}px;"
        f"}}"
        # 按钮：未选中与轨道同色（避免标题栏从后透出），文字用菜单文字色
        f"QPushButton#ModeSwitchBtn {{"
        f"  background-color: {theme.menu_bg};"
        f"  color: {theme.menu_text};"
        f"  border: none;"
        f"  border-radius: {TITLE_BAR_HEIGHT // 2}px;"
        f"  padding: 0 12px;"
        f"  font-size: 12px;"
        f"}}"
        # hover（仅未选中态）
        f"QPushButton#ModeSwitchBtn:hover {{"
        f"  background-color: {theme.menu_hover};"
        f"}}"
        # 选中态：强调色（必要时回退 menu_text）背景 + 白字
        f"QPushButton#ModeSwitchBtn:checked {{"
        f"  background-color: {sel_bg};"
        f"  color: white;"
        f"}}"
        f"QPushButton#ModeSwitchBtn:checked:hover {{"
        f"  background-color: {sel_bg};"
        f"  color: white;"
        f"}}"
    )


class _ModeSwitch(QWidget):
    """任务模式 / 工程模式 二选一切换开关（胶囊形分段控件）。

    设计：
    - 顶部 chrome 的视觉密度低、宽 34px，所以这里把两个按钮合并到同一个圆角
      容器里，左「任务模式」右「工程模式」，选中态走强调色底 + 白字（用
      ``input_focus`` 主题色，4 个主题都已定义）
    - 状态管理：内部 ``_mode``（str），外部通过 :attr:`mode` 只读属性读取
    - 事件：用户点击切换 → emit :attr:`mode_changed`
    - 默认值：``MODE_PROJECT``（GIS 工程模式更常用）
    """

    mode_changed = pyqtSignal(str)  # 新模式 key（MODE_TASK / MODE_PROJECT）

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setObjectName("ModeSwitchContainer")
        self.setFixedHeight(TITLE_BAR_HEIGHT - 8)  # 上下各留 4px 边距
        # 普通 QWidget 的 QSS background / border 必须开 WA_StyledBackground 才
        # 渲染。否则「轨道」容器是透明的，蓝/绿主题（input_focus == title_bg）
        # 下选中段会直接坐在同色标题栏上 → 隐形（看不出哪个模式被选中）。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._mode = MODE_PROJECT  # 默认工程模式
        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        for key, label in _MODE_OPTIONS:
            btn = QPushButton(label, self)
            btn.setObjectName("ModeSwitchBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedHeight(self.height())
            btn.setMinimumWidth(64)
            # 在 group 中按顺序加 → buttonId 0 / 1，用于反向查 key
            self._group.addButton(btn, len(self._buttons))
            self._buttons[key] = btn
            layout.addWidget(btn)

        # 默认勾选 + 信号转发
        self._buttons[self._mode].setChecked(True)
        self._group.idClicked.connect(self._on_id_clicked)

    # --------------------------------------------------------- 公共接口

    @property
    def mode(self) -> str:
        """当前模式 key（``MODE_TASK`` / ``MODE_PROJECT``）。"""
        return self._mode

    def set_mode(self, key: str, *, emit: bool = True) -> None:
        """程序化设置模式（如从工程文件恢复）；默认 emit 触发信号。"""
        if key not in self._buttons:
            return
        if key == self._mode:
            return
        self._mode = key
        self._buttons[key].setChecked(True)
        if emit:
            self.mode_changed.emit(key)

    def apply_theme(self, theme) -> None:
        """主题切换时刷新样式（colors 全从 theme 字段取，无硬编码）。"""
        self.setStyleSheet(_mode_switch_qss(theme))

    # --------------------------------------------------------- 内部

    def _on_id_clicked(self, button_id: int) -> None:
        # buttonId 顺序与 _MODE_OPTIONS 一致
        if 0 <= button_id < len(_MODE_OPTIONS):
            key = _MODE_OPTIONS[button_id][0]
            if key != self._mode:
                self._mode = key
                self.mode_changed.emit(key)


class _TitleButton(QToolButton):
    """窗口控制按钮：hover 时切换图标（关闭按钮 hover 变白、背景变红）。"""

    def __init__(self, icon: QIcon, hover_icon: QIcon = None, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._normal_icon = icon
        self._hover_icon = hover_icon or icon
        self.setIcon(icon)
        self.setFixedSize(46, TITLE_BAR_HEIGHT)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAutoRaise(True)

    def set_icons(self, normal: QIcon, hover: QIcon = None) -> None:
        self._normal_icon = normal
        if hover is not None:
            self._hover_icon = hover
        if not self.underMouse():
            self.setIcon(self._normal_icon)

    def enterEvent(self, event) -> None:
        self.setIcon(self._hover_icon)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.setIcon(self._normal_icon)
        super().leaveEvent(event)


class TitleBar(QWidget):
    """自绘标题栏。

    空白区域可拖动：Windows 由 :class:`FramelessMixin.nativeEvent` 的 ``WM_NCHITTEST``
    接管（返回 ``HTCAPTION``），其他平台走本组件的鼠标兜底。

    默认展示：应用图标 + 窗口标题 + 用户信息 + 主题切换 + 模式切换 +
    最小化/最大化/关闭。流程编辑器 / 对话框通过 ``show_user`` / ``show_theme`` /
    ``show_mode_switch`` 关掉非窗口控制元素，只留图标 / 标题 / 窗口按钮。
    """

    minimize_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    close_requested = pyqtSignal()
    mode_changed = pyqtSignal(str)  # 转发 _ModeSwitch.mode_changed（``MODE_TASK`` / ``MODE_PROJECT``）

    def __init__(
        self,
        parent: QWidget = None,
        show_min_button: bool = True,
        show_max_button: bool = True,
        show_user: bool = True,
        show_theme: bool = True,
        show_mode_switch: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # 让 QSS 背景色生效

        self._maximized = False
        self._drag_offset = None
        self._icon_color = "#5a5f66"  # 窗口按钮图标色（随主题更新）

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 4, 0)
        layout.setSpacing(0)

        # 应用图标 + 窗口标题（非交互区，拖动用）
        self._icon_label = QLabel(self)
        self._icon_label.setPixmap(Icons.app().pixmap(18, 18))
        layout.addWidget(self._icon_label)
        layout.addSpacing(6)

        self._title_label = QLabel(self)
        self._title_label.setObjectName("TitleBarTitle")
        self._title_label.setText(self.window().windowTitle() if self.window() else "")
        layout.addWidget(self._title_label, 1)

        layout.addSpacing(8)

        # 用户信息（头像 + 用户名），点击弹出账号菜单
        self._user_btn: QToolButton | None = None
        self._user_menu: QMenu | None = None
        if show_user:
            self._user_btn = QToolButton(self)
            self._user_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._user_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self._user_btn.setIconSize(QSize(22, 22))
            self._user_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._user_menu = QMenu(self._user_btn)
            self._user_btn.setMenu(self._user_menu)
            self._sync_login_state()
            layout.addWidget(self._user_btn)

        # 主题切换按钮（调色盘图标，弹出主题菜单）
        self._btn_theme: _TitleButton | None = None
        self._theme_menu: QMenu | None = None
        if show_theme:
            self._btn_theme = _TitleButton(_theme_icon(), parent=self)
            self._btn_theme.setToolTip("切换主题")
            self._btn_theme.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self._theme_menu = QMenu(self._btn_theme)
            self._btn_theme.setMenu(self._theme_menu)
            layout.addWidget(self._btn_theme)

        # 任务模式 / 工程模式 二选一开关（胶囊形分段控件）
        self._mode_switch: _ModeSwitch | None = None
        if show_mode_switch:
            self._mode_switch = _ModeSwitch(self)
            self._mode_switch.mode_changed.connect(self.mode_changed)
            layout.addSpacing(4)
            layout.addWidget(self._mode_switch)
            layout.addSpacing(4)

        # 窗口控制按钮
        self._btn_min = _TitleButton(_window_icon("min"), parent=self)
        self._btn_max = _TitleButton(_window_icon("max"), parent=self)
        self._btn_close = _TitleButton(
            _window_icon("close"),
            _window_icon("close", QColor("white")),
            parent=self,
        )
        self._btn_close.setObjectName("TitleBarClose")
        self._btn_min.clicked.connect(self.minimize_requested)
        self._btn_max.clicked.connect(self.maximize_requested)
        self._btn_close.clicked.connect(self.close_requested)
        layout.addWidget(self._btn_min)
        layout.addWidget(self._btn_max)
        layout.addWidget(self._btn_close)

        # 对话框等场景隐藏不必要的窗口按钮（隐藏的按钮不参与命中测试）
        if not show_min_button:
            self._btn_min.hide()
        if not show_max_button:
            self._btn_max.hide()

        # 可交互子控件（WM_NCHITTEST 判定是否返回 HTCLIENT 用）
        self._interactive = [
            b
            for b in (
                self._user_btn,
                self._btn_theme,
                self._mode_switch,
                self._btn_min,
                self._btn_max,
                self._btn_close,
            )
            if b is not None and not b.isHidden()
        ]

        # 主题：跟随 ThemeManager，切换时重绘自身配色（连接放最后，避免初始化期回调）
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._apply_theme(ThemeManager.instance().current())

    # ---------------------------------------------------------- 对外接口

    def set_title(self, text: str) -> None:
        self._title_label.setText(text)

    def set_maximized(self, maximized: bool) -> None:
        """最大化状态切换 → 更新最大/还原按钮图标（颜色跟随当前主题）。"""
        if maximized == self._maximized:
            return
        self._maximized = maximized
        kind = "restore" if maximized else "max"
        self._btn_max.set_icons(_window_icon(kind, QColor(self._icon_color)))

    def set_user(self, username: str, expires_at: int = None) -> None:
        """设置标题栏用户信息；``expires_at`` 为 unix 秒时显示剩余有效期。

        用户区被隐藏（``show_user=False``，如对话框标题栏）时无操作。
        """
        if self._user_btn is None or self._user_menu is None:
            return
        self._username = (username or "未登录").strip()
        self._user_btn.setText(self._username)
        self._user_btn.setIcon(QIcon(make_avatar_pixmap(self._username)))
        self._user_menu.clear()
        name_act = self._user_menu.addAction(f"账号：{self._username}")
        name_act.setEnabled(False)
        if expires_at:
            remaining = expires_at - int(time.time())
            days = max(0, remaining // 86400)
            exp_act = self._user_menu.addAction(f"凭证剩余：{days} 天")
            exp_act.setEnabled(False)

    def _sync_login_state(self) -> None:
        """与主窗口一致：构造时从本地凭证加载登录状态（未登录显示"未登录"）。"""
        if self._user_btn is None:
            return
        from app.core.auth import load_credential

        cred = load_credential()
        if cred is not None:
            self.set_user(cred.username, expires_at=cred.expires_at)
        else:
            self.set_user("未登录")

    # ---------------------------------------------------------- 主题切换

    def _select_theme(self, key: str) -> None:
        ThemeManager.instance().set_theme(key)

    def _rebuild_theme_menu(self) -> None:
        """按 :data:`THEMES` 重建主题菜单（勾选当前主题）。

        主题按钮被隐藏（``show_theme=False``）时无操作。
        """
        if self._theme_menu is None:
            return
        tm = ThemeManager.instance()
        self._theme_menu.clear()
        for key, theme in THEMES.items():
            act = self._theme_menu.addAction(theme.name)
            act.setCheckable(True)
            act.setChecked(key == tm.current().key)
            act.triggered.connect(lambda checked=False, k=key: self._select_theme(k))

    def _on_theme_changed(self, theme) -> None:
        self._apply_theme(theme)

    def _apply_theme(self, theme) -> None:
        """按主题重绘：QSS、窗口按钮图标色、用户/主题菜单配色、菜单勾选态。"""
        self.setStyleSheet(title_bar_qss(theme))
        self._icon_color = theme.title_icon
        icon_color = QColor(self._icon_color)
        self._btn_min.set_icons(_window_icon("min", icon_color))
        self._btn_max.set_icons(
            _window_icon("restore" if self._maximized else "max", icon_color)
        )
        self._btn_close.set_icons(
            _window_icon("close", icon_color),
            _window_icon("close", QColor("white")),
        )
        menu_style = (
            f"QMenu {{ background-color: {theme.menu_bg}; color: {theme.menu_text}; "
            f"border: 1px solid {theme.border}; }} "
            f"QMenu::item:selected {{ background-color: {theme.menu_hover}; }}"
        )
        if self._user_menu is not None:
            self._user_menu.setStyleSheet(menu_style)
        if self._theme_menu is not None:
            self._theme_menu.setStyleSheet(menu_style)
        if self._mode_switch is not None:
            self._mode_switch.apply_theme(theme)
        self._rebuild_theme_menu()

    def is_interactive_child(self, widget) -> bool:
        """``widget`` 是否落在可交互控件（窗口按钮 / 用户按钮）上。"""
        if widget is self:
            return False
        for child in self._interactive:
            if widget is child or child.isAncestorOf(widget):
                return True
        return False

    def is_interactive_pos(self, pos: QPoint) -> bool:
        """``pos``（标题栏本地坐标）是否落在任一可交互控件几何内。

        供 :class:`FramelessMixin._wm_nchittest` 做命中判定：按钮/用户信息区返回
        True（→ ``HTCLIENT``，Qt 正常处理点击），空白区返回 False（→ ``HTCAPTION``
        系统拖动）。直接用子控件几何命中，不依赖 ``QApplication.widgetAt`` 全局查询
        （后者在 Win11 / HiDPI 下可能返回错位控件，导致按钮区被误判为拖动区）。
        """
        return any(child.geometry().contains(pos) for child in self._interactive)

    def _is_interactive_pos(self, pos: QPoint) -> bool:
        return any(child.geometry().contains(pos) for child in self._interactive)

    # ---------------------------------------------------------- 非 Windows 拖动兜底

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._is_interactive_pos(event.pos()):
            frame = self.window().frameGeometry() if self.window() else None
            if frame is not None:
                self._drag_offset = global_pos(event) - frame.topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            handle = self.window().windowHandle()
            if handle is not None and hasattr(handle, "startSystemMove"):
                handle.startSystemMove()  # Qt 5.15+ 原生拖动（含贴边吸附）
            else:
                self.window().move(global_pos(event) - self._drag_offset)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._is_interactive_pos(event.pos()):
            self.maximize_requested.emit()
            return
        super().mouseDoubleClickEvent(event)
