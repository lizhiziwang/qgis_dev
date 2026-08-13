"""主窗口。

旧 :class:`main.QGISMapWindow` 700+ 行，拆成 6 个子组件 + 业务回调：

- :class:`MenuBar`     - 菜单
- :class:`ToolBar`     - 工具条
- :class:`StatusBar`   - 状态栏
- :class:`LayerDock`   - 图层树
- :class:`BrowserDock` - 数据浏览器
- :class:`LogDock`     - 日志面板

业务逻辑（打开文件、空间查询、质检等）通过 callback 注入；窗口本身只负责
"装配 + 事件分发"。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Optional

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt
from PyQt6.QtGui import QAction, QCloseEvent, QColor, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QListWidget,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QSizePolicy,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTreeGroup,
    QgsMapLayerStyle,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.core import QgsStyle
from qgis.gui import (
    QgsLayerTreeMapCanvasBridge,
    QgsMapCanvas,
    QgsMapToolPan,
    QgsMapToolZoom,
    QgsMessageBar,
    QgsRendererPropertiesDialog,
    QgsVectorLayerProperties,
    QgsVectorLayerSaveAsDialog,
)

from app.core.domain.editing import LayerEditService
from app.core.domain.query import QueryType, SpaceQueryService
from app.infra.fs.paths import Paths
from app.ui.common.canvas_zoom import CanvasZoomTool
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, toolbar_qss, window_qss
from app.ui.docks.attribute_dock import AttributeDock
from app.ui.docks.feature_info_dock import FeatureInfoDock
from app.ui.docks.log_dock import LogDock
from app.ui.docks.tools_dock import ToolsDock
from app.ui.main_window.browser_dock import BrowserDock
from app.ui.docks.project_dock import ProjectDock
from app.ui.main_window.layer_dock import LayerDock
from app.ui.main_window.menu_bar import MenuBar
from app.ui.main_window.status_bar import StatusBar
from app.ui.main_window.tool_bar import ToolBar
from app.ui.main_window.draw_toolbar import DigitizingToolbar
from app.ui.main_window.qgis_digitizing_toolbar import QgisDigitizingToolbar

# ---------------------------------------------------------------------------
# Windows 无边框窗口辅助（方案 A）：
# WM_NCHITTEST 让系统接管"标题栏拖动 + 边缘缩放"；WM_GETMINMAXINFO 修正最大化
# 几何；WM_NCLBUTTONDOWN(HTCAPTION) 重发 SC_MOVE 触发系统移动循环以获得 Aero
# Snap 贴边吸附；WM_NCLBUTTONDBLCLK(HTCAPTION) 双击标题栏最大化/还原。
# 仅 Windows 平台会收到 windows_generic_MSG，其他平台这些常量/结构不会被用到。
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


# 让组件行为兼容旧 `component.SpaceQuery` 等可选的 legacy 模块
# （这些模块在阶段 4 后期会从 legacy 重新启用，或被新 service 替换）


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, project_root: Optional[str] = None) -> None:
        super().__init__()
        # 方案 A：无边框窗口，标题栏由 TitleBar 自绘；nativeEvent 里用
        # WM_NCHITTEST / WM_GETMINMAXINFO 让系统接管拖动、缩放与最大化。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._paths = Paths.default()
        if project_root:
            self._paths = Paths(
                project_root=project_root,
                resources=self._paths.resources,
                logs=self._paths.logs,
                tmp=self._paths.tmp,
                config=self._paths.config,
            )
        # 把 project_root 加进 sys.path（兼容旧 component 旧 import）
        if str(self._paths.project_root) not in sys.path:
            sys.path.insert(0, str(self._paths.project_root))

        self.setWindowTitle("qgis_dev")
        self.setGeometry(100, 100, 1200, 800)
        self.setWindowIcon(Icons.app())

        # ---- 项目 + 图层树桥（先建好，_build_central_widget 要用 self.project）----
        self.project = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()

        # ---- 地图画布 ----
        self.canvas = self._build_canvas()
        self.bridge = QgsLayerTreeMapCanvasBridge(self.tree_root, self.canvas)
        self.bridge.setAutoSetupOnFirstLayer(True)
        self._build_central_widget()

        # ---- 画布左上角缩放工具（覆盖层）----
        # self.zoom_tool = CanvasZoomTool(self.canvas)

        # ---- service 注入 ----
        self.editing_service = LayerEditService()
        self.space_query_service = SpaceQueryService()
        self.draw_manager = None
        self.edit_layer: Optional[QgsVectorLayer] = None
        #: 当前选中的图层（图层树点击 / 右键菜单选中）。必须在 __init__ 里初始化，
        #: 否则未选中图层时直接访问会 AttributeError —— 该异常在 Qt 槽内未捕获
        #: 会 native 崩溃（0xC0000409）。
        self._selected_layer: Optional[QgsVectorLayer] = None
        #: 样式剪贴板：复制样式时存 xmlData()，粘贴样式时读（QGIS 图层菜单习惯）
        self._style_clipboard: Optional[str] = None
        #: 流程节点编辑器窗口（懒建单实例，见 _open_flow_editor）
        self._flow_editor_window = None

        # ---- dock ----
        self._build_docks()

        # ---- 自定义标题栏（标题栏 + 菜单栏整体作为顶部 menuWidget）----
        self._build_chrome()

        # ---- 工具 / 菜单 / 状态栏 ----
        self._build_tool_bar()
        self._build_menu_bar()
        self._build_status_bar()

        # ---- 默认激活平移 ----
        self.canvas.setMapTool(self._pan_tool)

    # ===================================================== 装配

    def _build_canvas(self) -> QgsMapCanvas:
        canvas = QgsMapCanvas(self)
        canvas.setAcceptDrops(True)
        # 拦截浏览器拖出的图层 URI，走与双击一致的添加路径（原生 drop 会把图层
        # 直接挂到工程根节点，不进"数据图层"分组、也不设 path_c）
        canvas.installEventFilter(self)
        # 画布底色跟随主题（默认浅色为白色）
        canvas.setCanvasColor(QColor(ThemeManager.instance().current().canvas_bg))
        canvas.enableAntiAliasing(True)
        return canvas

    def _build_central_widget(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # 消息栏
        self.messageBar = QgsMessageBar()
        self.messageBar.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        # 把 messageBar 放到全局属性（兼容旧 sendMessageToTopParent）
        qgis_app = QgsApplication.instance()
        if qgis_app is not None:
            qgis_app.setProperty("messageBar", self.messageBar)
        layout.addWidget(self.messageBar)

        layout.addWidget(self.canvas)

        crs = QgsCoordinateReferenceSystem("EPSG:4490")
        self.project.setCrs(crs)
        self.canvas.setDestinationCrs(crs)
        self.canvas.setProject(self.project)

    def _build_docks(self) -> None:
        # 1. 图层树
        self.layer_dock = LayerDock(self)
        self.layer_dock.context_menu_requested.connect(self._on_layer_context_menu)
        # 点击图层树即自动设为编辑目标（与 QGIS 桌面一致）：
        # 用户期望点完一层就能在工具条点"开始编辑"
        self.layer_dock.layer_clicked.connect(self._on_layer_clicked_in_dock)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.layer_dock)

        # 2. 数据浏览器
        self.browser_dock = BrowserDock(self)
        self.browser_dock.double_clicked.connect(self._on_browser_double_clicked)
        self.browser_dock.context_menu_requested.connect(self._on_browser_context_menu)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.browser_dock)

        # 3. 日志（默认隐藏，由菜单唤起）
        self.log_dock = None  # 懒加载

    def _build_tool_bar(self) -> None:
        callbacks = {
            "pan": self._on_pan,
            "identify": self._on_identify,
            "zoom_in": self._on_zoom_in,
            "zoom_out": self._on_zoom_out,
            "quality": self._on_quality,
            "analyse": self._on_analyse,
            "zoom_full": self._on_zoom_full,
            "refresh": self._on_refresh,
            "test": self._on_test,
        }
        self.tool_bar_builder = ToolBar(self)
        self.tool_bar = self.tool_bar_builder.build(callbacks)

        # 追加绘图 / 编辑工具条（2 个下拉按钮，与旧 component.draw_tool.Draw 一致）
        self.digitizing_toolbar = DigitizingToolbar(
            self,
            self.canvas,
            editing_layer_getter=lambda: self.edit_layer,
        )

        # DigitizingToolbar 编辑态切换 → 同步给 QGIS 复用版工具条
        self.digitizing_toolbar.editing_state_changed.connect(self._on_editing_state_changed)

        # QGIS 复用版数字化工具条（点/线/面/矩形/圆，与自定义版平行）
        # self.qgis_digitizing_toolbar = QgisDigitizingToolbar(
        #     self,
        #     self.canvas,
        #     editing_layer_getter=lambda: self.edit_layer,
        #     on_finished=self._on_qgis_draw_finished,
        # )

        # 创建平移工具（保留引用避免被 GC）
        self._pan_tool = QgsMapToolPan(self.canvas)
        self._zoom_in_tool = QgsMapToolZoom(self.canvas, False)
        self._zoom_out_tool = QgsMapToolZoom(self.canvas, True)

    def _build_digitizing_toolbar(self) -> None:
        """兼容旧接口（占位）：由 :class:`DigitizingToolbar` 直接接管。"""
        return

    def _build_menu_bar(self) -> None:
        callbacks = {
            "open_file": self._on_open_file,
            "open_project": self._on_open_project,
            "new_project": self._on_new_project,
            "show_log": self._show_log_dock,
            "open_flow_editor": self._open_flow_editor,
        }
        self.menu_bar_builder = MenuBar(self, self._menubar)
        self.menu_bar_builder.build(callbacks)

    def _build_status_bar(self) -> None:
        self.status_bar_builder = StatusBar(self)
        self.status_bar_builder.build()

    # ===================================================== 自定义标题栏（方案 A）

    def _build_chrome(self) -> None:
        """顶部 chrome：自绘标题栏 + 菜单栏，整体作为 QMainWindow 菜单区。

        菜单栏用 ``QMenuBar`` 手动创建 —— 不能先 ``self.menuBar()`` 再
        ``setMenuWidget`` 换掉菜单区：实测 Qt 5.15 下该顺序会让随后任何
        ``menuBar()`` 调用段错误（菜单栏已脱离 QMainWindow 布局仍被其持有）。
        所以全程不调 ``menuBar()``，chrome 只做一次 ``setMenuWidget``。
        """
        from app.ui.main_window.title_bar import TitleBar

        self.title_bar = TitleBar(self)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximize)
        self.title_bar.close_requested.connect(self.close)
        self.windowTitleChanged.connect(self.title_bar.set_title)
        self._apply_user_info_to_titlebar()

        self._menubar = QMenuBar(self)
        chrome = QWidget(self)
        v = QVBoxLayout(chrome)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.title_bar)
        v.addWidget(self._menubar)
        self.setMenuWidget(chrome)

        # 主题：跟随 ThemeManager，切换时重绘窗体 chrome + 工具条配色
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._apply_theme_qss()

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _apply_theme_qss(self) -> None:
        """按当前主题重绘窗体 chrome（菜单栏/状态栏/Dock）、主工具条和画布底色。"""
        theme = ThemeManager.instance().current()
        self.setStyleSheet(window_qss(theme))
        tb = getattr(self, "tool_bar", None)
        if tb is not None:
            tb.setStyleSheet(toolbar_qss(theme))
        # 工具条图标是 QPainter 画的，颜色固定烧在 PNG 里，切主题时需重刷
        tbb = getattr(self, "tool_bar_builder", None)
        if tbb is not None:
            tbb.apply_theme(theme)
        # 画布底色跟随主题（setCanvasColor，不走 QSS）
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.setCanvasColor(QColor(theme.canvas_bg))
            canvas.refresh()

    def _on_theme_changed(self, theme) -> None:
        self._apply_theme_qss()

    def _apply_user_info_to_titlebar(self) -> None:
        """把登录用户信息（用户名 + 凭证有效期）挂到标题栏。"""
        from app.core.auth import load_credential

        cred = load_credential()
        if cred is not None:
            self.title_bar.set_user(cred.username, expires_at=cred.expires_at)
        else:
            self.title_bar.set_user("未登录")

    # ------------------------------------------------ Windows 无边框 native 事件

    def nativeEvent(self, eventType, message):
        """拦截 WM_NCHITTEST / WM_GETMINMAXINFO / WM_NCLBUTTONDOWN 等消息，
        让无边框窗口行为接近原生（拖动 + 缩放 + 贴边吸附 + 双击最大化）。"""
        try:
            if bytes(eventType) == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_NCHITTEST:
                    return (True, self._wm_nchittest(msg))
                if msg.message == WM_GETMINMAXINFO:
                    self._wm_getminmaxinfo(msg)
                    return (True, 0)
                if msg.message == WM_NCLBUTTONDOWN and msg.wParam == HTCAPTION:
                    # 无边框窗口下 Qt 把标题栏区当客户区处理，系统原生拖动（含
                    # Aero Snap 贴边吸附）不会启动。ReleaseCapture + 重发
                    # WM_SYSCOMMAND(SC_MOVE|HTCAPTION) 让系统进入原生移动循环，
                    # 从而获得拖到屏幕边缘吸附 / 顶边最大化的能力。
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
        # PyQt6 下 super().nativeEvent 对未处理的消息返回 (False, None)；把这个
        # None 直接透传回 Qt 时，sip 无法把 None 转成 qintptr 结果参数，抛出的
        # TypeError 会穿进 C++ 事件分发，导致原生访问冲突（进程无 traceback 退出）。
        # 必须把结果强转成 int。
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

        if self.isMaximized():
            return HTCLIENT  # 最大化时禁止拖动 / 缩放

        g = self.frameGeometry()
        left, top, right, bottom = g.left(), g.top(), g.right(), g.bottom()
        m = 6
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

        # 标题栏空白区域 → 系统拖动；按钮/用户信息区域交给 Qt 正常处理
        tb = getattr(self, "title_bar", None)
        if tb is not None:
            tb_tl = tb.mapTo(self, QPoint(0, 0))
            if QRect(tb_tl, tb.size()).contains(QPoint(x - left, y - top)):
                w = QApplication.widgetAt(QPoint(x, y))
                if w is not None and (w is tb or tb.isAncestorOf(w)):
                    if not tb.is_interactive_child(w):
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

    def changeEvent(self, event) -> None:
        """最大化/还原状态变化 → 同步标题栏按钮图标。"""
        if event.type() == QEvent.Type.WindowStateChange:
            if hasattr(self, "title_bar"):
                self.title_bar.set_maximized(self.isMaximized())
        super().changeEvent(event)

    # ===================================================== 工具回调

    def _on_pan(self) -> None:
        self.canvas.setMapTool(self._pan_tool)

    def _on_identify(self) -> None:
        from app.ui.tools.find_feature import FindFeatureTool
        self._find_feature_tool = FindFeatureTool(self.canvas, parent=self)
        self.canvas.setMapTool(self._find_feature_tool)

    def show_feature_info(self, result, highlight=None) -> None:
        """点击识别后把要素详情显示到右侧 Dock（懒创建，内容就地刷新）。"""
        if not hasattr(self, "_feature_info_dock") or self._feature_info_dock is None:
            self._feature_info_dock = FeatureInfoDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._feature_info_dock)
        self._feature_info_dock.show_feature(result, highlight=highlight)

    def _on_zoom_in(self) -> None:
        self.canvas.setMapTool(self._zoom_in_tool)

    def _on_zoom_out(self) -> None:
        self.canvas.setMapTool(self._zoom_out_tool)

    def _on_quality(self) -> None:
        from app.ui.dialogs.check_viewer import CheckViewerDialog
        dlg = CheckViewerDialog(self, "")
        dlg.show()

    def _on_analyse(self) -> None:
        # 复用单个"分析工具"dock，避免每次点"分析"都新建一个
        if getattr(self, "_analyse_dock", None) is None:
            self._analyse_dock = ToolsDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._analyse_dock)
        self._analyse_dock.show()
        self._analyse_dock.raise_()

    def _on_zoom_full(self) -> None:
        layers = [l for l in self.project.mapLayers().values() if l.isValid()]
        if not layers:
            QMessageBox.information(self, "提示", "当前无有效图层")
            return
        combined = layers[0].extent()
        for l in layers[1:]:
            combined.combineExtentWith(l.extent())
        self.canvas.setExtent(combined)
        self.canvas.refresh()

    def _on_refresh(self) -> None:
        self.canvas.refresh()

    def _on_test(self) -> None:
        """测试按钮：启动独立 FastAPI 服务（在新进程中）。"""
        try:
            from app.server.cli import main as server_main
            import threading
            threading.Thread(target=server_main, daemon=True).start()
            self.messageBar.pushMessage("已启动服务（8000）", level=Qgis.MessageLevel.Info, duration=3)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _on_new_project(self) -> None:
        """新建工程：先弹窗输入工程名，创建后把名字带到右侧工程 Dock 显示。"""
        # 已有图层时先确认清空（避免输入完名字又取消）
        if self.project.mapLayers():
            ret = QMessageBox.question(
                self,
                "新建工程",
                "将清空当前所有图层并新建工程，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        name = self._ask_project_name()
        if name is None:
            return
        # 确保 Dock 存在
        if getattr(self, "project_dock", None) is None:
            self.project_dock = ProjectDock(self)
            self.project_dock.render_requested.connect(self._show_style_editor)
            self.project_dock.label_requested.connect(self._show_label_settings)
            self.project_dock.layer_context_menu_requested.connect(
                self._on_project_dock_context_menu
            )
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.project_dock)
        self._on_project_create(name)
        self.project_dock.show()
        self.project_dock.raise_()

    def _ask_project_name(self):
        """弹窗输入工程名；返回工程名，取消或为空则返回 ``None``。"""
        from PyQt6.QtWidgets import QInputDialog

        while True:
            name, ok = QInputDialog.getText(self, "新建工程", "请输入工程名：")
            if not ok:
                return None
            name = name.strip()
            if name:
                return name
            QMessageBox.warning(self, "提示", "工程名不能为空")

    def _on_project_create(self, name: str) -> None:
        """真正创建工程：清空图层、建两个分组、设工程名（Dock 展示名字）。"""
        # 退出编辑态，避免遗留脏 buffer
        if self.edit_layer is not None:
            if self.edit_layer.isEditable():
                self.editing_service.stop(self.edit_layer, save=False)
            self.edit_layer = None
            if hasattr(self, "digitizing_toolbar"):
                self.digitizing_toolbar.set_edit_target(None)
        self.project.clear()

        # 重建分组：数据图层 + 参考图层
        root = self.project.layerTreeRoot()
        self._data_group = root.addGroup("数据图层")
        self._ref_group = root.addGroup("参考图层")

        # 工程名 + 坐标系（project.clear() 会把 CRS 重置回默认）
        self.project.setTitle(name)
        self.setWindowTitle(f"qgis_dev - {name}")
        self.project.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))

        # 底图进参考分组
        self._load_config_base_tile_into_ref_group()

        self.project_dock.set_project(name, self._data_group, self._ref_group)
        self.canvas.refreshAllLayers()
        self.messageBar.pushMessage(f"已创建工程：{name}", level=Qgis.MessageLevel.Info, duration=3)

    def _add_layer_to_project(self, layer) -> None:
        """把图层加进项目：默认路由到"数据图层"分组（未创建工程时平铺挂根）。

        用 ``insertLayer(0, ...)`` 把新图层插到分组最顶层——QGIS 面板顶部即渲染
        最上层，后加的图层直接盖在最上面。
        """
        self.project.addMapLayer(layer, False)
        group = getattr(self, "_data_group", None) or self.project.layerTreeRoot()
        group.insertLayer(0, layer)

    def _load_config_base_tile_into_ref_group(self) -> None:
        """加载配置底图并移入"参考图层"分组（底图已平铺挂根，需移动）。"""
        layer = self._load_config_base_tile()
        if layer is not None and getattr(self, "_ref_group", None) is not None:
            self._move_layer_to_group(layer, self._ref_group)

    def _on_open_project(self) -> None:
        """打开工程：读入 ``.qgs`` / ``.qgz`` 工程文件并刷新画布。"""
        from app.ui.common.widgets import open_file_dir

        path = open_file_dir(
            self,
            is_dir=False,
            title="打开工程",
            filter_="QGIS 工程 (*.qgs *.qgz);;所有文件 (*)",
        )
        if not path:
            return
        if self.project.read(path):
            self.canvas.setDestinationCrs(self.project.crs())
            self.canvas.refreshAllLayers()
            self.setWindowTitle(f"qgis_dev - {self.project.title()}")
            # 打开的工程里若带"数据图层/参考图层"分组则重绑 Dock，否则复位
            # （只匹配真正的分组，避免把同名图层节点误当分组）
            self._data_group = None
            self._ref_group = None
            for child in self.project.layerTreeRoot().children():
                if not isinstance(child, QgsLayerTreeGroup):
                    continue
                if child.name() == "数据图层":
                    self._data_group = child
                elif child.name() == "参考图层":
                    self._ref_group = child
            if getattr(self, "project_dock", None) is not None:
                if self._data_group is not None and self._ref_group is not None:
                    self.project_dock.set_project(
                        self.project.title(), self._data_group, self._ref_group
                    )
                else:
                    self.project_dock.reset()
            self.messageBar.pushMessage(f"已打开工程：{path}", level=Qgis.MessageLevel.Info, duration=3)
        else:
            QMessageBox.warning(self, "打开失败", f"无法打开工程：{path}")

    def _load_config_base_tile(self):
        """读取配置并加载默认底图（新建工程时恢复天地图底图）。返回图层或 ``None``。"""
        import os
        from pathlib import Path

        try:
            from app.core.domain.basemap import add_base_tile_layer
            from app.env.config import AppConfig
            from app.infra.fs.paths import Paths

            cfg_path = Path(
                os.environ.get("QGIS_DEV_CONFIG")
                or Paths.default().config_file("app.yaml")
            )
            cfg = AppConfig.from_file(cfg_path)
            layer = add_base_tile_layer(cfg.map, project=self.project)
            if layer is not None:
                self.messageBar.pushMessage(
                    f"已加载底图：{layer.name()}", level=Qgis.MessageLevel.Info, duration=2
                )
            return layer
        except Exception as exc:  # noqa: BLE001
            self.messageBar.pushMessage(f"底图加载失败：{exc}", level=Qgis.MessageLevel.Critical, duration=5)
            return None

    def _on_open_file(self) -> None:
        from app.ui.common.widgets import open_file_dir
        path = open_file_dir(
            self,
            is_dir=False,
            title="选择矢量文件",
            filter_="矢量文件 (*.shp *.geojson *.gpkg *.gdb);;所有文件 (*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, file_path: str) -> None:
        """加载文件到项目（支持 shp / gdb / geojson / gpkg）。"""
        from osgeo import ogr
        from qgis.core import QgsVectorLayer

        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".gdb":
                ogr.RegisterAll()
                ds = ogr.Open(file_path)
                if not ds:
                    QMessageBox.warning(self, "失败", f"无法打开 GDB: {file_path}")
                    return
                for i in range(ds.GetLayerCount()):
                    fc = ds.GetLayerByIndex(i)
                    name = fc.GetName()
                    source = f"{file_path}|layername={name}"
                    layer = QgsVectorLayer(source, name, "ogr")
                    layer.path_c = file_path
                    if layer.isValid():
                        self._add_layer_to_project(layer)
                        self.canvas.setExtent(layer.extent())
                ds.Destroy()
            else:
                name = os.path.basename(file_path)
                layer = QgsVectorLayer(file_path, name, "ogr")
                layer.path_c = file_path
                self._add_layer_to_project(layer)
                self.canvas.setExtent(layer.extent())
            self.canvas.refreshAllLayers()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "加载错误", f"文件加载失败：{e}")

    # ===================================================== 浏览器事件

    def _on_browser_double_clicked(self, index) -> None:
        from qgis.core import QgsLayerItem, QgsVectorLayer, QgsRasterLayer

        if not index.isValid():
            return
        item = self.browser_dock.model.dataItem(index)
        if not item or not isinstance(item, QgsLayerItem):
            return
        uri, name, provider = item.uri(), item.name(), item.providerKey()
        vlayer = QgsVectorLayer(uri, name, provider)
        if vlayer.isValid():
            vlayer.path_c = item.path()
            self._add_layer_to_project(vlayer)
            return
        rlayer = QgsRasterLayer(uri, name, provider)
        if rlayer.isValid():
            rlayer.path_c = item.path()
            self._add_layer_to_project(rlayer)

    def _on_browser_context_menu(self, pos) -> None:
        from PyQt6.QtWidgets import QMenu

        index = self.browser_dock.view.indexAt(pos)
        if not index.isValid():
            return
        item = self.browser_dock.model.dataItem(index)
        if not item:
            return
        menu = QMenu(self)
        # QGIS 3.40 起 QgsDataItemGuiContext 持有的是浏览器 view + 消息栏
        # （没有 setProviderRegistry，调它会 AttributeError → Qt 槽内未捕获 → 崩溃）。
        # 包一层防御：任何异常只回退到"刷新"，不让菜单构建失败拖垮应用。
        try:
            from qgis.gui import QgsDataItemGuiContext, QgsGui

            registry = QgsGui.instance().dataItemGuiProviderRegistry()
            context = QgsDataItemGuiContext()
            context.setView(self.browser_dock.view)
            context.setMessageBar(self.messageBar)
            for provider in registry.providers():
                try:
                    provider.populateContextMenu(
                        item, menu, selectedItems=[item], context=context
                    )
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            from app.infra.logging import get_logger

            get_logger(__name__).exception("构建数据浏览器右键菜单失败，仅保留「刷新」")
        refresh = menu.addAction("刷新")
        refresh.triggered.connect(lambda: item.refresh())
        menu.exec(self.browser_dock.view.viewport().mapToGlobal(pos))

    # ---- 画布拖拽添加图层（数据浏览器拖出 → 画布 drop）----

    def eventFilter(self, watched, event) -> bool:
        """画布拖拽拦截：把浏览器拖出的图层 URI 转给 :meth:`_on_canvas_drop`。

        原生 :class:`QgsMapCanvas` 的 drop 会把图层直接加进工程根节点，不进
        "数据图层"分组、也不设 ``path_c``，与双击行为不一致；这里拦下来复用
        同一套 ``_add_layer_to_project`` 路径。非图层 URI 的拖拽（如外部文件）
        放行给原生处理。
        """
        if getattr(self, "canvas", None) is watched and event.type() in (
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
        ):
            from qgis.core import QgsMimeDataUtils

            if QgsMimeDataUtils.isUriList(event.mimeData()):
                if event.type() == QEvent.Type.Drop:
                    self._on_canvas_drop(event)
                else:
                    event.acceptProposedAction()
                return True
        return super().eventFilter(watched, event)

    def _on_canvas_drop(self, event) -> None:
        """解析 drop 的图层 URI 列表，逐个按类型添加（矢量/栅格）。

        与 :meth:`_on_browser_double_clicked` 一致：provider 为 ``ogr`` 时把
        URI 存到 ``path_c``（供"执行SQL"按文件路径打开），再走
        ``_add_layer_to_project`` 挂进"数据图层"分组。
        """
        from qgis.core import QgsMimeDataUtils, QgsRasterLayer, QgsVectorLayer

        for u in QgsMimeDataUtils.decodeUriList(event.mimeData()):
            if u.layerType == "vector":
                layer = QgsVectorLayer(u.uri, u.name, u.providerKey)
            elif u.layerType == "raster":
                layer = QgsRasterLayer(u.uri, u.name, u.providerKey)
            else:
                continue
            if not layer.isValid():
                continue
            if u.providerKey == "ogr":
                layer.path_c = u.uri
            self._add_layer_to_project(layer)
        event.acceptProposedAction()

    # ===================================================== 图层右键菜单

    def _on_layer_context_menu(self, pos: QPoint) -> None:
        """图层树右键 → 交给共享的菜单构建器。"""
        view = self.layer_dock.view
        index = view.indexAt(pos)
        if not index.isValid():
            return
        model = index.model()
        source_index = model.mapToSource(index)
        source_model = source_index.model()
        layer_name = source_model.data(source_index, Qt.ItemDataRole.DisplayRole)
        if not layer_name:
            return
        layers = self.project.mapLayersByName(layer_name)
        if not layers:
            return
        self._build_layer_context_menu(layers[0], view.mapToGlobal(pos))

    def _on_project_dock_context_menu(self, layer, global_pos: QPoint) -> None:
        """工程管理 Dock 的图层右键 → 同一套 QGIS 式图层菜单。"""
        self._build_layer_context_menu(layer, global_pos)

    def _build_layer_context_menu(self, layer, global_pos: QPoint) -> None:
        """QGIS 式图层右键菜单（图层树与工程 Dock 共用）。

        菜单项统一作用在 ``self._selected_layer`` 上；各动作方法（缩放/设编辑/
        移分组/属性表/样式/移除/导出/属性）读的都是这个字段，所以进入时先
        把它指向当前右键的图层。

        布局贴近 QGIS 原生图层面板：缩放至图层 / 放大至选中要素 → 设为编辑图层
        → 打开属性表 / 执行SQL（仅矢量）→ 重命名 / 复制图层 → 样式（含
        复制/粘贴样式）→ 移到分组 → 移除 / 导出 / 属性。
        """
        if layer is None:
            return
        self._selected_layer = layer
        is_vector = isinstance(layer, QgsVectorLayer)
        menu = QMenu(self)
        # 缩放至图层
        act_zoom = QAction("缩放至图层", self)
        act_zoom.triggered.connect(lambda: self._zoom_to_layer(self._selected_layer))
        menu.addAction(act_zoom)
        # 放大至选中要素（有选中才显示，与 QGIS 一致）
        if is_vector and layer.selectedFeatureCount() > 0:
            act_sel = QAction("放大至选中要素", self)
            act_sel.triggered.connect(lambda: self._zoom_to_selection(self._selected_layer))
            menu.addAction(act_sel)
        # 设编辑
        act_edit = QAction("设为编辑图层", self)
        act_edit.triggered.connect(self._set_edit_layer)
        menu.addAction(act_edit)
        # 属性表 / SQL（栅格没有要素/表，不显示）
        if is_vector:
            menu.addSeparator()
            act_attr = QAction("打开属性表", self)
            act_attr.triggered.connect(self._open_attribute_table)
            menu.addAction(act_attr)
            act_sql = QAction("执行SQL", self)
            act_sql.triggered.connect(self._open_execute_sql)
            menu.addAction(act_sql)
        # 重命名 / 复制图层
        menu.addSeparator()
        act_rename = QAction("重命名", self)
        act_rename.triggered.connect(lambda: self._rename_layer(self._selected_layer))
        menu.addAction(act_rename)
        act_dup = QAction("复制图层", self)
        act_dup.triggered.connect(lambda: self._duplicate_layer(self._selected_layer))
        menu.addAction(act_dup)
        # 样式
        sub_style = menu.addMenu("样式")
        if is_vector:
            edit_style = QAction("编辑样式", self)
            edit_style.triggered.connect(lambda: self._show_style_editor(self._selected_layer))
            sub_style.addAction(edit_style)
        open_style = QAction("打开样式配置", self)
        open_style.triggered.connect(lambda: self._open_style_file(self._selected_layer))
        sub_style.addAction(open_style)
        save_style = QAction("保存样式", self)
        save_style.triggered.connect(lambda: self._save_style(self._selected_layer))
        sub_style.addAction(save_style)
        sub_style.addSeparator()
        copy_style = QAction("复制样式", self)
        copy_style.triggered.connect(lambda: self._copy_layer_style(self._selected_layer))
        sub_style.addAction(copy_style)
        paste_style = QAction("粘贴样式", self)
        paste_style.setEnabled(bool(getattr(self, "_style_clipboard", None)))
        paste_style.triggered.connect(lambda: self._paste_layer_style(self._selected_layer))
        sub_style.addAction(paste_style)
        # 工程分组：移动图层归属（创建工程后才显示）
        if (
            getattr(self, "_data_group", None) is not None
            and getattr(self, "_ref_group", None) is not None
        ):
            menu.addSeparator()
            act_to_ref = QAction("移到参考图层", self)
            act_to_ref.triggered.connect(
                lambda: self._move_layer_to_group(self._selected_layer, self._ref_group)
            )
            menu.addAction(act_to_ref)
            act_to_data = QAction("移到数据图层", self)
            act_to_data.triggered.connect(
                lambda: self._move_layer_to_group(self._selected_layer, self._data_group)
            )
            menu.addAction(act_to_data)
        # 移除 / 导出 / 属性
        menu.addSeparator()
        act_remove = QAction("移除", self)
        act_remove.setIcon(Icons.remove())
        act_remove.triggered.connect(lambda: self._remove_layer(self._selected_layer))
        menu.addAction(act_remove)
        if is_vector:
            # 导出走 QgsVectorLayerSaveAsDialog，仅矢量
            act_export = QAction("导出", self)
            act_export.triggered.connect(lambda: self._export_layer(self._selected_layer))
            menu.addAction(act_export)
        act_props = QAction("属性", self)
        act_props.triggered.connect(lambda: self._open_layer_properties(self._selected_layer))
        menu.addAction(act_props)
        menu.exec(global_pos)

    def _zoom_to_selection(self, layer) -> None:
        """放大至选中要素（与 QGIS 图层面板一致）。"""
        if layer is None or layer.selectedFeatureCount() <= 0:
            return
        ext = QgsRectangle()
        for feat in layer.selectedFeatures():
            ext.combineExtentWith(feat.geometry().boundingBox())
        if ext.isNull():
            return
        # 单点/零面积（点的 bbox isEmpty() 为真，但几何本身有效）：先扩一圈
        # 再设置，避免 setExtent 拿到空范围
        if ext.width() <= 0 or ext.height() <= 0:
            center = ext.center()
            half = 0.001
            ext = QgsRectangle(
                center.x() - half, center.y() - half,
                center.x() + half, center.y() + half,
            )
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if layer.crs() != dest_crs:
            xform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
            ext = xform.transformBoundingBox(ext)
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    def _rename_layer(self, layer) -> None:
        """重命名图层：layer.setName 会同步树节点名（已实测）。"""
        if layer is None:
            return
        from PyQt6.QtWidgets import QInputDialog

        new_name, ok = QInputDialog.getText(self, "重命名图层", "图层名称：", text=layer.name())
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == layer.name():
            return
        layer.setName(new_name)
        self.canvas.refresh()
        self.messageBar.pushMessage(f"已重命名为：{new_name}", level=Qgis.MessageLevel.Info, duration=2)

    def _duplicate_layer(self, layer) -> None:
        """复制图层（与 QGIS 图层菜单一致）。

        - 矢量：内存副本（同一 provider 几何/字段/要素），挂到原图层所在分组
        - 栅格：直接指向同一数据源的新图层
        副本默认带上原图层的当前样式。
        """
        if layer is None:
            return
        if isinstance(layer, QgsVectorLayer):
            geom = QgsWkbTypes.displayString(layer.wkbType())
            copy = QgsVectorLayer(
                f"{geom}?crs={layer.crs().authid()}", f"{layer.name()}_副本", "memory"
            )
            dp = copy.dataProvider()
            dp.addAttributes(layer.fields().toList())
            copy.updateFields()
            dp.addFeatures(list(layer.getFeatures()))
            copy.updateExtents()
        elif isinstance(layer, QgsRasterLayer):
            copy = QgsRasterLayer(layer.source(), f"{layer.name()}_副本")
            if not copy.isValid():
                QMessageBox.warning(self, "复制图层", "无法复制该栅格图层")
                return
        else:
            return
        # 复制当前样式
        try:
            sm = layer.styleManager()
            copy_sm = copy.styleManager()
            copy_sm.addStyle("copied", sm.style(sm.currentStyle()))
            copy_sm.setCurrentStyle("copied")
        except Exception:  # noqa: BLE001
            pass
        # 加到原图层所在分组（找不到就挂到树根，走 _add_layer_to_project）
        node = self.project.layerTreeRoot().findLayer(layer)
        group = node.parent() if node is not None else None
        self.project.addMapLayer(copy, False)
        if group is not None:
            group.addLayer(copy)
        else:
            self._add_layer_to_project(copy)
        self.canvas.refresh()
        self.messageBar.pushMessage(f"已复制图层：{copy.name()}", level=Qgis.MessageLevel.Info, duration=2)

    def _copy_layer_style(self, layer) -> None:
        """复制当前样式（xml 存到剪贴板，粘贴样式时用）。"""
        if layer is None:
            return
        sm = layer.styleManager()
        self._style_clipboard = sm.style(sm.currentStyle()).xmlData()
        self.messageBar.pushMessage(f"已复制 {layer.name()} 的样式", level=Qgis.MessageLevel.Info, duration=2)

    def _paste_layer_style(self, layer) -> None:
        """把复制的样式贴到目标图层。"""
        if layer is None:
            return
        xml = getattr(self, "_style_clipboard", None)
        if not xml:
            QMessageBox.information(self, "粘贴样式", "请先「复制样式」")
            return
        sm = layer.styleManager()
        sm.addStyle("pasted", QgsMapLayerStyle(xml))
        sm.setCurrentStyle("pasted")
        layer.triggerRepaint()
        self.canvas.refresh()
        self.messageBar.pushMessage(f"已粘贴样式到 {layer.name()}", level=Qgis.MessageLevel.Info, duration=2)

    def _zoom_to_layer(self, layer) -> None:
        if layer is None:
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if layer.crs() != dest_crs:
            xform = QgsCoordinateTransform(layer.crs(), dest_crs, self.project)
            ext = xform.transformBoundingBox(layer.extent())
        else:
            ext = layer.extent()
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    def zoom_to_layer_extent_transform(self, extent, source_crs) -> None:
        """缩放到指定范围（属性表右键"缩放到要素"用），做 CRS 变换到画布坐标系。"""
        if extent is None or extent.isNull():
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if source_crs is not None and source_crs.isValid() and source_crs != dest_crs:
            xform = QgsCoordinateTransform(source_crs, dest_crs, self.project)
            extent = xform.transformBoundingBox(extent)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def _move_layer_to_group(self, layer, group) -> None:
        """把图层节点移动到指定分组。

        不能只 ``takeChild`` 再 ``addLayer``：`QgsLayerTreeRegistryBridge`（由
        ``QgsProject`` 自动创建、负责树与图层注册表同步）会在图层**在树里没有任何
        节点**时把图层从工程删掉——事件循环一跑图层就没了，"移动"看起来没生效。
        所以先 `group.addLayer` 在目标组挂新节点（图层始终在树里），再摘原节点。
        """
        if layer is None or group is None:
            return
        node = self.project.layerTreeRoot().findLayer(layer)
        if node is not None and node.parent() is not group:
            group.addLayer(layer)          # 先挂新节点，保证图层仍在树里
            node.parent().takeChild(node)  # 再摘原节点（bridge 不会删图层）
            self.messageBar.pushMessage(
                f"已把 {layer.name()} 移到「{group.name()}」", level=Qgis.MessageLevel.Info, duration=2
            )

    def _remove_layer(self, layer) -> None:
        """移除图层：清引用 + 刷新画布。

        ``project.removeMapLayer`` 只会把图层从 project 树摘掉，
        画布仍保留最后一次 render 缓存 → 必须显式 ``refresh()`` 否则看到
        残留图形。

        关键时序：``canvas.refresh()`` 不能在 ``removeMapLayer`` 返回后立刻
        调用——我们还在 ``QAction.triggered`` 信号栈里，QGIS 这时尝试
        render 会撞上半移除状态的 layer，导致 0xC0000409。
        用 :class:`QTimer.singleShot` 推到下一个事件循环 tick，等
        ``layersRemoved`` 信号链彻底走完再画。
        """
        from PyQt6.QtCore import QTimer

        if layer is None:
            return
        layer_id = layer.id()
        layer_name = layer.name()
        # 如果该图层正在编辑，先退出（避免脏 buffer 滞留）
        was_editing = layer.isEditable()
        if self.edit_layer is layer and was_editing:
            self.editing_service.stop(layer, save=False)
        # 清空本地引用（在 removeMapLayer 之前，避免后续回调引用悬空 layer）
        if self._selected_layer is layer:
            self._selected_layer = None
        if self.edit_layer is layer:
            self.edit_layer = None
            # 工具条也回滚到"无编辑图层"态
            if hasattr(self, "digitizing_toolbar"):
                try:
                    self.digitizing_toolbar.set_edit_target(None)
                except Exception:  # noqa: BLE001
                    pass
            qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
            if qgis_tb is not None:
                try:
                    qgis_tb.disable_all_buttons()
                except Exception:  # noqa: BLE001
                    pass

        # 真正从 project 摘掉
        self.project.removeMapLayer(layer_id)
        # 延迟刷新画布到下一个事件循环 tick，让 layersRemoved 信号链走完
        QTimer.singleShot(0, self._deferred_refresh_after_remove)
        self.messageBar.pushMessage(f"已移除图层：{layer_name}", level=Qgis.MessageLevel.Info, duration=3)

    def _deferred_refresh_after_remove(self) -> None:
        """延迟到事件循环下一拍才刷新画布（避免半移除状态 native 崩溃）。"""
        try:
            self.canvas.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _set_edit_layer(self) -> None:
        """仅"指定编辑图层"：不真正进入编辑态，让用户点工具条「开始编辑」。

        与工具条上的 :meth:`DigitizingToolbar.start_edit_fun` 拆分职责：

        - 本方法负责：记录 ``self.edit_layer``、刷新"开始编辑"按钮
        - 工具条的"开始编辑"才真正 ``layer.startEditing()``
        """
        if self._selected_layer and isinstance(self._selected_layer, QgsVectorLayer):
            # 如果之前有别的图层在编辑，先停掉（避免遗留脏 buffer）
            prev = self.edit_layer
            if prev is not None and prev is not self._selected_layer and prev.isEditable():
                self.editing_service.stop(prev, save=False)

            self.edit_layer = self._selected_layer
            # 切到"已指定编辑图层"态：开始编辑可用、停止/保存禁用、按图层类型启用添加要素
            if hasattr(self, "digitizing_toolbar"):
                self.digitizing_toolbar.set_edit_target(self.edit_layer)
            # QGIS 复用版按钮：进编辑态时才启用（与 drawing_state 联动）
            qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
            if qgis_tb is not None:
                qgis_tb.set_layer(self.edit_layer)  # 仅记录图层，不开按钮
            self.messageBar.pushMessage(f"已设 {self._selected_layer.name()} 为编辑图层，点击「开始编辑」生效", level=Qgis.MessageLevel.Info, duration=3)

    def _on_layer_clicked_in_dock(self, index) -> None:
        """用户在图层树点击一层：自动同步 ``_selected_layer`` / ``edit_layer``。

        这样点击任意矢量图层后，工具条上的"开始编辑"立即可用，
        不必再走右键 → "设为编辑图层"。
        """
        try:
            node = self.layer_dock.view.indexToNode(index)
        except Exception:  # noqa: BLE001
            return
        if node is None:
            return
        # node 可能是 group / layer / legend node；只要带 ``layer()`` 方法的就拿
        layer_getter = getattr(node, "layer", None)
        if not callable(layer_getter):
            return
        layer = layer_getter()
        if layer is None:
            return

        self._selected_layer = layer
        # 非矢量图层（栅格 / mesh 等）也只做选择，不进入编辑目标
        if not isinstance(layer, QgsVectorLayer):
            return
        # 如果旧 edit_layer 还在 editing，先退出
        prev = self.edit_layer
        if prev is not None and prev is not layer and prev.isEditable():
            self.editing_service.stop(prev, save=False)

        self.edit_layer = layer
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.set_edit_target(layer)
        qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
        if qgis_tb is not None:
            qgis_tb.set_layer(layer)

    # ---------------------------------------------------- 绘图工具回调（保留兼容）

    def _on_draw_finished(self, geometry) -> None:
        """DrawManager.finished 信号：跳到 DigitizingToolbar.finish_draw。"""
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.finish_draw(geometry)

    def _on_qgis_draw_finished(self, geometry) -> None:
        """QgisDigitizingToolbar 走的是独立的 DrawManager，finished 信号也跳到同一处。"""
        if getattr(self, "digitizing_toolbar", None) is not None:
            self.digitizing_toolbar.finish_draw(geometry)

    def _on_editing_state_changed(self, editing: bool) -> None:
        """DigitizingToolbar 的开始/停止编辑切换 → 同步给 QGIS 复用版工具条。"""
        qgis_tb = getattr(self, "qgis_digitizing_toolbar", None)
        if qgis_tb is None:
            return
        if editing:
            qgis_tb.enable_buttons_for_layer(self.edit_layer)
        else:
            qgis_tb.disable_all_buttons()

    def _open_attribute_table(self) -> None:
        if self._selected_layer is None:
            return
        self._show_attribute_dock(self._selected_layer)

    def _open_execute_sql(self) -> None:
        from app.ui.dialogs.execute_sql import ExecuteSqlDialog
        if self._selected_layer is None:
            return
        dlg = ExecuteSqlDialog(self._selected_layer, self)
        dlg.show()

    def _show_style_editor(self, layer) -> None:
        dlg = QgsRendererPropertiesDialog(layer=layer, style=QgsStyle.defaultStyle(), embedded=False, parent=self)
        dlg.setWindowTitle(f"图层样式 - {layer.name()}")
        if dlg.exec():
            layer.triggerRepaint()
            self.canvas.refresh()

    def _show_label_settings(self, layer) -> None:
        """打开标注设置。

        本 QGIS 构建没有绑定独立的 ``QgsLabelingWidget``，改用图层属性对话框
        并尽力跳到"标注"页（失败则停留在默认页，用户可手动点"标注"）。
        """
        if not isinstance(layer, QgsVectorLayer):
            return
        try:
            from qgis._gui import QgsVectorLayerProperties

            dlg = QgsVectorLayerProperties(
                canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self
            )
            dlg.setWindowIcon(Icons.app())
            dlg.setWindowTitle(f"标注设置 - {layer.name()}")
            self._select_properties_page(dlg, ("标注", "Labels"))
            dlg.exec()
            layer.triggerRepaint()
            self.canvas.refresh()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _select_properties_page(self, dlg, texts: tuple) -> None:
        """尽力跳到图层属性对话框的指定页（按左侧选项列表项文本匹配，失败则忽略）。"""
        try:
            for w in dlg.findChildren(QListWidget):
                for i in range(w.count()):
                    item = w.item(i)
                    if item and item.text() in texts:
                        w.setCurrentItem(item)
                        return
            for w in dlg.findChildren(QTreeWidget):
                root = w.invisibleRootItem()
                for i in range(root.childCount()):
                    item = root.child(i)
                    if item and item.text(0) in texts:
                        w.setCurrentItem(item)
                        return
        except Exception:  # noqa: BLE001
            pass

    def _open_style_file(self, layer) -> None:
        from app.ui.dialogs.style_io import StyleOpenDialog
        dlg = StyleOpenDialog(layer, self)
        dlg.show()

    def _save_style(self, layer) -> None:
        from app.ui.dialogs.style_io import StyleSaveDialog
        dlg = StyleSaveDialog(layer, self)
        dlg.exec()

    def _export_layer(self, layer) -> None:
        dlg = QgsVectorLayerSaveAsDialog(layer)
        for btn in dlg.findChildren(QAction):
            t = btn.text().lower()
            if t in ("ok", "save", "确定", "保存"):
                btn.setText("确定")
            elif t in ("cancel", "取消"):
                btn.setText("取消")
            elif t in ("help", "帮助"):
                btn.hide()
        dlg.exec()

    def _open_layer_properties(self, layer) -> None:
        try:
            from qgis._gui import QgsVectorLayerProperties
            dlg = QgsVectorLayerProperties(canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self)
            dlg.setWindowIcon(Icons.app())
            dlg.exec()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def _show_log_dock(self) -> None:
        if self.log_dock is None:
            self.log_dock = LogDock(self)
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.show()
        self.log_dock.raise_()

    def _open_flow_editor(self) -> None:
        """打开流程节点编辑器（懒建单实例，重复调用只置前）。"""
        try:
            if self._flow_editor_window is None:
                from app.ui.flow_editor import FlowEditorWindow

                self._flow_editor_window = FlowEditorWindow()
            win = self._flow_editor_window
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception as exc:  # noqa: BLE001
            # 槽内异常会 native 崩溃，务必兜住（见 qgis-ui-integration-gotchas）
            from app.infra.logging import get_logger

            get_logger(__name__).error("打开流程编辑器失败：%s", exc, exc_info=True)
            if hasattr(self, "messageBar"):
                self.messageBar.pushWarning("流程编辑器", f"打开失败：{exc}")


    def _show_attribute_dock(self, layer, filter_str: str = "1=1", show_ids: list = None) -> None:
        if not hasattr(self, "current_attribute_dock") or self.current_attribute_dock is None:
            self.current_attribute_dock = AttributeDock(layer, page_size=50, parent=self, filter_str=filter_str, show_ids=show_ids)
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.current_attribute_dock)
            self.current_attribute_dock.show()
        else:
            self.current_attribute_dock.add_new_layer(layer, self, filter_str, show_ids, 50, f"属性表 - {layer.name()}")

    # ===================================================== 关闭

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.project.clear()
        finally:
            event.accept()
