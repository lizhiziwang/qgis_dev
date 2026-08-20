"""无边框窗口通用能力（mixin）。

把原来只写在 :class:`MainWindow` 里的「无边框 chrome」能力抽成可复用的
:class:`FramelessMixin`，让主窗口、流程编辑器（以及后续的对话框）共用同一套
标题栏 + 拖动 + 缩放 + 最大化修正，保证所有窗口 chrome 风格统一。

跨平台策略：

- **Windows**：``nativeEvent`` 拦截 ``WM_NCHITTEST``（标题栏拖动 + 边缘缩放）、
  ``WM_GETMINMAXINFO``（最大化修正到工作区）、``WM_NCLBUTTONDOWN`` /
  ``WM_NCLBUTTONDBLCLK``（Aero Snap 贴边吸附 + 双击最大化）。
- **Linux (X11)**：拖动走 :class:`TitleBar` 的 ``startSystemMove``；边缘缩放走
  本类的 ``eventFilter`` + ``QWindow.startSystemResize``；最大化用
  ``QScreen.availableGeometry`` 手动 setGeometry（避开 panel/任务栏）。
- **Linux (Wayland)**：``startSystemResize`` 依赖合成器支持，失败时回退到
  手动 ``setGeometry``（:meth:`_continue_manual_resize`）。

设计约定：

- :meth:`hit_test` 是**平台无关的纯函数**，命中检测逻辑可脱离 GUI 单测。
- mixin 不继承 QWidget，靠 duck typing 依赖宿主的 QWidget API；宿主必须把
  本 mixin 放在 MRO 最前：``class MainWindow(FramelessMixin, QMainWindow)``。
- 宿主仍各自负责自己的 ``window_qss`` / 画布 / 工具条主题刷新；mixin 只接管
  「窗口 chrome」这一层（标题栏 + 系统边框行为）。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import QMainWindow, QMenuBar, QTabBar, QVBoxLayout, QWidget

# ---------------------------------------------------------------------------
# Windows 无边框窗口辅助：WM_NCHITTEST 让系统接管"标题栏拖动 + 边缘缩放"；
# WM_GETMINMAXINFO 修正最大化几何；WM_NCLBUTTONDOWN(HTCAPTION) 重发 SC_MOVE
# 触发系统移动循环以获得 Aero Snap 贴边吸附；WM_NCLBUTTONDBLCLK(HTCAPTION)
# 双击标题栏最大化/还原。仅 Windows 平台会收到 windows_generic_MSG。
# ---------------------------------------------------------------------------

WM_NCHITTEST = 0x0084
WM_GETMINMAXINFO = 0x0024
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
SC_MOVE = 0xF010
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17
MONITOR_DEFAULTTONEAREST = 2


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


#: 边缘命中判定的像素宽度（窗口边缘外这一范围内都算可缩放）。
RESIZE_MARGIN = 6


def hit_test(rect, pos, margin: int = RESIZE_MARGIN) -> "Qt.Edge":
    """返回 ``pos``（``rect`` 本地坐标）落在哪条边缘（:class:`Qt.Edge` 组合）。

    纯函数、平台无关、可单测。语义与 Windows ``WM_NCHITTEST`` 一致：紧贴窗口
    外沿（负坐标）也算命中，方便用户贴边缩放。
    """
    x, y = pos.x(), pos.y()
    edges = Qt.Edge(0)
    if y <= margin:
        edges |= Qt.Edge.TopEdge
    elif y >= rect.height() - margin:
        edges |= Qt.Edge.BottomEdge
    if x <= margin:
        edges |= Qt.Edge.LeftEdge
    elif x >= rect.width() - margin:
        edges |= Qt.Edge.RightEdge
    return edges


def _cursor_for(edges) -> Qt.CursorShape:
    """根据边缘组合返回对应的调整大小光标形状。"""
    if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
        return Qt.CursorShape.SizeHorCursor
    if edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
        return Qt.CursorShape.SizeVerCursor
    if edges in (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge):
        return Qt.CursorShape.SizeFDiagCursor
    if edges in (Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge):
        return Qt.CursorShape.SizeBDiagCursor
    return Qt.CursorShape.ArrowCursor


class FramelessMixin:
    """无边框窗口能力（mixin）。

    宿主必须是 QMainWindow / QDialog 子类，并把本 mixin 放在 MRO 最前。用法::

        class MainWindow(FramelessMixin, QMainWindow):
            def __init__(self):
                super().__init__()
                self.title_bar = self.enable_frameless(with_tab_bar=True)
    """

    def enable_frameless(
        self,
        *,
        with_menu_bar: bool = False,
        with_tab_bar: bool = False,
        show_min_button: bool = True,
        show_max_button: bool = True,
    ):
        """把宿主设为无边框窗口，挂载自绘标题栏，并装配平台行为。

        :param with_menu_bar: QMainWindow 是否在标题栏下再挂一个菜单栏。
        :param with_tab_bar: 是否在标题栏下挂一个 QTabBar 取代菜单栏（MainWindow
            的"菜单栏改 tab 切换器"用）。与 ``with_menu_bar`` 互斥，优先。
        :param show_min_button: 标题栏是否显示最小化按钮（对话框传 False）。
        :param show_max_button: 标题栏是否显示最大化按钮（对话框传 False）。
        :return: 创建好的 :class:`TitleBar` 实例（也存到 ``self.title_bar``）。
        """
        from app.ui.main_window.title_bar import TitleBar

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # ---- 状态（mixin 无 __init__，在这里统一初始化）----
        self._resize_targets: set = set()
        self._resize_edges = Qt.Edge(0)  # 非 0 = 正在手动 resize（Wayland 兜底）

        # ---- 标题栏 + 信号 ----
        self.title_bar = TitleBar(self, show_min_button, show_max_button)
        self.title_bar.close_requested.connect(self._frameless_close)
        self.title_bar.maximize_requested.connect(self._toggle_maximize)
        if show_min_button:
            self.title_bar.minimize_requested.connect(self.showMinimized)
        self.windowTitleChanged.connect(self.title_bar.set_title)
        self.title_bar.set_title(self.windowTitle())  # 初始标题（构造时 windowTitle 可能已设）

        # ---- chrome 挂载：QMainWindow 走 setMenuWidget，QDialog 走布局顶部 ----
        if isinstance(self, QMainWindow):
            # 不能先 self.menuBar() 再 setMenuWidget 换掉菜单区：Qt 5.15 下该顺序
            # 会让随后任何 menuBar() 调用段错误（菜单栏已脱离 QMainWindow 布局仍被
            # 其持有）。所以全程不调 menuBar()，只做一次 setMenuWidget。
            chrome = QWidget(self)
            v = QVBoxLayout(chrome)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            v.addWidget(self.title_bar)
            if with_tab_bar:
                # Tab 切换器：取代菜单栏。objectName 供 window_qss 精确套样式
                # （QTabBar#ChromeTabBar），不影响 dock 里的普通 QTabBar。
                self._tab_bar = QTabBar(self)
                self._tab_bar.setObjectName("ChromeTabBar")
                v.addWidget(self._tab_bar)
            elif with_menu_bar:
                self._menubar = QMenuBar(self)
                v.addWidget(self._menubar)
            self.setMenuWidget(chrome)
        else:
            # QDialog 无 setMenuWidget：标题栏插入布局顶部，子类把内容加到
            # self._frameless_layout（预留，BaseDialog 接入时使用）。
            self._frameless_layout = QVBoxLayout(self)
            self._frameless_layout.setContentsMargins(0, 0, 0, 0)
            self._frameless_layout.setSpacing(0)
            self._frameless_layout.addWidget(self.title_bar)

        # ---- Linux/X11 边缘缩放需要鼠标追踪 ----
        if not self._is_windows():
            self.setMouseTracking(True)

        return self.title_bar

    # ------------------------------------------------------------ 平台判定

    @staticmethod
    def _is_windows() -> bool:
        return sys.platform == "win32"

    @staticmethod
    def _is_x11() -> bool:
        """是否运行在 Linux + X11 会话（Qt 6 平台名 ``xcb``）。"""
        if sys.platform != "linux":
            return False
        app = QGuiApplication.instance()
        return app is not None and app.platformName() == "xcb"

    # ------------------------------------------------------------ 关闭 / 最大化

    def _frameless_close(self) -> None:
        """标题栏关闭按钮：QDialog 走 reject，其它走 close。"""
        from PyQt6.QtWidgets import QDialog

        if isinstance(self, QDialog):
            self.reject()
        else:
            self.close()

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        elif self._is_windows():
            self.showMaximized()          # WM_GETMINMAXINFO 已修正到工作区
        elif self._is_x11():
            # X11：availableGeometry 已扣掉 panel/任务栏，手动 setGeometry 避免盖住
            screen = self.screen()
            if screen is not None:
                self._restore_geo = self.geometry()
                self.setGeometry(screen.availableGeometry())
            else:
                self.showMaximized()
        else:
            self.showMaximized()          # Wayland：交给合成器

    def changeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """最大化/还原状态变化 → 同步标题栏按钮图标。"""
        if event.type() == QEvent.Type.WindowStateChange:
            tb = getattr(self, "title_bar", None)
            if tb is not None and hasattr(self, "isMaximized"):
                tb.set_maximized(self.isMaximized())
        super().changeEvent(event)

    # ------------------------------------------------------------ Windows 原生事件

    def nativeEvent(self, eventType, message):
        """Windows：拦截 WM_NCHITTEST / WM_GETMINMAXINFO 等，让系统接管拖动/缩放。

        其它平台 ``windows_generic_MSG`` 不会出现，直接透传给 Qt 默认。
        """
        if self._is_windows():
            try:
                if bytes(eventType) == b"windows_generic_MSG":
                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == WM_NCHITTEST:
                        return (True, self._wm_nchittest(msg))
                    if msg.message == WM_GETMINMAXINFO:
                        self._wm_getminmaxinfo(msg)
                        return (True, 0)
                    if msg.message == WM_NCLBUTTONDOWN and msg.wParam == HTCAPTION:
                        # 无边框窗口下 Qt 把标题栏区当客户区，系统原生拖动（含 Aero
                        # Snap 贴边吸附）不会启动。ReleaseCapture + 重发
                        # WM_SYSCOMMAND(SC_MOVE|HTCAPTION) 让系统进入原生移动循环。
                        ctypes.windll.user32.ReleaseCapture()
                        ctypes.windll.user32.SendMessageW(
                            int(self.winId()), WM_SYSCOMMAND, SC_MOVE | HTCAPTION, 0
                        )
                        return (True, 0)
                    if msg.message == WM_NCLBUTTONDBLCLK and msg.wParam == HTCAPTION:
                        self._toggle_maximize()
                        return (True, 0)
            except Exception:  # noqa: BLE001 —— 兜底走 Qt 默认，不拖垮事件循环
                pass
        # PyQt6 下 super().nativeEvent 对未处理的消息返回 (False, None)；把这个 None
        # 直接透传回 Qt 时，sip 无法把 None 转成 qintptr 结果参数，抛出的 TypeError
        # 会穿进 C++ 事件分发，导致原生访问冲突（进程无 traceback 退出）。必须强转 int。
        ok, res = super().nativeEvent(eventType, message)
        return (ok, res if isinstance(res, int) else 0)

    def _wm_nchittest(self, msg) -> int:
        """命中测试：窗口边缘 → resize 码；标题栏空白 → HTCAPTION；其余 HTCLIENT。"""
        lp = int(msg.lParam)
        x = lp & 0xFFFF
        y = (lp >> 16) & 0xFFFF
        if x & 0x8000:
            x -= 0x10000
        if y & 0x8000:
            y -= 0x10000

        # WM_NCHITTEST 的 lParam 是**物理像素**屏幕坐标；Qt 的 widget/geometry
        # 坐标系是逻辑像素。HiDPI 缩放下（Win11 高分屏常见 125%/150%）两者不一致，
        # 不换算会与下面的 frameGeometry()/mapFromGlobal 全部错位：按钮区可能被
        # 误判成拖动区（HTCAPTION），点击被系统吃掉。DPI-unaware 进程 dpr=1 时
        # 该换算退化为恒等，不影响。
        ratio = self.devicePixelRatioF()
        if ratio not in (0.0, 1.0):
            x = round(x / ratio)
            y = round(y / ratio)

        if self.isMaximized():
            return HTCLIENT  # 最大化时禁止拖动 / 缩放

        g = self.frameGeometry()
        left, top, right, bottom = g.left(), g.top(), g.right(), g.bottom()
        m = RESIZE_MARGIN
        if y <= top + m:
            if x <= left + m:
                return HTTOPLEFT
            if x >= right - m:
                return HTTOPRIGHT
            return HTTOP
        if y >= bottom - m:
            if x <= left + m:
                return HTBOTTOMLEFT
            if x >= right - m:
                return HTBOTTOMRIGHT
            return HTBOTTOM
        if x <= left + m:
            return HTLEFT
        if x >= right - m:
            return HTRIGHT

        # 标题栏空白区域 → 系统拖动；按钮/用户信息区域交给 Qt 正常处理。
        # 用子控件几何命中（is_interactive_pos）而不是 QApplication.widgetAt：
        # widgetAt 是全局查询，在 Win11 / HiDPI / 跨屏缩放下可能返回 None 或错位
        # 控件，导致按钮区被误判为 HTCAPTION（点击被系统当成拖动吃掉）。
        tb = getattr(self, "title_bar", None)
        if tb is not None and not tb.isHidden():
            tb_tl = tb.mapTo(self, QPoint(0, 0))
            if QRect(tb_tl, tb.size()).contains(QPoint(x - left, y - top)):
                if not tb.is_interactive_pos(tb.mapFromGlobal(QPoint(x, y))):
                    return HTCAPTION
        return HTCLIENT

    def _wm_getminmaxinfo(self, msg) -> None:
        """修正最大化几何：无边框窗口最大化时填满工作区，不盖住任务栏。"""
        mmi = _MINMAXINFO.from_address(int(msg.lParam))
        monitor = ctypes.windll.user32.MonitorFromWindow(
            int(self.winId()), MONITOR_DEFAULTTONEAREST
        )
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        w = info.rcWork.right - info.rcWork.left
        h = info.rcWork.bottom - info.rcWork.top
        mmi.ptMaxSize.x, mmi.ptMaxSize.y = w, h
        mmi.ptMaxPosition.x, mmi.ptMaxPosition.y = info.rcWork.left, info.rcWork.top
        mmi.ptMaxTrackSize.x, mmi.ptMaxTrackSize.y = w, h

    # ------------------------------------------------------------ Linux 边缘缩放

    def monitor_resize_edges(self, widget) -> None:
        """把 ``widget`` 注册为 Linux 边缘缩放监控目标。

        ``widget`` 必须已 ``installEventFilter(self)``（本方法只负责记录 +
        开鼠标追踪，不重复安装，避免同一 filter 对同一 widget 过滤两次）。
        宿主对 central widget（canvas / FlowView）调用即可。
        """
        if widget not in self._resize_targets:
            self._resize_targets.add(widget)
            widget.setMouseTracking(True)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 —— Qt 命名
        """Linux/X11 边缘缩放：在注册的 resize target 上检测边缘 → 光标 / 缩放。

        与宿主的 ``eventFilter``（如 MainWindow 的画布拖拽拦截）通过
        ``super().eventFilter`` 沿 MRO 协同，互不覆盖。
        """
        if not self._is_windows() and watched in getattr(self, "_resize_targets", ()):
            t = event.type()
            if t == QEvent.Type.MouseMove:
                if self._resize_edges == Qt.Edge(0):
                    edges = hit_test(self.rect(), watched.mapTo(self, event.position().toPoint()))
                    self._apply_resize_cursor(watched, edges)
            elif t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                edges = hit_test(self.rect(), watched.mapTo(self, event.position().toPoint()))
                if edges != Qt.Edge(0):
                    self._start_system_resize(edges)
                    return True
        return super().eventFilter(watched, event)

    def _apply_resize_cursor(self, watched, edges) -> None:
        watched.setCursor(_cursor_for(edges))

    def _start_system_resize(self, edges) -> None:
        """优先走 ``QWindow.startSystemResize``（X11 原生）；失败回退手动 resize。"""
        handle = self.windowHandle()
        if handle is not None and hasattr(handle, "startSystemResize"):
            try:
                if handle.startSystemResize(edges):
                    return
            except Exception:  # noqa: BLE001
                pass
        # Wayland / 合成器不支持：记录初始状态，由 _continue_manual_resize 驱动
        self._resize_edges = edges
        self._resize_start_pos = QCursor.pos()
        self._resize_start_geo = self.geometry()
        self.grabMouse()

    def _continue_manual_resize(self, global_pos) -> None:
        if self._resize_edges == Qt.Edge(0):
            return
        delta = global_pos - self._resize_start_pos
        geo = QRect(self._resize_start_geo)
        edges = self._resize_edges
        if edges & Qt.Edge.LeftEdge:
            geo.setLeft(geo.left() + delta.x())
        if edges & Qt.Edge.RightEdge:
            geo.setRight(geo.right() + delta.x())
        if edges & Qt.Edge.TopEdge:
            geo.setTop(geo.top() + delta.y())
        if edges & Qt.Edge.BottomEdge:
            geo.setBottom(geo.bottom() + delta.y())
        self.setGeometry(geo)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """手动 resize 兜底：grabMouse 后 MouseMove 直接发给窗口自身。"""
        if self._resize_edges != Qt.Edge(0):
            self._continue_manual_resize(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        """手动 resize 结束：松开鼠标清除状态。"""
        if self._resize_edges != Qt.Edge(0):
            self._resize_edges = Qt.Edge(0)
            self.releaseMouse()
        super().mouseReleaseEvent(event)


__all__ = ["FramelessMixin", "RESIZE_MARGIN", "hit_test"]
