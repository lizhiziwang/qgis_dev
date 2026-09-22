"""无边框窗口通用能力（mixin）。

把「无边框 chrome」能力抽成可复用的 :class:`FramelessMixin`，让主窗口、流程
编辑器、对话框共用同一套标题栏 + 拖动 + 缩放 + 最大化修正，保证所有窗口
chrome 风格统一。

跨平台策略（各走最省事、效果最好的一条路）：

- **Windows**：``nativeEvent`` 拦截 ``WM_NCHITTEST``（标题栏拖动 + 边缘缩放）、
  ``WM_SETCURSOR``（补上被 Qt 重置的缩放光标）、``WM_GETMINMAXINFO``（最大化
  填满工作区 + 缩放下限）、``WM_NCLBUTTONDOWN`` / ``WM_NCLBUTTONDBLCLK``
  （Aero Snap 贴边吸附 + 双击最大化）。缩放本身交给系统，手感最好。
- **其它平台（X11 / Wayland）**：由 :class:`_WindowFrame` 这层置顶子控件在
  窗口四周的 8px 边框带上接管鼠标：移动改光标，按下调
  ``QWindow.startSystemResize``（X11 原生 / Wayland 交给合成器），失败再回退
  手动 ``setGeometry``。

设计约定：

- :func:`hit_test` / :func:`resized_geometry` 是**平台无关的纯函数**，命中与
  缩放几何逻辑可脱离 GUI 单测；Windows 与其它平台共用同一套命中结果。
- mixin 不继承 QWidget，靠 duck typing 依赖宿主的 QWidget API；宿主必须把
  本 mixin 放在 QWidget 基类**之前**，其它功能 mixin 可以排在其前面，并通过
  ``super()`` 链协同：``class MainWindow(WindowShellMixin, FramelessMixin, QMainWindow)``。
- 描边与边缘缩放统一由 :class:`_WindowFrame` 承担：铺满客户区、始终置顶、只在
  最外圈 :data:`WINDOW_BORDER_WIDTH` 像素画描边；非 Windows 平台再用 ``setMask``
  把它裁成"边框带"，于是只有贴边那几像素吃鼠标，窗口中间的事件照常落到子控件。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from qgis.PyQt.QtCore import QEvent, QPoint, QRect, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPainter, QPen, QRegion
from qgis.PyQt.QtWidgets import QMainWindow, QMenuBar, QTabBar, QVBoxLayout, QWidget

from app.ui.common.qt_compat import event_pos, global_pos
from app.ui.common.theme import ThemeManager
from app.ui.main_window.title_bar import TitleBar

# ---------------------------------------------------------------------------
# Windows 无边框窗口辅助。无边框窗口在 WM_NCHITTEST 里返回 HT* 后，系统并不
# 自己设置缩放光标（Qt 的 WM_SETCURSOR 处理会把非客户区光标重置成控件的
# 箭头），所以要自己处理 WM_SETCURSOR（见 :meth:`FramelessMixin._win_set_cursor`）。
# 仅 Windows 平台会收到 windows_generic_MSG。
# ---------------------------------------------------------------------------

WM_NCHITTEST = 0x0084
WM_SETCURSOR = 0x0020
WM_GETMINMAXINFO = 0x0024
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
SC_MOVE = 0xF010
SC_SIZE = 0xF000
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
#: 全部边缘缩放 HT 码（WM_NCHITTEST 的 HT* 返回值中用于启动系统缩放循环的那些）。
_SIZE_HT_CODES = frozenset(
    {HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT}
)
MONITOR_DEFAULTTONEAREST = 2

#: Windows 系统光标资源 id（``LoadCursorW(NULL, id)``，即 IDC_*）。
IDC_ARROW = 32512
IDC_SIZEWE = 32644
IDC_SIZENS = 32645
IDC_SIZENWSE = 32642
IDC_SIZENESW = 32643
_HT_RESIZE_CURSOR_ID = {
    HTLEFT: IDC_SIZEWE,
    HTRIGHT: IDC_SIZEWE,
    HTTOP: IDC_SIZENS,
    HTBOTTOM: IDC_SIZENS,
    HTTOPLEFT: IDC_SIZENWSE,
    HTBOTTOMRIGHT: IDC_SIZENWSE,
    HTTOPRIGHT: IDC_SIZENESW,
    HTBOTTOMLEFT: IDC_SIZENESW,
}
#: ``LoadCursorW`` 返回的是系统共享光标句柄（不可 DestroyCursor），缓存避免每次
#: 鼠标移动都重新加载。
_win_cursor_cache: dict = {}


def _load_win_cursor(cursor_id: int) -> int:
    """按系统光标 id 加载共享光标句柄（带缓存）。仅 Windows 调用。"""
    handle = _win_cursor_cache.get(cursor_id)
    if handle is None:
        handle = ctypes.windll.user32.LoadCursorW(None, cursor_id)
        _win_cursor_cache[cursor_id] = handle
    return handle


def resize_cursor_id(ht: int):
    """Windows HT 缩放码 → 光标资源 id；非缩放码返回 ``None``。纯函数、可单测。"""
    return _HT_RESIZE_CURSOR_ID.get(ht)


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


#: 边缘命中判定的像素宽度（窗口边缘内这一范围内都算可缩放）。
#: 取 Windows 系统边框默认值（SM_CXSIZEFRAME + SM_CXPADDEDBORDER = 8），
#: 比描边（:data:`WINDOW_BORDER_WIDTH` = 2px）宽，命中区域才有手感。
RESIZE_MARGIN = 8

#: 无边框窗口描边宽度（px）。无边框窗口没有系统边框，描边用于和桌面 /
#: 其它窗口区分，故比普通分隔线（1px）略粗。
WINDOW_BORDER_WIDTH = 2

#: :func:`hit_test` 结果 → Windows ``WM_NCHITTEST`` 返回码。
_EDGE_TO_HT = {
    Qt.Edge.LeftEdge: HTLEFT,
    Qt.Edge.RightEdge: HTRIGHT,
    Qt.Edge.TopEdge: HTTOP,
    Qt.Edge.BottomEdge: HTBOTTOM,
    Qt.Edge.TopEdge | Qt.Edge.LeftEdge: HTTOPLEFT,
    Qt.Edge.TopEdge | Qt.Edge.RightEdge: HTTOPRIGHT,
    Qt.Edge.BottomEdge | Qt.Edge.LeftEdge: HTBOTTOMLEFT,
    Qt.Edge.BottomEdge | Qt.Edge.RightEdge: HTBOTTOMRIGHT,
}


def hit_test(rect, pos, margin: int = RESIZE_MARGIN) -> Qt.Edge:
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


def resized_geometry(start_geo: QRect, edges, delta: QPoint) -> QRect:
    """按拖动增量 ``delta`` 把 ``start_geo`` 的 ``edges`` 边推出去。纯函数、可单测。"""
    geo = QRect(start_geo)
    if edges & Qt.Edge.LeftEdge:
        geo.setLeft(geo.left() + delta.x())
    if edges & Qt.Edge.RightEdge:
        geo.setRight(geo.right() + delta.x())
    if edges & Qt.Edge.TopEdge:
        geo.setTop(geo.top() + delta.y())
    if edges & Qt.Edge.BottomEdge:
        geo.setBottom(geo.bottom() + delta.y())
    return geo


def _cursor_for(edges) -> Qt.CursorShape:
    """边缘组合 → 调整大小光标形状。"""
    if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
        return Qt.CursorShape.SizeHorCursor
    if edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
        return Qt.CursorShape.SizeVerCursor
    if edges in (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge):
        return Qt.CursorShape.SizeFDiagCursor
    if edges in (Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge):
        return Qt.CursorShape.SizeBDiagCursor
    return Qt.CursorShape.ArrowCursor


def sc_size_wparam(ht: int) -> int:
    """把 WM_NCHITTEST 的 HT 码映射成 ``WM_SYSCOMMAND`` + ``SC_SIZE`` 的 wParam。

    不能直接 ``SC_SIZE | ht``：HTBOTTOMRIGHT=17 占 5 bit，``0xF000 | 17 = 0xF011``，
    系统按 ``wParam & 0xFFF0`` 取命令会得到 SC_MOVE(0xF010)，缩放被当成拖动。
    SC_SIZE 的方向编号与 HT 码错一位：HTLEFT..HTBOTTOMRIGHT(10..17) → 1..8。
    """
    return SC_SIZE | (ht - 9)


class _WindowFrame(QWidget):
    """无边框窗口的描边层，非 Windows 平台同时是边缘缩放手柄。

    铺满整个客户区并置顶，只在最外圈画 :data:`WINDOW_BORDER_WIDTH` 像素描边：

    - 颜色在 :meth:`paintEvent` 里实时读当前主题的 ``border``，主题切换时宿主
      重刷 QSS 会顺带触发重绘，无需额外信号连接；
    - 自带事件过滤器监听宿主窗口的 Resize / Show / LayoutRequest /
      WindowStateChange，自动同步几何、显隐、层级——宿主不必再写一堆
      ``resizeEvent`` / ``showEvent`` / ``event`` 样板；
    - 最大化 / 全屏时自动隐藏（此时系统窗口也无需描边）。

    平台差异：

    - **Windows**：``WA_TransparentForMouseEvents``，不吃鼠标事件；缩放交给
      ``WM_NCHITTEST`` 原生处理，描边层纯装饰。
    - **其它平台**：用 ``setMask`` 把它裁成边框带，于是只有贴着窗口外沿的
      ``RESIZE_MARGIN`` 像素吃鼠标（窗口中间的事件照常落到子控件）。移动改
      光标，按下调 ``startSystemResize``，失败回退手动 ``setGeometry``。
    """

    def __init__(self, parent: QWidget, *, interactive: bool, paint_border: bool) -> None:
        super().__init__(parent)
        self._interactive = interactive
        self._paint_border = paint_border
        self._resize_edges = Qt.Edge(0)
        self._resize_start_pos = QPoint()
        self._resize_start_geo = QRect()
        self.setObjectName("WindowFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if interactive:
            self.setMouseTracking(True)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        parent.installEventFilter(self)
        self._sync()

    # ------------------------------------------------------------- 同步

    def eventFilter(self, watched, event) -> bool:
        if watched is self.parentWidget() and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
            QEvent.Type.WindowStateChange,
        ):
            self._sync()
        return False

    def _sync(self) -> None:
        window = self.parentWidget()
        if window is None:
            return
        target = QRect(0, 0, window.width(), window.height())
        if self.geometry() != target:
            self.setGeometry(target)
        hidden = (
            window.isMaximized()
            or window.isFullScreen()
            or getattr(window, "_visual_maximized", False)
        )
        self.setVisible(not hidden)
        if hidden:
            return
        self.raise_()
        if self._interactive:
            inner = self.rect().adjusted(
                RESIZE_MARGIN, RESIZE_MARGIN, -RESIZE_MARGIN, -RESIZE_MARGIN
            )
            self.setMask(QRegion(self.rect()) - QRegion(inner))

    # ------------------------------------------------------------- 绘制

    def paintEvent(self, _event) -> None:
        if not self._paint_border:
            return
        theme = ThemeManager.instance().current()
        painter = QPainter(self)
        painter.setPen(QPen(QColor(theme.border), WINDOW_BORDER_WIDTH))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 画笔以路径为中心向两侧展开；把路径内缩半个线宽，描边才恰好落在
        # 客户区内侧（1px→1px，2px→2px……），不会向窗口外溢半像素。
        half = WINDOW_BORDER_WIDTH / 2.0
        painter.drawRect(QRectF(self.rect()).adjusted(half, half, -half, -half))

    # ------------------------------------------------------- 边缘缩放（非 Windows）

    def mouseMoveEvent(self, event) -> None:
        if self._resize_edges != Qt.Edge(0):
            self._drag_resize(global_pos(event))
            return
        self.setCursor(_cursor_for(hit_test(self.rect(), event_pos(event))))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            edges = hit_test(self.rect(), event_pos(event))
            if edges != Qt.Edge(0):
                self._begin_resize(edges, global_pos(event))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._resize_edges != Qt.Edge(0):
            self._resize_edges = Qt.Edge(0)
            self.releaseMouse()
        super().mouseReleaseEvent(event)

    def _begin_resize(self, edges, start_pos: QPoint) -> None:
        """优先 ``QWindow.startSystemResize``（X11 原生 / Wayland 合成器），失败回退手动。

        手动兜底用**按下事件的全局坐标**做锚点（而非 ``QCursor.pos()``），跨屏 / 光标
        被 warp 时更可靠。
        """
        handle = self.window().windowHandle()
        if handle is not None and hasattr(handle, "startSystemResize"):
            try:
                if handle.startSystemResize(edges):
                    return
            except Exception:  # noqa: BLE001 —— 合成器不支持时静默回退
                pass
        self._resize_edges = edges
        self._resize_start_pos = start_pos
        self._resize_start_geo = self.window().geometry()
        self.grabMouse()

    def _drag_resize(self, global_position: QPoint) -> None:
        delta = global_position - self._resize_start_pos
        self.window().setGeometry(
            resized_geometry(self._resize_start_geo, self._resize_edges, delta)
        )


class FramelessMixin:
    """无边框窗口能力（mixin）。

    宿主必须是 QMainWindow / QDialog 子类，并把本 mixin 放在 QWidget 基类之前
    （其它功能 mixin 可排在其前面，通过 ``super()`` 链协同）。用法::

        class MainWindow(WindowShellMixin, FramelessMixin, QMainWindow):
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
        show_user_button: bool = True,
        show_theme_button: bool = True,
        show_mode_switch: bool = True,
        with_border: bool = True,
    ):
        """把宿主设为无边框窗口，挂载自绘标题栏，并装配平台行为。

        :param with_menu_bar: QMainWindow 是否在标题栏下再挂一个菜单栏。
        :param with_tab_bar: 是否在标题栏下挂一个 QTabBar 取代菜单栏（MainWindow
            的"菜单栏改 tab 切换器"用）。与 ``with_menu_bar`` 互斥，优先。
        :param show_min_button: 标题栏是否显示最小化按钮（对话框传 False）。
        :param show_max_button: 标题栏是否显示最大化按钮（对话框传 False）。
        :param show_user_button: 标题栏是否显示用户信息（头像 + 用户名）。
        :param show_theme_button: 标题栏是否显示主题切换按钮。
        :param show_mode_switch: 标题栏是否显示任务/工程模式开关。
            流程编辑器 / 对话框把后三个传 False，只留 图标/标题/窗口按钮。
        :param with_border: 是否给窗口画一圈描边（默认开，宽度
            :data:`WINDOW_BORDER_WIDTH`）。无边框窗口没有系统边框，描边用于
            和桌面 / 其它窗口区分；全屏类窗口可传 False。
        :return: 创建好的 :class:`TitleBar` 实例（也存到 ``self.title_bar``）。
        """
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # ---- 状态（mixin 无 __init__，在这里统一初始化）----
        self._visual_maximized = False  # X11 手动放大标记（windowState 不反映）
        self._restore_geo = None        # X11 手动放大前的几何，还原时恢复

        # ---- 标题栏 + 信号 ----
        self.title_bar = TitleBar(
            self,
            show_min_button,
            show_max_button,
            show_user_button,
            show_theme_button,
            show_mode_switch,
        )
        self.title_bar.close_requested.connect(self._frameless_close)
        self.title_bar.maximize_requested.connect(self._toggle_maximize)
        if show_min_button:
            self.title_bar.minimize_requested.connect(self.showMinimized)
        self.windowTitleChanged.connect(self.title_bar.set_title)
        self.title_bar.set_title(self.windowTitle())  # 构造时 windowTitle 可能已设

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

        # ---- 描边 +（非 Windows）边缘缩放手柄（置顶透明子控件，自动跟随宿主）----
        self._window_frame = _WindowFrame(
            self,
            interactive=not self._is_windows(),
            paint_border=with_border,
        )

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
        from qgis.PyQt.QtWidgets import QDialog

        if isinstance(self, QDialog):
            self.reject()
        else:
            self.close()

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        elif self._visual_maximized:
            # X11 手动放大（windowState 非 Maximized，isMaximized() 为 False）
            # → 恢复放大前的几何，避免再点按钮走"放大"分支而无法还原
            if self._restore_geo is not None:
                self.setGeometry(self._restore_geo)
            else:
                self.showNormal()
            self._visual_maximized = False
        elif self._is_x11():
            # X11：availableGeometry 已扣掉 panel/任务栏，手动 setGeometry 避免盖住
            screen = self.screen()
            if screen is None:
                self.showMaximized()
            else:
                self._restore_geo = self.geometry()
                self._visual_maximized = True  # 先置位，setGeometry 触发的 Resize 才知道要隐藏描边
                self.setGeometry(screen.availableGeometry())
        else:
            # Windows：WM_GETMINMAXINFO 已修正到工作区；Wayland：交给合成器
            self.showMaximized()
        self._sync_max_button()

    def _sync_max_button(self) -> None:
        """同步标题栏最大化按钮图标（X11 手动放大不触发 WindowStateChange）。"""
        tb = getattr(self, "title_bar", None)
        if tb is not None:
            tb.set_maximized(self.isMaximized() or self._visual_maximized)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_max_button()
        super().changeEvent(event)

    # ------------------------------------------------------------ Windows 原生事件

    def nativeEvent(self, eventType, message):
        """Windows：拦截 WM_NCHITTEST / WM_SETCURSOR / WM_GETMINMAXINFO / WM_NCLBUTTON*。

        其它平台不出现 ``windows_generic_MSG``，直接透传 Qt 默认。
        """
        if self._is_windows() and bytes(eventType) == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_NCHITTEST:
                    return (True, self._win_hit_test(msg))
                if msg.message == WM_SETCURSOR:
                    if self._win_set_cursor(msg):
                        return (True, 1)
                elif msg.message == WM_GETMINMAXINFO:
                    self._win_apply_minmax(msg)
                    return (True, 0)
                elif msg.message == WM_NCLBUTTONDOWN:
                    if self._win_start_native(int(msg.wParam)):
                        return (True, 0)
                elif msg.message == WM_NCLBUTTONDBLCLK and int(msg.wParam) == HTCAPTION:
                    self._toggle_maximize()
                    return (True, 0)
            except Exception:  # noqa: BLE001 —— 兜底走 Qt 默认，不拖垮事件循环
                pass
        # Qt6 绑定下 super().nativeEvent 对未处理的消息返回 (False, None)；把 None
        # 直接透传回 Qt 时 sip 无法把 None 转成 qintptr 结果参数，抛出的 TypeError
        # 会穿进 C++ 事件分发导致原生访问冲突（进程无 traceback 退出）。必须强转 int。
        ok, res = super().nativeEvent(eventType, message)
        return (ok, res if isinstance(res, int) else 0)

    def _win_hit_test(self, msg) -> int:
        """WM_NCHITTEST：边缘→HT* 缩放；标题栏空白→HTCAPTION 拖动；其余 HTCLIENT。"""
        if self.isMaximized():
            return HTCLIENT  # 最大化时禁止拖动 / 缩放
        local = self._win_local_pos(msg.lParam)
        edges = hit_test(self.rect(), local)
        if edges:
            return _EDGE_TO_HT[edges]
        if self._title_bar_is_draggable(local):
            return HTCAPTION
        return HTCLIENT

    def _title_bar_is_draggable(self, local: QPoint) -> bool:
        """``local``（窗口本地坐标）是否落在标题栏空白区（→ 交给系统拖动）。

        用子控件几何命中（``TitleBar.is_interactive_pos``）而不是
        ``QApplication.widgetAt``：后者是全局查询，在 Win11 / HiDPI / 跨屏缩放
        下可能返回 None 或错位控件，导致按钮区被误判为拖动区、点击被吃掉。
        """
        tb = getattr(self, "title_bar", None)
        if tb is None or tb.isHidden():
            return False
        if not QRect(tb.mapTo(self, QPoint(0, 0)), tb.size()).contains(local):
            return False
        return not tb.is_interactive_pos(tb.mapFrom(self, local))

    def _win_local_pos(self, lparam) -> QPoint:
        """WM_NCHITTEST 的 lParam（屏幕坐标，打包的两个 16 位有符号数）→ 窗口本地逻辑坐标。

        进程 DPI-aware 时 lParam 是**物理**像素，需按 ``devicePixelRatioF`` 换算回
        逻辑像素再交给 Qt 的 ``mapFromGlobal``；DPI-unaware 进程 dpr=1，换算退化为
        恒等。
        """
        lp = int(lparam)
        x = ctypes.c_short(lp & 0xFFFF).value
        y = ctypes.c_short((lp >> 16) & 0xFFFF).value
        ratio = self.devicePixelRatioF()
        if ratio not in (0.0, 1.0):
            x = round(x / ratio)
            y = round(y / ratio)
        return self.mapFromGlobal(QPoint(x, y))

    def _win_set_cursor(self, msg) -> bool:
        """WM_SETCURSOR：缩放区手动 SetCursor，返回 Qt 已处理；其它状态交回 Qt。"""
        cursor_id = resize_cursor_id(int(msg.lParam) & 0xFFFF)
        if cursor_id is None:
            return False
        ctypes.windll.user32.SetCursor(_load_win_cursor(cursor_id))
        return True

    def _win_start_native(self, wparam: int) -> bool:
        """WM_NCLBUTTONDOWN：重发 SC_MOVE（Aero Snap 拖动）/ SC_SIZE（原生缩放）。"""
        if wparam == HTCAPTION:
            self._send_syscommand(SC_MOVE | HTCAPTION)
            return True
        if wparam in _SIZE_HT_CODES:
            self._send_syscommand(sc_size_wparam(wparam))
            return True
        return False

    def _send_syscommand(self, wparam: int) -> None:
        user32 = ctypes.windll.user32
        user32.ReleaseCapture()
        user32.SendMessageW(int(self.winId()), WM_SYSCOMMAND, wparam, 0)

    def _win_apply_minmax(self, msg) -> None:
        """修正最大化几何 + 缩放下限：无边框窗口最大化时填满工作区，不盖住任务栏。

        ``WM_GETMINMAXINFO`` 被本类拦截后不再走 Qt 默认填充，必须在这里手动把
        ``ptMinTrackSize`` 填成 ``setMinimumSize`` 的值 —— 否则原生 SC_SIZE 缩放
        循环会把它当 0，窗口能被拖到任意小（主窗口的 300×500 下限就失效了）。
        """
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
        # 缩放下限：Qt setMinimumSize 是逻辑像素，WM 坐标是物理像素，HiDPI 需按 dpr 换算
        min_w, min_h = self.minimumSize().width(), self.minimumSize().height()
        ratio = self.devicePixelRatioF()
        if ratio not in (0.0, 1.0):
            min_w = round(min_w * ratio)
            min_h = round(min_h * ratio)
        mmi.ptMinTrackSize.x, mmi.ptMinTrackSize.y = min_w, min_h


__all__ = [
    "RESIZE_MARGIN",
    "WINDOW_BORDER_WIDTH",
    "FramelessMixin",
    "hit_test",
    "resize_cursor_id",
    "resized_geometry",
    "sc_size_wparam",
]
