"""自定义标题栏（方案 A：无边框窗口 + 自绘标题栏）。

布局：应用图标 | 窗口标题 …… | 用户信息(头像+用户名) | 主题切换 | 最小化 | 最大化/还原 | 关闭

拖动 / 缩放不在本组件内实现：由 :class:`MainWindow.nativeEvent` 拦截
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

from PyQt5.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from app.ui.common.icons import Icons
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
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(_avatar_color(name)))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    letter = (name or "?").strip()[0].upper()
    font = QFont("Microsoft YaHei")
    font.setBold(True)
    font.setPixelSize(int(size * 0.55))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, letter)
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
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(1.1)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
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
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    colors = ("#4A90E2", "#3AA78F", "#E2725B", "#C974A8")
    radius = 2
    for i, color in enumerate(colors):
        cx = 2 + (i % 2) * 4
        cy = 2 + (i // 2) * 4
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(cx, cy, radius * 2, radius * 2)
    painter.end()
    return QIcon(pm)


class _TitleButton(QToolButton):
    """窗口控制按钮：hover 时切换图标（关闭按钮 hover 变白、背景变红）。"""

    def __init__(self, icon: QIcon, hover_icon: QIcon = None, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._normal_icon = icon
        self._hover_icon = hover_icon or icon
        self.setIcon(icon)
        self.setFixedSize(46, TITLE_BAR_HEIGHT)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
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

    空白区域可拖动：Windows 由 :class:`MainWindow.nativeEvent` 的 ``WM_NCHITTEST``
    接管（返回 ``HTCAPTION``），其他平台走本组件的鼠标兜底。
    """

    minimize_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setAttribute(Qt.WA_StyledBackground, True)  # 让 QSS 背景色生效

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
        self._user_btn = QToolButton(self)
        self._user_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._user_btn.setPopupMode(QToolButton.InstantPopup)
        self._user_btn.setIconSize(QSize(22, 22))
        self._user_btn.setCursor(Qt.PointingHandCursor)
        self._user_menu = QMenu(self._user_btn)
        self._user_btn.setMenu(self._user_menu)
        self.set_user("未登录")
        layout.addWidget(self._user_btn)

        # 主题切换按钮（调色盘图标，弹出主题菜单）
        self._btn_theme = _TitleButton(_theme_icon(), parent=self)
        self._btn_theme.setToolTip("切换主题")
        self._btn_theme.setPopupMode(QToolButton.InstantPopup)
        self._theme_menu = QMenu(self._btn_theme)
        self._btn_theme.setMenu(self._theme_menu)
        layout.addWidget(self._btn_theme)

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

        # 可交互子控件（WM_NCHITTEST 判定是否返回 HTCLIENT 用）
        self._interactive = [
            self._user_btn,
            self._btn_theme,
            self._btn_min,
            self._btn_max,
            self._btn_close,
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
        """设置标题栏用户信息；``expires_at`` 为 unix 秒时显示剩余有效期。"""
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

    # ---------------------------------------------------------- 主题切换

    def _select_theme(self, key: str) -> None:
        ThemeManager.instance().set_theme(key)

    def _rebuild_theme_menu(self) -> None:
        """按 :data:`THEMES` 重建主题菜单（勾选当前主题）。"""
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
        self._user_menu.setStyleSheet(menu_style)
        self._theme_menu.setStyleSheet(menu_style)
        self._rebuild_theme_menu()

    def is_interactive_child(self, widget) -> bool:
        """``widget`` 是否落在可交互控件（窗口按钮 / 用户按钮）上。"""
        if widget is self:
            return False
        for child in self._interactive:
            if widget is child or child.isAncestorOf(widget):
                return True
        return False

    def _is_interactive_pos(self, pos: QPoint) -> bool:
        return any(child.geometry().contains(pos) for child in self._interactive)

    # ---------------------------------------------------------- 非 Windows 拖动兜底

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._is_interactive_pos(event.pos()):
            frame = self.window().frameGeometry() if self.window() else None
            if frame is not None:
                self._drag_offset = event.globalPos() - frame.topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            handle = self.window().windowHandle()
            if handle is not None and hasattr(handle, "startSystemMove"):
                handle.startSystemMove()  # Qt 5.15+ 原生拖动（含贴边吸附）
            else:
                self.window().move(event.globalPos() - self._drag_offset)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._is_interactive_pos(event.pos()):
            self.maximize_requested.emit()
            return
        super().mouseDoubleClickEvent(event)
