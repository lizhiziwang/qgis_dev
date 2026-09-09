"""主窗口。

旧 :class:`main.QGISMapWindow` 700+ 行，拆成 6 个子组件 + 业务回调：

- :class:`ToolBar`      - 工具条（顶部 chrome 用 Tab 切换器切换各功能工具条）
- :class:`StatusBar`    - 状态栏
- :class:`LayerDock`    - 图层树
- :class:`BrowserDock`  - 数据浏览器
- :class:`LogDock`      - 日志面板

业务逻辑（打开文件、空间查询、质检等）通过 callback 注入；窗口本身只负责
"装配 + 事件分发"。
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from typing import Optional

from qgis.PyQt.QtCore import QEvent, QPoint, Qt
from qgis.PyQt.QtGui import QAction, QCloseEvent, QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QShortcut,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsLayerTreeGroup,
    QgsMapLayerStyle,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsStyle,
    QgsVectorDataProvider,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import (
    QgsLayerTreeMapCanvasBridge,
    QgsMapCanvas,
    QgsMapToolPan,
    QgsMapToolZoom,
    QgsRendererPropertiesDialog,
    QgsVectorLayerSaveAsDialog,
)

from app.core.domain.check.space_index import clear_spatial_index
from app.core.domain.editing import LayerEditService
from app.core.domain.query import SpaceQueryService
from app.core.util.local_layer_loader import (
    LocalLayerError,
    LocalLayerLoader,
    LocalLayerUnsupportedCategoryError,
)
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger
from app.infra.messagebar import FloatingMessageBar
from app.ui.common.frameless import FramelessMixin
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, toolbar_qss, window_qss
from app.ui.docks.attribute_dock import AttributeDock
from app.ui.docks.feature_info_dock import FeatureInfoDock
from app.ui.docks.log_dock import LogDock
from app.ui.docks.project_dock import ProjectDock
from app.ui.docks.tools_dock import ToolsDock
from app.ui.main_window.browser_dock import BrowserDock
from app.ui.main_window.draw_toolbar import DigitizingToolbar
from app.ui.main_window.snapping_toolbar import QgisSnappingToolbar
from app.ui.main_window.layer_dock import LayerDock
from app.ui.main_window.side_toolbar import SideToolBar
from app.ui.main_window.status_bar import StatusBar
from app.ui.main_window.tool_bar import ToolBar

logger = get_logger(__name__)

# 让组件行为兼容旧 `component.SpaceQuery` 等可选的 legacy 模块
# （这些模块在阶段 4 后期会从 legacy 重新启用，或被新 service 替换）

#: 浏览器目录右键"新建"子菜单支持的矢量格式：(显示名, 扩展名, OGR 驱动)
_NEW_VECTOR_FORMATS = (
    ("Shapefile 文件（点/线/面）", "shp", "ESRI Shapefile"),
    ("GeoPackage 数据库", "gpkg", "GPKG"),
    ("File Geodatabase 数据库", "gdb", "OpenFileGDB"),
    ("GeoJSON 文件", "geojson", "GeoJSON"),
    ("GML 文件", "gml", "GML"),
    ("KML 文件", "kml", "KML"),
    ("SQLite 数据库", "sqlite", "SQLite"),
    ("CSV 表格", "csv", "CSV"),
)


class MainWindow(FramelessMixin, QMainWindow):
    """主窗口。"""

    def __init__(self, project_root: Optional[str] = None, app_name: str = "qgis_dev") -> None:
        super().__init__()
        # 无边框窗口：标题栏 / 拖动 / 缩放 / 最大化统一由 FramelessMixin 装配。
        # 顶部 chrome 用 QTabBar 取代菜单栏（点 tab 切换下方工具条）。
        self.title_bar = self.enable_frameless(with_tab_bar=True)
        self._app_name = app_name
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

        self.setWindowTitle(self._app_name)
        self.setGeometry(100, 100, 1200, 800)
        # 最小宽高 500×300：无边框窗口边缘拖拽缩放下限
        self.setMinimumSize(500, 300)
        self.setWindowIcon(Icons.app())

        # ---- 项目 + 图层树桥（先建好，_build_central_widget 要用 self.project）----
        self.project = QgsProject.instance()
        self.tree_root = self.project.layerTreeRoot()

        # ---- 地图画布 ----
        self.canvas = self._build_canvas()
        self.bridge = QgsLayerTreeMapCanvasBridge(self.tree_root, self.canvas)
        self.bridge.setAutoSetupOnFirstLayer(True)
        # 后台瓦片预热：天地图 XYZ 底图拖动/放大后白屏的修复（渲染完成 →
        # 后台离屏预取四周 8 个相邻视口的瓦片进缓存，平移时命中缓存不白屏）
        from app.core.domain.tile_prefetch import TilePrefetcher, ZoomTileGuard

        self._tile_prefetcher = TilePrefetcher(self.canvas, project=self.project, parent=self)
        # 缩放白屏防护：extentsChanged 时冻结画布 + 预取当前视口瓦片 + 解冻重绘，
        # 消除"缩放后总要白一下"的卡顿感（与 TilePrefetcher 互补：TilePrefetcher
        # 负责邻居预热用于平移，ZoomTileGuard 负责当前缩放不白屏）
        self._zoom_tile_guard = ZoomTileGuard(self.canvas, project=self.project, parent=self)
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
        #: 离线下载弹窗（懒建单实例，保留用户勾选/进度状态）
        self._offline_download_dialog = None
        #: 可用离线任务列表（平台任务接口就绪后由此注入 TaskInfo，见 TaskInfo.from_dict）
        self.offline_tasks: list = []
        #: 数据管理 Dock 加载策略(image/spatial/dict 分流,见 app.core.util.local_layer_loader)
        self._layer_loader = LocalLayerLoader(self)

        # ---- dock ----
        self._build_docks()

        # ---- 自定义标题栏（标题栏 + 菜单栏整体作为顶部 menuWidget）----
        self._build_chrome()

        # ---- 工具 / Tab 切换器 / 状态栏 ----
        self._build_tool_bar()
        self._build_tabs()
        self._build_status_bar()

        # ---- 默认激活平移 ----
        self.canvas.setMapTool(self._pan_tool)

        # ---- 启动清扫: 删上一会话残留的 .gpkg 别名(仅老 .db 兼容路径; 新规
        #      .gpkg 本体库无别名, 不会命中清扫的 _is_our_alias 闸门) ----
        from qgis.PyQt.QtCore import QTimer as _SweepTimer

        _SweepTimer.singleShot(2000, self._sweep_startup_gpkg_aliases)

    # ===================================================== 装配

    def _build_canvas(self) -> QgsMapCanvas:
        canvas = QgsMapCanvas(self)
        canvas.setAcceptDrops(True)
        # 拦截浏览器拖出的图层 URI，走与双击一致的添加路径（原生 drop 会把图层
        # 直接挂到工程根节点，不进"数据图层"分组、也不设 path_c）
        canvas.installEventFilter(self)
        # Linux/X11 边缘缩放：canvas 覆盖窗口主要边缘，注册为缩放监控目标
        # （canvas 已 installEventFilter(self)，monitor 只记录 + 开鼠标追踪）
        self.monitor_resize_edges(canvas)
        # 画布底色跟随主题（默认浅色为白色）
        canvas.setCanvasColor(QColor(ThemeManager.instance().current().canvas_bg))
        canvas.enableAntiAliasing(True)
        # ---- 编辑撤销 / 重做快捷键（Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y）----
        # 编辑工具（点选/框选 Delete、要素编辑、融合、分割、属性录入）每个动作都
        # 是一条 beginEditCommand…endEditCommand 记录，存在图层 undoStack；这里
        # 把快捷键接到「当前编辑图层」的撤销/重做上（QGIS 删除选中同一套语义）。
        # 作用域限定画布子树（WidgetWithChildrenShortcut）：焦点在画布时才生效，
        # 不抢 dock / 对话框文本输入框的原生 Ctrl+Z（画布本身无文本输入）。
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), canvas)
        self._undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._undo_shortcut.activated.connect(self._undo_edit)
        self._redo_shortcuts = [
            QShortcut(QKeySequence(seq), canvas) for seq in ("Ctrl+Shift+Z", "Ctrl+Y")
        ]
        for redo_sc in self._redo_shortcuts:
            redo_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            redo_sc.activated.connect(self._redo_edit)
        return canvas

    # ===================================================== 编辑撤销 / 重做

    def _undo_edit(self) -> None:
        """Ctrl+Z：撤销当前编辑图层最近一个编辑命令。

        目标图层取 :meth:`_first_editable_layer`（优先编辑目标，其次画布上任意
        可编辑矢量图层）；执行后清空间索引缓存并推迟出栈刷新画布。
        """
        self._step_edit_stack(undo=True)

    def _redo_edit(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z：重做最近被撤销的编辑命令。"""
        self._step_edit_stack(undo=False)

    def _step_edit_stack(self, undo: bool) -> None:
        """对优先编辑图层执行一步 undo / redo；无目标或无可用步骤则静默返回。"""
        target = self._first_editable_layer()
        if target is None:
            return
        stack = target.undoStack()
        if stack is None:
            return
        if undo and not stack.canUndo():
            return
        if not undo and not stack.canRedo():
            return
        if undo:
            stack.undo()
        else:
            stack.redo()
        # undo/redo 可能恢复或再次删除要素 → 空间索引缓存失效，下次命中需重建
        clear_spatial_index(target)
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, self._safe_edit_refresh)

    def _first_editable_layer(self) -> QgsVectorLayer | None:
        """撤销目标图层：优先 ``self.edit_layer``，否则画布上任一可编辑矢量图层。"""
        if self._is_undoable(self.edit_layer):
            return self.edit_layer
        for layer in self.canvas.layers():
            if self._is_undoable(layer):
                return layer
        return None

    @staticmethod
    def _is_undoable(layer) -> bool:
        """目标资格：矢量图层、处于编辑态、且已有编辑命令栈。"""
        return (
            layer is not None
            and isinstance(layer, QgsVectorLayer)
            and layer.isEditable()
            and layer.undoStack() is not None
        )

    def _safe_edit_refresh(self) -> None:
        """undo/redo 后刷新画布（事件栈外执行；窗口已销毁时静默）。"""
        with suppress(RuntimeError):
            self.canvas.refresh()

    def _build_central_widget(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        # 横向布局：[左侧竖排工具条][地图画布]——竖条位于左侧面板（图层列表 /
        # 数据浏览器 Dock）右侧、地图左侧，与设计稿一致
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 消息栏：悬浮盖在地图画布顶部，不挤占画布空间。
        self.messageBar = FloatingMessageBar(self.canvas)
        self.messageBar.setObjectName("FloatingMessageBar")
        self.messageBar.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        # 悬浮 toast 外观：消息条自带浅底圆角卡片，文字盖在地图上也可读
        # （该应用不引入 QGIS 桌面 qss，消息条/条目本身无样式，需自备）。
        self.messageBar.setStyleSheet(
            "#FloatingMessageBar {"
            "  background-color: rgba(255, 255, 255, 240);"
            "  border: 1px solid rgba(120, 120, 120, 120);"
            "  border-radius: 6px;"
            "}"
            "#FloatingMessageBar QLabel { color: #202020; }"
            "#FloatingMessageBar QToolButton {"
            "  border: none; background: transparent; color: #606060;"
            "}"
        )
        # 把 messageBar 放到全局属性（兼容旧 sendMessageToTopParent）
        qgis_app = QgsApplication.instance()
        if qgis_app is not None:
            qgis_app.setProperty("messageBar", self.messageBar)
        # 窗口销毁时必须清理该属性，否则残留悬空 QgsMessageBar*，
        # 后续 send() 访问会 native access violation
        self.destroyed.connect(self._clear_global_messagebar)

        # 显隐完全由消息条自己的"展示租约"驱动（见 app/infra/messagebar 的
        # FloatingMessageBar）：
        # 启动时从未 push → 保持隐藏；push → 显示，duration 秒后自动收起，
        # 不依赖 QgsMessageBar 内部移除机制，空条不可能出现。
        self._msg_active = False
        self.messageBar.set_on_state(self._on_floating_message_state)
        self.messageBar.hide()

        # 左侧竖排工具条（图层列表等左 Dock 与画布之间）：
        # 已实现工具接主窗口回调；未实现的由 SideToolBar 占位提示开发中
        self.side_toolbar = SideToolBar(
            self,
            {
                "select_point": self._on_select_point,
                "select_box": self._on_select_box,
                "pan": self._on_pan,
                "zoom_in": self._on_zoom_in,
                "zoom_full": self._on_zoom_full,
                "clear_selection": self._on_clear_selection,
                "condition_query": self._on_condition_query,
                "swipe": self._on_swipe,
            },
        )
        layout.addWidget(self.side_toolbar.widget)

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
        # 头部工具条：样式面板 / 添加图层组 / 删除 / 全部展开 / 全部折叠 / 上移 / 下移
        self.layer_dock.style_panel_requested.connect(self._on_header_style_panel)
        self.layer_dock.add_group_requested.connect(self._on_add_layer_group)
        self.layer_dock.remove_requested.connect(self._on_header_remove)
        self.layer_dock.expand_all_requested.connect(self._on_header_expand_all)
        self.layer_dock.collapse_all_requested.connect(self._on_header_collapse_all)
        self.layer_dock.move_up_requested.connect(lambda: self._on_move_layer_node(-1))
        self.layer_dock.move_down_requested.connect(lambda: self._on_move_layer_node(1))
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
            "select_point": self._on_select_point,
            "select_box": self._on_select_box,
            "clear_selection": self._on_clear_selection,
            "identify": self._on_identify,
            "zoom_in": self._on_zoom_in,
            "zoom_out": self._on_zoom_out,
            "quality": self._on_quality,
            "analyse": self._on_analyse,
            "swipe": self._on_swipe,
            "qmap_compare": self._on_qmap_compare,
            "online_basemap": self._on_online_basemap,
            "data_manage": self._on_data_manage,
            "offline_download": self._on_offline_download,
            "zoom_full": self._on_zoom_full,
            "refresh": self._on_refresh,
            "test": self._on_test,
        }
        self.tool_bar_builder = ToolBar(self)
        self.tool_bar = self.tool_bar_builder.build(callbacks)
        # 工具条按钮高亮跟随画布激活的地图工具（含启动时默认平移）
        self.canvas.mapToolSet.connect(self._on_map_tool_set)

        # 追加绘图 / 编辑工具条（2 个下拉按钮，与旧 component.draw_tool.Draw 一致）
        self.digitizing_toolbar = DigitizingToolbar(
            self,
            self.canvas,
            editing_layer_getter=lambda: self.edit_layer,
            # 点「开始编辑」时弹窗选层（不再由图层树右键/点击预设编辑图层）
            edit_layer_chooser=self._choose_edit_layer,
        )

        # 磁性捕捉工具条：吸附配置写入 QgsProject.snappingConfig() 并同步到
        # 画布 snapping utils；自定义绘制工具（添加点/线/面/矩形/圆）取点时经
        # snapped_map_point 吸附已有要素顶点/边——开关/类型/容差/模式全由这里控制
        self.snapping_toolbar = QgisSnappingToolbar(
            self,
            self.canvas,
            self.project,
            editing_layer_getter=lambda: self.edit_layer,
        )

        # 创建平移工具（保留引用避免被 GC）
        self._pan_tool = QgsMapToolPan(self.canvas)
        self._zoom_in_tool = QgsMapToolZoom(self.canvas, False)
        self._zoom_out_tool = QgsMapToolZoom(self.canvas, True)

    def _build_digitizing_toolbar(self) -> None:
        """兼容旧接口（占位）：由 :class:`DigitizingToolbar` 直接接管。"""
        return

    def _build_tabs(self) -> None:
        """把菜单栏改造为顶部 Tab 切换器：点不同 tab，下方工具条跟着切换。

        原菜单栏的「文件 / 查看 / 流程」功能拆成独立工具条挂到对应 tab；
        默认的「地图工具」工具条放进新增的「通用」tab。菜单项的快捷键保留在
        对应 QAction 上（Ctrl+N / Ctrl+O / Ctrl+L 等仍生效）。
        """
        tab_bar = self._tab_bar
        tab_bar.setDrawBase(False)
        tab_bar.setExpanding(False)

        self._file_toolbar = self._build_file_toolbar()
        self._view_toolbar = self._build_view_toolbar()

        #: tab 索引 → 该 tab 下应显示的工具条列表（切换时互斥显示）
        self._tab_toolbars: dict[int, list] = {}
        for label, toolbars in (
            ("文件", [self._file_toolbar]),
            ("查看", [self._view_toolbar]),
            ("编辑", [
                self.digitizing_toolbar.digitizing_toolbar,
                self.snapping_toolbar.toolbar,
            ]),
            ("通用", [self.tool_bar]),
        ):
            self._tab_toolbars[tab_bar.addTab(label)] = toolbars

        tab_bar.currentChanged.connect(self._on_tab_changed)
        self._on_tab_changed(tab_bar.currentIndex())

    def _build_file_toolbar(self) -> QToolBar:
        """「文件」tab 工具条：新建工程 / 打开工程 / 打开文件 / 退出（原文件菜单）。"""
        tb = QToolBar("文件", self)
        tb.setObjectName("FileTabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        act_new = QAction("新建工程", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._on_new_project)
        tb.addAction(act_new)

        act_open_proj = QAction("打开工程", self)
        act_open_proj.setShortcut("Ctrl+Shift+O")
        act_open_proj.triggered.connect(self._on_open_project)
        tb.addAction(act_open_proj)

        act_open = QAction("打开文件", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self._on_open_file)
        tb.addAction(act_open)

        tb.addSeparator()

        act_exit = QAction("退出", self)
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.close)
        tb.addAction(act_exit)

        self.addToolBar(tb)
        return tb

    def _build_view_toolbar(self) -> QToolBar:
        """「查看」tab 工具条：日志、流程节点编辑器（原查看 / 流程菜单）。"""
        tb = QToolBar("查看", self)
        tb.setObjectName("ViewTabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        act_log = QAction("日志", self)
        act_log.setShortcut("Ctrl+L")
        act_log.triggered.connect(self._show_log_dock)
        tb.addAction(act_log)

        act_flow = QAction("流程节点编辑器", self)
        act_flow.setShortcut("Ctrl+Shift+F")
        act_flow.triggered.connect(self._open_flow_editor)
        tb.addAction(act_flow)

        self.addToolBar(tb)
        return tb

    def _on_tab_changed(self, index: int) -> None:
        """tab 切换：只显示当前 tab 对应的工具条，其余隐藏（模拟"换工具栏"）。"""
        for idx, toolbars in self._tab_toolbars.items():
            visible = idx == index
            for tb in toolbars:
                tb.setVisible(visible)

    def _build_status_bar(self) -> None:
        self.status_bar_builder = StatusBar(self)
        self.status_bar_builder.build()

    # ------------------------------------------------------------ 图层加载指示

    def _begin_layer_loading(self, text: str) -> None:
        """图层加载开始：状态栏右侧亮起 QGIS 式转圈指示（配平计数）。"""
        builder = getattr(self, "status_bar_builder", None)
        if builder is not None:
            builder.show_loading(text)

    def _end_layer_loading(self) -> None:
        """图层加载结束（成功 / 失败都调）：状态栏指示熄灭。"""
        builder = getattr(self, "status_bar_builder", None)
        if builder is not None:
            builder.hide_loading()

    # ===================================================== 自定义标题栏（方案 A）

    def _build_chrome(self) -> None:
        """顶部 chrome 主题装配。

        无边框标题栏 + 菜单栏的装配已由 :meth:`FramelessMixin.enable_frameless`
        完成（__init__ 开头调用）；这里只负责窗口级主题：用户信息 + 主题连接 + QSS，
        并把标题栏模式开关的 ``mode_changed`` 信号接上日志。
        """
        self._apply_user_info_to_titlebar()
        self.title_bar.mode_changed.connect(self._on_mode_changed)
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._apply_theme_qss()

    def _on_mode_changed(self, mode: str) -> None:
        """任务 / 工程模式切换 → 记日志（供日志面板追溯操作历史）。"""
        from app.ui.main_window.title_bar import MODE_PROJECT, MODE_TASK

        label = {MODE_PROJECT: "工程模式", MODE_TASK: "任务模式"}.get(mode, mode)
        logger.info("模式切换：%s", label)

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
        # 左侧竖排工具条图标同步重刷
        side_tb = getattr(self, "side_toolbar", None)
        if side_tb is not None:
            side_tb.apply_theme(theme)
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

    # ===================================================== 工具回调

    def _tool_key(self, tool) -> Optional[str]:
        """地图工具 → 工具条按钮 key（None 表示非工具条地图工具，不高亮）。"""
        if tool is None:
            return None
        if tool is self._pan_tool:
            return "pan"
        if tool is self._zoom_in_tool:
            return "zoom_in"
        if tool is self._zoom_out_tool:
            return "zoom_out"
        return {
            "PointSelectTool": "select_point",
            "RectSelectTool": "select_box",
            "FindFeatureTool": "identify",
            "SwipeTool": "swipe",
        }.get(type(tool).__name__)

    def _on_map_tool_set(self, tool) -> None:
        """画布激活的地图工具变化 → 同步顶部工具条与左侧竖排工具条单选高亮。"""
        key = self._tool_key(tool)
        self.tool_bar_builder.set_active(key)
        self.side_toolbar.set_active(key)

    def _on_condition_query(self) -> None:
        """条件查询：对当前选中 / 编辑的矢量图层打开 SQL 条件查询构建器。

        未选中图层时提示先选层（与属性表「筛选」同一对话框，见
        :class:`app.ui.dialogs.query_builder.QueryBuilderDialog`）。
        """
        from qgis.core import QgsVectorLayer

        layer = self._selected_layer or self.edit_layer
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self.messageBar.pushMessage(
                "请先在图层列表选择要查询的矢量图层",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        from app.ui.dialogs.query_builder import QueryBuilderDialog

        dlg = QueryBuilderDialog(layer, parent=self)
        dlg.exec()

    def _on_pan(self) -> None:
        self.canvas.setMapTool(self._pan_tool)
        # setMapTool(同一工具) 不触发 mapToolSet：重复点击已激活按钮时
        # 勾选态被 QAction 自动翻转，这里显式恢复
        self.tool_bar_builder.set_active("pan")

    def _on_select_point(self) -> None:
        from app.ui.tools.select_feature import PointSelectTool

        self._select_point_tool = PointSelectTool(self.canvas)
        self.canvas.setMapTool(self._select_point_tool)
        self.tool_bar_builder.set_active("select_point")

    def _on_select_box(self) -> None:
        from app.ui.tools.select_feature import RectSelectTool

        self._select_box_tool = RectSelectTool(self.canvas)
        self.canvas.setMapTool(self._select_box_tool)
        self.tool_bar_builder.set_active("select_box")

    def _on_clear_selection(self) -> None:
        """取消选择：清空**所有矢量图层**的当前选择并刷新画布。"""
        from qgis.PyQt.QtCore import QTimer
        from qgis.core import QgsVectorLayer

        for layer in self.project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.selectedFeatureCount() > 0:
                layer.removeSelection()
        QTimer.singleShot(0, self.canvas.refresh)

    def _on_identify(self) -> None:
        from app.ui.tools.find_feature import FindFeatureTool

        self._find_feature_tool = FindFeatureTool(self.canvas, parent=self)
        self.canvas.setMapTool(self._find_feature_tool)
        self.tool_bar_builder.set_active("identify")

    def show_feature_info(self, result, highlight=None) -> None:
        """点击识别后把要素详情显示到右侧 Dock（懒创建，内容就地刷新）。"""
        if not hasattr(self, "_feature_info_dock") or self._feature_info_dock is None:
            self._feature_info_dock = FeatureInfoDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._feature_info_dock)
        self._feature_info_dock.show_feature(result, highlight=highlight)

    def _on_zoom_in(self) -> None:
        self.canvas.setMapTool(self._zoom_in_tool)
        self.tool_bar_builder.set_active("zoom_in")

    def _on_zoom_out(self) -> None:
        self.canvas.setMapTool(self._zoom_out_tool)
        self.tool_bar_builder.set_active("zoom_out")

    def _on_quality(self) -> None:
        from app.ui.dialogs.check_viewer import CheckViewerDialog

        dlg = CheckViewerDialog(self, "")
        dlg.show()

    def _on_offline_download(self) -> None:
        """打开「下载离线图层」弹窗（懒建单实例，保留勾选与进度状态）。"""
        from app.ui.dialogs.offline_download import OfflineDownloadDialog

        if self._offline_download_dialog is None:
            self._offline_download_dialog = OfflineDownloadDialog(
                self, canvas=self.canvas, tasks=self.offline_tasks or []
            )
        dlg = self._offline_download_dialog
        dlg.set_tasks(self.offline_tasks or dlg.tasks)
        # 最小化状态下 show()/raise_() 不还原窗口 → 显式 showNormal 再激活
        if dlg.isMinimized():
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_analyse(self) -> None:
        # 复用单个"分析工具"dock，避免每次点"分析"都新建一个
        if getattr(self, "_analyse_dock", None) is None:
            self._analyse_dock = ToolsDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._analyse_dock)
        self._analyse_dock.show()
        self._analyse_dock.raise_()

    def _on_online_basemap(self) -> None:
        """在线底图：懒建右侧 Dock，按独立清单文件刷新列表。"""
        from app.ui.docks.online_basemap_dock import OnlineBasemapDock

        dock = getattr(self, "_online_basemap_dock", None)
        if dock is None:
            dock = OnlineBasemapDock(
                self,
                on_add=self._add_online_basemap,
                on_add_template=self._add_online_basemap_template,
                on_add_custom=self._add_custom_basemap,
                on_manage_keys=self._manage_basemap_keys,
                on_edit=self._edit_online_basemap,
                on_delete=self._delete_online_basemap,
            )
            self._online_basemap_dock = dock
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.set_layers(self._online_basemap_defs())
        dock.show()
        dock.raise_()

    def _on_data_manage(self) -> None:
        """数据管理：懒建底部 Dock（本地数据库注册目录分级展示）。

        「+ 新建连接」打开选库弹窗（本地数据库 Tab，见
        :mod:`app.ui.dialogs.add_connection`），成功后把新连接追加进左树并
        联动右侧数据表；创建数据 / 导入 / 刷新由 Dock 内部占位记日志
        （后续版本接入真实功能）。
        """
        from app.ui.docks.data_manage_dock import DataManageDock

        dock = getattr(self, "_data_manage_dock", None)
        if dock is None:
            dock = DataManageDock(
                self,
                on_add_connection=self._on_dm_add_connection,
                on_add_layer=self._on_dm_add_layer,
            )
            self._data_manage_dock = dock
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        dock.show()
        dock.raise_()

    def _on_dm_add_connection(self) -> None:
        """数据管理「+ 新建连接」：弹窗选本地库文件或填服务器连接并加入树。"""
        from app.ui.dialogs.add_connection import AddConnectionDialog

        dlg = AddConnectionDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dock = getattr(self, "_data_manage_dock", None)
        if dock is not None:
            dock.add_connection(dlg.connection())

    def _on_dm_add_layer(self, db: dict, category: str, name: str) -> None:
        """数据管理表格行双击：把注册表作为图层加入工程。

        加载策略(image / spatial / dict 分流)统一委托
        :class:`app.core.util.local_layer_loader.LocalLayerLoader`。本方法
        只负责 UX(spinner / 模态警告 / 信息弹窗 / messageBar Success / 入
        工程 / 缩放),业务路径与原 MainWindow 行为一致:

        - ``image``:gpkg 内容 → GDAL/GPKG 栅格直开(gdal provider, 普通文件
          路径, 经免 .aux.xml 边车的打开); 直开失败(非 GeoPackage 内容)直接
          模态提示改用 .gpkg 库 —— 无本地 XYZ 回落(服务已移除);
        - ``spatial``:OGR provider 直开(``路径|layername=表名``)。新规 gpkg
          内容库本体即 ``.gpkg`` 后缀 → GPKG 驱动直开, 无别名; 仅老 ``.db``
          后缀 gpkg 库才经同 inode 的 ``.gpkg`` 硬链接别名打开(GDAL 3.11
          按后缀裁决驱动, .db 会被 SQLite 抢先成纯属性表; 别名仅图层存续
          期间存在, 移除/退出即删); 老 spatialite 库 OGR 失败时才有回退;
        - ``dict`` 等无几何数据:沿用原 ``QMessageBox.information`` 提示。
        """
        from app.core.util.local_db_util import _CATEGORY_LABEL

        path = str(db.get("path") or "")
        label = _CATEGORY_LABEL.get(category, category)
        if not path:
            QMessageBox.warning(self, "数据管理", f"「{name}」缺少库文件路径, 无法加载。")
            return
        kind_zh = "影像" if category == "image" else "图层"
        self._begin_layer_loading(f"正在加载{kind_zh}：{name}")
        try:
            try:
                layer = self._layer_loader.load(db, category, name, parent=self)
            except LocalLayerUnsupportedCategoryError:
                QMessageBox.information(
                    self,
                    "数据管理",
                    f"「{name}」为{label}数据(无空间几何), 无法加入地图。\n"
                    f"可在「执行SQL」中按表查看记录。",
                )
                return
            except LocalLayerError as exc:
                QMessageBox.warning(self, "数据管理", f"「{name}」无法打开: {exc}")
                return
            if layer is None:
                QMessageBox.warning(
                    self,
                    "数据管理",
                    f"「{name}」无法作为地图图层打开\n(文件: {path})",
                )
                return
            layer.path_c = path  # 容器路径, 供"执行SQL"按文件打开
            self._add_layer_to_project(layer)
            self._zoom_to_layer(layer)
            self.messageBar.pushMessage(
                f"已加载{kind_zh}：{name}",
                level=Qgis.MessageLevel.Success,
                duration=3,
            )
        finally:
            self._end_layer_loading()

    def _on_swipe(self) -> None:
        """卷帘对比：分屏会话已存续则直接恢复（不弹窗）；否则弹窗选栅格激活。

        分屏是视图状态常驻：切到任意工具（平移/缩放/画要素/查询）后覆盖层仍在，
        再点「卷帘对比」即恢复分隔条拖拽；Esc 才真正退出会话。
        """
        tool = getattr(self, "_swipe_tool", None)
        if tool is not None and tool.is_alive():
            self.canvas.setMapTool(tool)
            self.tool_bar_builder.set_active("swipe")
            return
        if not any(isinstance(l, QgsRasterLayer) for l in self.project.mapLayers().values()):
            QMessageBox.information(self, "提示", "请先加载至少一个栅格图层")
            return
        from app.ui.dialogs.swipe_compare import SwipeCompareDialog
        from app.ui.tools.swipe import SwipeTool

        dlg = SwipeCompareDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        base, compare, orientation = dlg.selected_params()
        if base is None or compare is None:
            return
        self._swipe_tool = SwipeTool(self.canvas, base, compare, orientation, parent=self)
        self.canvas.setMapTool(self._swipe_tool)
        self.tool_bar_builder.set_active("swipe")

    def _on_qmap_compare(self) -> None:
        """地图对比：会话激活时再点即停止；否则弹窗选择图层 + 模式后启动。

        支持三种模式（参照 qmapcompare 插件）：
        - 垂直/水平分割、放大镜：mask 图层组方案（CompareMask）
        - 镜像分屏：右侧 dock 第二画布（MirrorCompare）
        """
        session = getattr(self, "_qmap_compare_session", None)
        if session is not None and session.is_active:
            session.stop()
            self.tool_bar_builder.set_session_active("qmap_compare", False)
            self.messageBar.pushMessage("已停止地图对比", level=Qgis.MessageLevel.Info, duration=3)
            return
        if not self.project.mapLayers():
            QMessageBox.information(self, "提示", "请先加载至少一个图层")
            return
        from app.ui.dialogs.qmap_compare import QMapCompareDialog

        dlg = QMapCompareDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        layers, mode, lens_shape, lens_rate = dlg.selected()
        if not layers:
            return
        if mode == "mirror":
            from app.ui.tools.qmap_compare import MirrorCompare

            session = MirrorCompare(self, self.canvas, parent=self)
            session.start(layers)
        else:
            from app.ui.tools.qmap_compare import CompareMask

            session = CompareMask(self.project, parent=self)
            session.start(layers, mode, lens_shape, lens_rate)
        self._qmap_compare_session = session
        # 地图对比无地图工具：会话存活期间按钮独立常亮（再点停止）
        self.tool_bar_builder.set_session_active("qmap_compare", True)
        self.messageBar.pushMessage(
            "地图对比已启动，再点「地图对比」停止",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )

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
            import threading

            from app.server.cli import main as server_main

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
            self.project_dock.layer_context_menu_requested.connect(self._on_project_dock_context_menu)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.project_dock)
        self._on_project_create(name)
        self.project_dock.show()
        self.project_dock.raise_()

    def _ask_project_name(self):
        """弹窗输入工程名；返回工程名，取消或为空则返回 ``None``。"""
        from qgis.PyQt.QtWidgets import QInputDialog

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

        # 重建分组：参考图层（底图）+ 数据图层（用户数据）。
        # **顺序敏感**：QGIS 按 layer tree 索引升序渲染——索引 0 先画、最后索引
        # 画在最上面。所以参考图层（底图）必须建在根分组**底部**（先渲染），
        # 数据图层建在**顶部**（后渲染、盖在底图上）。若顺序反了，缩放/平移时
        # 底图会被排到最后渲染，慢速矢量图层会拖住它——表现为"等矢量画完才出
        # 底图"。``addGroup`` 是 append，所以建组顺序直接决定渲染顺序。
        root = self.project.layerTreeRoot()
        self._ref_group = root.addGroup("参考图层")
        self._data_group = root.addGroup("数据图层")

        # 工程名 + 坐标系（project.clear() 会把 CRS 重置回默认）
        self.project.setTitle(name)
        self.setWindowTitle(f"{self._app_name} - {name}")
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
        # 所有用户导入（打开文件 / 浏览器双击 / 拖拽）都走这里，统一记日志
        src = getattr(layer, "path_c", None) or layer.source()
        logger.info("导入图层：%s（来源=%s）", layer.name(), src)

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
        # QgsProject.read 会同步读入工程里所有图层（大工程较慢）→ 期间亮起加载指示
        self._begin_layer_loading(f"正在打开图层：{os.path.basename(path)}")
        try:
            opened = self.project.read(path)
        finally:
            self._end_layer_loading()
        if opened:
            self.canvas.setDestinationCrs(self.project.crs())
            self.canvas.refreshAllLayers()
            self.setWindowTitle(f"{self._app_name} - {self.project.title()}")
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
            # 老工程可能把"参考图层"建在数据图层之上（索引更大、后渲染），
            # 缩放时底图会被慢速矢量图层拖住。强制把参考图层挪到根分组最底，
            # 与 ``_on_project_create`` 的建组顺序一致。``insertChildNode`` 在
            # 同父节点上移动已存在的子节点时只重排不重发 willRemoveChildren 给
            # bridge（group 节点不挂 layer，bridge 不关心）。
            if self._ref_group is not None:
                root = self.project.layerTreeRoot()
                if root.children().index(self._ref_group) != 0:
                    root.insertChildNode(0, self._ref_group)
            if getattr(self, "project_dock", None) is not None:
                if self._data_group is not None and self._ref_group is not None:
                    self.project_dock.set_project(self.project.title(), self._data_group, self._ref_group)
                else:
                    self.project_dock.reset()
            self.messageBar.pushMessage(f"已打开工程：{path}", level=Qgis.MessageLevel.Info, duration=3)
        else:
            QMessageBox.warning(self, "打开失败", f"无法打开工程：{path}")

    def _map_config_path(self):
        """``config/app.yaml`` 实际路径（``QGIS_DEV_CONFIG`` 可覆盖），读/写共用。"""
        import os
        from pathlib import Path

        from app.infra.fs.paths import Paths

        return Path(os.environ.get("QGIS_DEV_CONFIG") or Paths.default().config_file("app.yaml"))

    def _load_map_config(self):
        """读取并解析 ``config/app.yaml``；失败返回 ``None``（不抛、只记日志）。"""
        try:
            from app.env.config import AppConfig

            return AppConfig.from_file(self._map_config_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取配置文件失败：%s", exc, exc_info=True)
            return None

    def _load_config_base_tile(self):
        """读取配置并加载默认底图（新建工程时恢复天地图底图）。返回图层或 ``None``。"""
        self._begin_layer_loading("正在加载默认底图…")
        try:
            from app.core.domain.basemap import add_base_tile_layer

            cfg = self._load_map_config()
            if cfg is None:
                return None
            layer = add_base_tile_layer(cfg.map, project=self.project)
            if layer is not None:
                self.messageBar.pushMessage(f"已加载底图：{layer.name()}", level=Qgis.MessageLevel.Info, duration=2)
            return layer
        except Exception as exc:  # noqa: BLE001
            self.messageBar.pushMessage(f"底图加载失败：{exc}", level=Qgis.MessageLevel.Critical, duration=5)
            return None
        finally:
            self._end_layer_loading()

    def _online_basemaps_config_path(self):
        """``online_basemap_config.yaml`` 实际路径（与 app.yaml 同目录）。

        在线底图清单独立于 ``config/app.yaml``（键/底图仍在 app.yaml，清单在此
        固定名文件）；QGIS_DEV_CONFIG 指向哪份 app.yaml，清单就跟到同目录。
        """
        return self._map_config_path().with_name("online_basemap_config.yaml")

    def _online_basemap_defs(self):
        """读取独立清单文件里的在线底图（文件缺失 / 读取失败返回空列表）。"""
        try:
            from app.env.config import load_online_basemaps

            return load_online_basemaps(self._online_basemaps_config_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取在线底图清单失败：%s", exc, exc_info=True)
            return []

    @staticmethod
    def _basemap_conflicts_with(d, others) -> bool:
        """``d`` 与 ``others`` 里任一条同源 → ``True``（添加去重 / 编辑撞名共用）。

        同源判定：``type`` 相同 + ``url`` 经 :func:`canonical_basemap_url` 归一后
        相同 + ``layers`` 相同（两个 WMS 可能共享服务 url 但 layers 不同；xyz
        layers 为空 → 等价于 url 判重）。url 归一把老配置内嵌真实 tk 的 url 与
        ``{tk}`` 模板判等时互认（tk=cb75… == tk={tk}），去重/撞名不失效。
        """
        from app.core.domain.basemap import canonical_basemap_url

        if not d.url:
            return False
        return any(
            (x.type or "xyz") == (d.type or "xyz")
            and canonical_basemap_url(x.url) == canonical_basemap_url(d.url)
            and x.layers == d.layers
            for x in others
        )

    def _add_online_basemap_template(self, d) -> bool:
        """把一条内置模板/自定义底图写回 ``online_basemap_config.yaml`` 并刷新树。

        右键「添加在线底图」/「添加自定义底图」的回调：Dock 只负责把条目递给
        本方法，真正持久化（文本追加到独立清单文件）与刷新 Dock 树在这里做；
        同源（url 相同）条目已存在则只提示不重复加。返回是否成功。
        """
        try:
            from app.env.config import (
                append_online_basemap_to_yaml,
                load_online_basemaps,
            )

            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)  # 缺失/空 = 空清单（可从模板加）
            if self._basemap_conflicts_with(d, current):
                self.messageBar.pushMessage(
                    f"该底图已在在线底图清单中：{d.name}",
                    level=Qgis.MessageLevel.Info,
                    duration=2,
                )
                return True
            append_online_basemap_to_yaml(path, d)
            # 写回后重读并刷新 dock 树（新增条目立刻出现在分组里）
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs())
            self.messageBar.pushMessage(
                f"已添加在线底图：{d.name}（已写入 online_basemap_config.yaml）",
                level=Qgis.MessageLevel.Info,
                duration=3,
            )
            logger.info("添加在线底图类型：%s", d.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("添加在线底图类型失败 %s：%s", getattr(d, "name", d), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"添加在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _add_custom_basemap(self) -> bool:
        """Dock 右键「添加自定义底图」：弹窗配置后走模板同一条持久化路径。

        弹窗组装好 :class:`OnlineBasemapDef`（type=xyz/arcgis/arcgisfeature/wms/wmts），
        委托 :meth:`_add_online_basemap_template` 写回 ``online_basemap_config.yaml`` 并
        刷新 Dock 树（去重 / messageBar / 异常处理全复用）。取消返回 False。
        按已定决策**不**加载画布——自定义底图先进树，双击再出图。
        """
        from app.ui.dialogs.custom_basemap import CustomBasemapDialog

        dlg = CustomBasemapDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        return self._add_online_basemap_template(dlg.basemap_def())

    def _manage_basemap_keys(self) -> bool:
        """Dock 右键「管理 Key」：弹窗集中管理 provider key，写回 ``map.keys``。

        弹窗预填当前 ``map.keys``（天地图 tk / 高德预留字段），确定后经
        :func:`set_map_keys` **文本写回** ``config/app.yaml`` 的 ``map.keys``
        （只动本段，URL 仍保持 ``{tk}`` 占位，注释/其余排版保留）。取消返回
        False。改 key 只影响**新建/重载**的图层（已加载图层 source 已焊死），
        文案已如实提示。
        """
        from app.env.config import set_map_keys
        from app.ui.dialogs.manage_keys import ManageKeysDialog

        cfg = self._load_map_config()
        if cfg is None:
            self.messageBar.pushMessage(
                "地图服务 Key 管理：配置文件读取失败",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        dlg = ManageKeysDialog(self, cfg.map.keys)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        new_keys = dlg.keys()
        try:
            set_map_keys(self._map_config_path(), new_keys)
        except Exception as exc:  # noqa: BLE001
            logger.error("写回 map.keys 失败：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"保存地图服务 Key 失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        self.messageBar.pushMessage(
            "已保存地图服务 Key：新建 / 下次加载的底图即生效（已加载底图需重载）",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )
        logger.info(
            "已更新地图服务 Key：tianditu_tk=%s gaode_key=%s",
            "***" if new_keys.tianditu_tk else "",
            "***" if new_keys.gaode_key else "",
        )
        return True

    def _edit_online_basemap(self, d) -> bool:
        """Dock 叶子右键「编辑连接」：弹窗编辑清单条目，确定后文本写回并刷新树。

        对话框（:class:`EditBasemapDialog`）复用「添加自定义底图」的字段并预填现有
        条目（名称/分组/类型/url/类型参数/反盗链 Referer），就近提供 API key 编辑——
        key 真值仍**集中管理**（url 里 ``{tk}`` / ``{gaode_key}`` 占位符引用
        ``map.keys``，改一处全站生效），确定后分两处落盘：

        - 清单条目经 :func:`update_online_basemap_in_yaml` **原位文本替换**回
          ``online_basemap_config.yaml``（注释/其余条目保留）并刷新 Dock 树；
        - key 有改动（或老配置内嵌 tk 被迁移成占位符）时，真值经
          :func:`set_map_keys` 只动 ``app.yaml`` 的 ``keys`` 段。

        改后的连接下次双击才按新参数出图（工程里已加载的旧图层不受影响，需移除后
        重新「添加到画布」）。与清单里其它条目撞源（同 type+归一 url+layers）则
        拒绝保存只提示。返回是否成功。取消返回 False。
        """
        try:
            from app.env.config import (
                load_online_basemaps,
                set_map_keys,
                update_online_basemap_in_yaml,
            )
            from app.ui.dialogs.edit_basemap import EditBasemapDialog

            cfg = self._load_map_config()
            if cfg is None:
                self.messageBar.pushMessage(
                    "编辑在线底图：配置文件读取失败",
                    level=Qgis.MessageLevel.Critical,
                    duration=5,
                )
                return False
            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)
            if d not in current:
                self.messageBar.pushMessage(
                    "在线底图清单已变化（找不到该连接），请刷新后重试",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False

            dlg = EditBasemapDialog(self, def_=d, keys=cfg.map.keys)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
            new_def = dlg.basemap_def()
            key_override = dlg.keys_override()
            if new_def == d and key_override is None:
                self.messageBar.pushMessage("在线底图未做修改", level=Qgis.MessageLevel.Info, duration=2)
                return True
            if new_def != d and self._basemap_conflicts_with(new_def, [x for x in current if x != d]):
                self.messageBar.pushMessage(
                    f"编辑后与清单中已有底图同源：{new_def.name}（未保存）",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False

            # 先写 key（集中真值，可能被 url 占位符引用），再原位替换清单条目
            if key_override is not None:
                set_map_keys(self._map_config_path(), key_override)
            if new_def != d:
                update_online_basemap_in_yaml(path, d, new_def)
                dock = getattr(self, "_online_basemap_dock", None)
                if dock is not None:
                    dock.set_layers(self._online_basemap_defs())

            parts = []
            if new_def != d:
                parts.append(f"已更新在线底图：{new_def.name}")
            if key_override is not None:
                parts.append("服务 Key 已同步（新建 / 重载底图生效）")
            self.messageBar.pushMessage("；".join(parts), level=Qgis.MessageLevel.Info, duration=3)
            logger.info(
                "更新在线底图连接：%s → %s%s",
                d.name,
                new_def.name if new_def != d else d.name,
                "（key 同步）" if key_override is not None else "",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("编辑在线底图失败 %s：%s", getattr(d, "name", d), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"编辑在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _delete_online_basemap(self, d) -> bool:
        """Dock 叶子右键「删除连接」：确认后从清单文件移除条目并刷新 Dock 树。

        删除是**破坏性**操作，确认弹窗（Modal QMessageBox）归 MainWindow 所有——
        Dock 只把要删的条目递给本方法。只移除 ``online_basemap_config.yaml`` 里的
        配置条目：工程里**已加载**的底图图层不受影响（source 已焊死），但下次无法
        再从列表添加该连接。找不到 ``d``（清单被外部改动）或确认取消都不落盘。
        返回是否成功删除（取消 / 失败为 False）。
        """
        try:
            from app.env.config import load_online_basemaps, remove_online_basemap_from_yaml

            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)
            if d not in current:
                self.messageBar.pushMessage(
                    "在线底图清单已变化（找不到该连接），请刷新后重试",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False
            ret = QMessageBox.question(
                self,
                "删除在线底图",
                f"确定从在线底图清单中删除「{d.name}」吗？\n\n"
                "已加载到工程的图层不受影响，删除后将无法再从列表添加该连接。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return False

            remove_online_basemap_from_yaml(path, d)
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs())
            self.messageBar.pushMessage(
                f"已删除在线底图：{d.name}",
                level=Qgis.MessageLevel.Info,
                duration=3,
            )
            logger.info("删除在线底图连接：%s", d.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("删除在线底图失败 %s：%s", getattr(d, "name", d), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"删除在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _add_online_basemap(self, tile) -> bool:
        """把一条在线底图配置加进工程「参考图层」分组顶部。

        ``type`` 分派：xyz 瓦片 / ArcGIS 影像服务（栅格）/ ArcGIS 要素服务
        （FeatureServer 矢量）/ OGC WMS / WMTS（见
        basemap 模块 :func:`build_online_basemap_layer`）。带 ``referer`` 的
        xyz 行先装全局 Referer 预处理（连接串 referer= 不总是生效，见 basemap
        模块 docstring）；工程里已有同源图层则只提示、不重复加。
        返回是否成功，供 :class:`OnlineBasemapDock` 回调。

        期间状态栏亮起「正在加载图层」指示（含 FeatureServer 矢量整层拉取的
        长等待），try/finally 保证成功 / 失败都熄灭。
        """
        self._begin_layer_loading(f"正在加载图层：{tile.name}")
        try:
            from app.core.domain.basemap import (
                build_online_basemap_layer,
                install_referer_preprocessor,
            )

            if tile.referer:
                install_referer_preprocessor(tile.referer)
            # 树里叶子 url 仍为 {tk} 占位：在 uri 构造边界读最新 map.keys 替换，
            # 让刚改过的 key 对下一次添加立即生效（config 模型里的 url 恒为占位符）
            cfg = self._load_map_config()
            keys = cfg.map.keys if cfg is not None else None
            layer = build_online_basemap_layer(tile, keys)
            if layer is None:
                self.messageBar.pushMessage(
                    f"在线底图加载失败：{tile.name}",
                    level=Qgis.MessageLevel.Critical,
                    duration=5,
                )
                return False
            # 同源图层已存在（xyz 连接串相同）→ 去重，只提示不重复加
            uri = layer.source()
            for lyr in self.project.mapLayers().values():
                if lyr.source() == uri:
                    self.messageBar.pushMessage(
                        f"该底图已在工程中：{tile.name}",
                        level=Qgis.MessageLevel.Info,
                        duration=2,
                    )
                    return True
            self.project.addMapLayer(layer, False)
            group = getattr(self, "_ref_group", None) or self.project.layerTreeRoot()
            group.insertLayer(0, layer)  # 参考分组顶部 = 默认底图之上、数据之下
            self.canvas.refreshAllLayers()
            self.messageBar.pushMessage(
                f"已添加在线底图：{tile.name}",
                level=Qgis.MessageLevel.Info,
                duration=2,
            )
            logger.info("添加在线底图：%s", tile.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("添加在线底图失败 %s：%s", getattr(tile, "name", tile), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"添加在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        finally:
            self._end_layer_loading()

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
        self._begin_layer_loading(f"正在加载图层：{os.path.basename(file_path)}")
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
        finally:
            self._end_layer_loading()

    # ===================================================== 浏览器事件

    def _on_browser_double_clicked(self, index) -> None:
        from qgis.core import QgsLayerItem, QgsRasterLayer, QgsVectorLayer

        if not index.isValid():
            return
        item = self.browser_dock.model.dataItem(index)
        if not item or not isinstance(item, QgsLayerItem):
            return
        uri, name, provider = item.uri(), item.name(), item.providerKey()
        self._begin_layer_loading(f"正在加载图层：{name}")
        try:
            vlayer = QgsVectorLayer(uri, name, provider)
            if vlayer.isValid():
                # path_c 存**容器路径**（供"执行SQL"按文件打开）：item.path()
                # 对 gdb/gpkg 子图层是 ``容器|layername=层名`` 的 QGIS URI，
                # ogr.Open 不认，剥掉 | 及之后所有 provider 选项
                vlayer.path_c = uri.split("|", 1)[0]
                self._add_layer_to_project(vlayer)
                return
            rlayer = QgsRasterLayer(uri, name, provider)
            if rlayer.isValid():
                rlayer.path_c = uri.split("|", 1)[0]
                self._add_layer_to_project(rlayer)
        finally:
            self._end_layer_loading()

    def _on_browser_context_menu(self, pos) -> None:
        from qgis.PyQt.QtWidgets import QMenu

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
                    provider.populateContextMenu(item, menu, selectedItems=[item], context=context)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            from app.infra.logging import get_logger

            get_logger(__name__).exception("构建数据浏览器右键菜单失败，仅保留「刷新」")
        # 目录项：提供"新建"子菜单（各类矢量格式）
        try:
            from qgis.core import QgsDataCollectionItem, QgsDirectoryItem

            # QGIS 4 浏览器里普通文件夹是 QgsDirectoryItem，而 .gdb 等 OGR 数据集
            # 目录是 QgsDataCollectionItem（无 dirPath），统一取磁盘路径判定
            dir_path = getattr(item, "dirPath", None) or item.path()
            is_dir = isinstance(item, QgsDirectoryItem) or (
                isinstance(item, QgsDataCollectionItem) and os.path.isdir(dir_path)
            )
            if is_dir:
                is_gdb = str(dir_path).lower().endswith(".gdb")
                # .gdb 是数据集目录：只在库里新增图层，不在目录里塞零散文件
                if is_gdb:
                    act = menu.addAction("新增图层")
                    act.triggered.connect(lambda checked=False: self._on_browser_add_gdb_layer(item))
                else:
                    new_menu = menu.addMenu("新建")
                    for name, ext, driver in _NEW_VECTOR_FORMATS:
                        act = new_menu.addAction(name)
                        act.triggered.connect(
                            lambda checked=False, nm=name, ex=ext, dr=driver: self._on_browser_create_vector(
                                item, nm, ex, dr
                            )
                        )
                menu.addSeparator()
        except Exception:  # noqa: BLE001
            pass
        refresh = menu.addAction("刷新")
        refresh.triggered.connect(lambda: item.refresh())
        menu.exec(self.browser_dock.view.viewport().mapToGlobal(pos))

    def _on_browser_create_vector(self, item, name: str, ext: str, driver: str) -> None:
        """在浏览器目录项下新建空矢量数据文件（"新建"子菜单入口）。"""
        from qgis.PyQt.QtWidgets import QFileDialog
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransformContext,
            QgsVectorFileWriter,
            QgsVectorLayer,
        )

        dir_path = getattr(item, "dirPath", None) or item.path()
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"新建 {name}",
            os.path.join(dir_path, f"新建矢量.{ext}"),
            f"{name} (*.{ext})",
        )
        if not path:
            return
        crs = self.project.crs()
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4490")
        layer = QgsVectorLayer(f"Point?crs={crs.authid()}", "new_layer", "memory")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = driver
        error, errmsg, _newfile, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsCoordinateTransformContext(), opts
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            self.messageBar.pushCritical("新建图层", f"创建失败：{errmsg}")
            return
        item.refresh()
        self.messageBar.pushSuccess("新建图层", f"已创建 {os.path.basename(path)}")
        logger.info("新建矢量文件：%s（驱动=%s）", path, driver)

    def _on_browser_add_gdb_layer(self, item) -> None:
        """向已存在的 File Geodatabase（.gdb）中新增一个空图层。"""
        from qgis.PyQt.QtWidgets import QInputDialog
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransformContext,
            QgsVectorFileWriter,
            QgsVectorLayer,
        )

        gdb = getattr(item, "dirPath", None) or item.path()
        layer_name, ok = QInputDialog.getText(self, "新增图层", "图层名称：")
        if not ok or not layer_name.strip():
            return
        layer_name = layer_name.strip()
        geom_names = ("点", "线", "面")
        geom_defs = {"点": "Point", "线": "LineString", "面": "Polygon"}
        geom_text, ok = QInputDialog.getItem(self, "新增图层", "几何类型：", geom_names, 0, False)
        if not ok:
            return
        crs = self.project.crs()
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4490")
        layer = QgsVectorLayer(f"{geom_defs[geom_text]}?crs={crs.authid()}", layer_name, "memory")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "OpenFileGDB"
        opts.layerName = layer_name
        error, errmsg, _newfile, _saved = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, gdb, QgsCoordinateTransformContext(), opts
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            self.messageBar.pushCritical("新增图层", f"创建失败：{errmsg}")
            return
        item.refresh()
        self.messageBar.pushSuccess("新增图层", f"已创建 {layer_name}")
        logger.info("GDB 新增图层：%s → %s", layer_name, gdb)

    def _on_floating_message_state(self, active: bool) -> None:
        """悬浮消息条显隐回调（由消息条自己的租约驱动）。

        active=True：摆到画布顶部显示；False：整条隐藏。画布缩放时由
        :meth:`eventFilter` 调用 :meth:`_relayout_floating_message_bar` 同步宽度。
        """
        self._msg_active = bool(active)
        if active:
            self._relayout_floating_message_bar()
        else:
            bar = getattr(self, "messageBar", None)
            if bar is not None:
                try:
                    if not bar.isHidden():
                        bar.hide()
                except RuntimeError:
                    pass  # 窗口关闭流程中 C++ 对象已销毁

    def _relayout_floating_message_bar(self) -> None:
        """把悬浮消息条摆到画布顶部（整宽覆盖、高度随内容），仅在有消息时。"""
        bar = getattr(self, "messageBar", None)
        canvas = getattr(self, "canvas", None)
        if bar is None or canvas is None or not getattr(self, "_msg_active", False):
            return
        try:
            # 顶部留 6px 边距、取画布整宽；高度按消息内容自适应
            bar.setGeometry(6, 6, max(canvas.width() - 12, 0), bar.sizeHint().height())
            bar.show()
            bar.raise_()
        except RuntimeError:
            pass  # 窗口关闭流程中 C++ 对象已销毁

    # ---- 画布拖拽添加图层（数据浏览器拖出 → 画布 drop）----

    def eventFilter(self, watched, event) -> bool:
        """画布拖拽拦截 + 画布缩放时同步悬浮消息条。

        画布拖拽：把浏览器拖出的图层 URI 转给 :meth:`_on_canvas_drop`。
        原生 :class:`QgsMapCanvas` 的 drop 会把图层直接加进工程根节点，不进
        "数据图层"分组、也不设 ``path_c``，与双击行为不一致；这里拦下来复用
        同一套 ``_add_layer_to_project`` 路径。非图层 URI 的拖拽（如外部文件）
        放行给原生处理。

        悬浮消息条宽度随画布缩放（消息条处于显示状态时同步几何）。
        """
        canvas = getattr(self, "canvas", None)
        if canvas is not None and watched is canvas:
            if event.type() == QEvent.Type.Resize:
                self._relayout_floating_message_bar()
            if event.type() in (
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
                # 容器路径（剥 |layername= 等 provider 选项），供"执行SQL"按文件打开
                layer.path_c = u.uri.split("|", 1)[0]
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

        菜单项统一作用在 ``self._selected_layer`` 上；各动作方法（缩放/移分组/
        属性表/样式/移除/导出/属性）读的都是这个字段，所以进入时先把它指向
        当前右键的图层。

        「设为编辑图层」入口已移除：编辑图层统一由点「开始编辑」时的弹窗选择
        （见 :meth:`_choose_edit_layer`）。

        布局贴近 QGIS 原生图层面板：缩放至图层 / 放大至选中要素 → 打开属性表 /
        执行SQL（仅矢量）→ 重命名 / 复制图层 → 样式（含复制/粘贴样式）→ 移到
        分组 → 移除 / 导出 / 属性。
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
        if getattr(self, "_data_group", None) is not None and getattr(self, "_ref_group", None) is not None:
            menu.addSeparator()
            act_to_ref = QAction("移到参考图层", self)
            act_to_ref.triggered.connect(lambda: self._move_layer_to_group(self._selected_layer, self._ref_group))
            menu.addAction(act_to_ref)
            act_to_data = QAction("移到数据图层", self)
            act_to_data.triggered.connect(lambda: self._move_layer_to_group(self._selected_layer, self._data_group))
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
                center.x() - half,
                center.y() - half,
                center.x() + half,
                center.y() + half,
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
        from qgis.PyQt.QtWidgets import QInputDialog

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
            copy = QgsVectorLayer(f"{geom}?crs={layer.crs().authid()}", f"{layer.name()}_副本", "memory")
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
            try:
                ext = xform.transformBoundingBox(layer.extent())
            except QgsCsException:
                # 脏数据(几何坐标范围与声明 CRS 不符)越界时回退到原始范围
                logger.warning(
                    "缩放到图层 %s 越界(%s→%s), 使用原始范围",
                    layer.name(), layer.crs().authid(), dest_crs.authid(),
                )
                ext = layer.extent()
        else:
            ext = layer.extent()
        self.canvas.setExtent(ext)
        self.canvas.refresh()

    def zoom_to_layer_extent_transform(self, extent, source_crs) -> None:
        """缩放到指定范围(属性表右键"缩放到要素"用), 做 CRS 变换到画布坐标系。

        脏几何(坐标范围与声明 CRS 不符, 如把经纬度度值误存进投影图层)在反变换
        时越界抛 ``QgsCsException`` —— 此时回退到原始范围直接缩放, 不崩溃。
        """
        if extent is None or extent.isNull():
            return
        dest_crs = self.canvas.mapSettings().destinationCrs()
        if source_crs is not None and source_crs.isValid() and source_crs != dest_crs:
            xform = QgsCoordinateTransform(source_crs, dest_crs, self.project)
            try:
                extent = xform.transformBoundingBox(extent)
            except QgsCsException:
                logger.warning(
                    "缩放到要素越界(%s→%s), 使用原始范围",
                    source_crs.authid(), dest_crs.authid(),
                )
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
            group.addLayer(layer)  # 先挂新节点，保证图层仍在树里
            node.parent().takeChild(node)  # 再摘原节点（bridge 不会删图层）
            self.messageBar.pushMessage(
                f"已把 {layer.name()} 移到「{group.name()}」", level=Qgis.MessageLevel.Info, duration=2
            )

    def _sweep_startup_gpkg_aliases(self) -> None:
        """启动清扫(延迟到 UI 就绪后跑一次): 删上一会话残留的 .gpkg 别名。

        仅老 ``.db`` 兼容路径会产生别名; 新规 ``.gpkg`` 本体库在 `_is_our_alias`
        闸门(需同 stem ``.db`` 为 gpkg 内容)下不会被误删。建别名的数据目录
        在上一会话已记进 QGIS settings(见 local_db_util
        ``sweep_startup_gpkg_aliases``)——退出/崩溃后目录里不留别名文件,
        下次加载某老库时由 ``gpkg_open_path`` 自动重建。正在被工程引用的
        别名(若启动即恢复工程)不会误删。
        """
        try:
            from app.core.util.local_db_util import sweep_startup_gpkg_aliases

            n = sweep_startup_gpkg_aliases()
            if n:
                logger.info("启动清扫: 已清理 %d 个残留 .gpkg 别名", n)
        except Exception:  # noqa: BLE001
            logger.debug("启动清扫失败", exc_info=True)

    def _release_gpkg_alias_retry(self, alias: str, attempt: int) -> None:
        """尽力删除已无活图层引用的 .gpkg 别名(分段重试, 最长约 3s)。

        QGIS 移除图层后异步析构、释放 GDAL 句柄需要几个事件循环拍; 期间
        别名可能仍被占用(WinError 32)。"删除"是尽力而为——仍失败就留给
        下一启动的清扫兜底。"""
        from qgis.PyQt.QtCore import QTimer

        try:
            from app.core.util.local_db_util import release_gpkg_alias

            if release_gpkg_alias(alias):
                return
        except Exception:  # noqa: BLE001
            logger.debug("释放别名异常 %s", alias, exc_info=True)
            return
        if attempt >= 6:
            return
        QTimer.singleShot(500, lambda: self._release_gpkg_alias_retry(alias, attempt + 1))

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
        from qgis.PyQt.QtCore import QTimer

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
            snap_tb = getattr(self, "snapping_toolbar", None)
            if snap_tb is not None:
                try:
                    snap_tb.set_current_layer(None)
                except Exception:  # noqa: BLE001
                    pass

        # 真正从 project 摘掉
        self.project.removeMapLayer(layer_id)
        # 若该图层是经 .gpkg 别名打开的老 .db gpkg 内容库(源为 “x.gpkg” /
        # “x.gpkg|layername=t”), 移除后别名已无活引用 → 延迟删除它。
        # 新规 .gpkg 本体库也以 .gpkg 结尾, 但 release_gpkg_alias 内部有
        # _is_our_alias 闸门(需同 stem .db 存在且为 gpkg 内容) → 不会误删。
        src = str(getattr(layer, "source", lambda: "")() or "")
        alias_candidate = src.split("|", 1)[0].strip()
        if alias_candidate.lower().endswith(".gpkg"):
            QTimer.singleShot(0, lambda: self._release_gpkg_alias_retry(alias_candidate, 0))
        # 延迟刷新画布到下一个事件循环 tick，让 layersRemoved 信号链走完
        QTimer.singleShot(0, self._deferred_refresh_after_remove)
        self.messageBar.pushMessage(f"已移除图层：{layer_name}", level=Qgis.MessageLevel.Info, duration=3)

    def _deferred_refresh_after_remove(self) -> None:
        """延迟到事件循环下一拍才刷新画布（避免半移除状态 native 崩溃）。"""
        try:
            self.canvas.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _choose_edit_layer(self) -> "Optional[QgsVectorLayer]":
        """「开始编辑」的选层弹窗：列出工程中可添加要素的矢量图层供选择。

        只负责**选择并返回**（取消返回 None）——把选中层落地为编辑目标
        （edit_layer / 停旧层 / 工具条 / 磁性捕捉联动）由工具条的
        :meth:`DigitizingToolbar.start_edit_fun` 统一调用
        :meth:`_apply_edit_target` 完成，避免职责散落。

        编辑图层入口已收敛到这里——图层树右键「设为编辑图层」与点击自动预设
        均已移除。
        """
        candidates = [
            layer
            for layer in self.project.mapLayers().values()
            if isinstance(layer, QgsVectorLayer)
            and not layer.readOnly()
            and bool(
                layer.dataProvider()
                and layer.dataProvider().capabilities()
                & QgsVectorDataProvider.Capability.AddFeatures
            )
        ]
        if not candidates:
            QMessageBox.information(
                self, "提示", "工程中没有可编辑的矢量图层，请先加载数据"
            )
            return None
        # 默认高亮：当前编辑图层 → 图层树选中项 → 第一个
        if self.edit_layer in candidates:
            default = self.edit_layer
        elif self._selected_layer in candidates:
            default = self._selected_layer
        else:
            default = candidates[0]

        dlg = QDialog(self)
        dlg.setWindowTitle("选择编辑图层")
        dlg.resize(380, 420)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("请选择需要编辑的图层："))
        list_widget = QListWidget(dlg)
        for layer in candidates:
            list_widget.addItem(layer.name())
        list_widget.setCurrentRow(candidates.index(default))
        layout.addWidget(list_widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)
        picked = None

        def on_ok() -> None:
            nonlocal picked
            row = list_widget.currentRow()
            if 0 <= row < len(candidates):
                picked = candidates[row]
            dlg.accept()

        buttons.accepted.connect(on_ok)
        buttons.rejected.connect(dlg.reject)
        list_widget.itemDoubleClicked.connect(lambda *_: on_ok())
        dlg.exec()
        return picked

    def _apply_edit_target(self, layer: "Optional[QgsVectorLayer]") -> None:
        """把 ``layer`` 设为编辑目标图层并联动工具条 / 磁性捕捉。

        仅指定目标（记录 ``self.edit_layer`` + 刷新按钮），不进入编辑态；真正的
        ``layer.startEditing()`` 由工具条的「开始编辑」执行。若之前有别的图层在
        编辑，先停掉（避免遗留脏 buffer）。
        """
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return
        # 如果之前有别的图层在编辑，先停掉（避免遗留脏 buffer）
        prev = self.edit_layer
        if prev is not None and prev is not layer and prev.isEditable():
            self.editing_service.stop(prev, save=False)

        self.edit_layer = layer
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.set_edit_target(layer)
        snap_tb = getattr(self, "snapping_toolbar", None)
        if snap_tb is not None:
            snap_tb.set_current_layer(layer)
        self.messageBar.pushMessage(
            f"已选 {layer.name()} 为编辑图层",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )

    def _on_layer_clicked_in_dock(self, index) -> None:
        """用户在图层树点击一层：仅记录为 ``_selected_layer``。

        不再自动设为编辑图层——编辑图层统一在点「开始编辑」时弹窗选择
        （见 :meth:`_choose_edit_layer`），避免"点了哪层就编辑哪层"的隐式切换。
        """
        # 注意：本构建没有 ``indexToNode``，只有 ``index2node``（两者都有
        # 的版本里行为一致）；index 直接来自 view 的模型，无需再 map。
        try:
            node = self.layer_dock.view.index2node(index)
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

    # ---------------------------------------------------- 图层树头部工具

    def _current_dock_node(self):
        """图层树当前选中节点；图例项自动上溯到所在图层节点。

        返回 None 表示没有选中项。
        """
        from qgis.core import QgsLayerTreeGroup

        view = self.layer_dock.view
        node = None
        try:
            node = view.currentNode()
        except Exception:  # noqa: BLE001
            node = None
        if node is None:
            try:
                index = view.currentIndex()
                if index.isValid():
                    node = view.index2node(index)
            except Exception:  # noqa: BLE001
                node = None
        if node is None:
            return None
        # 图例项（symbol 行）的父是图层节点，不是组：上溯到图层节点为止
        while node.parent() is not None and not isinstance(node.parent(), QgsLayerTreeGroup):
            node = node.parent()
        return node

    def _on_header_style_panel(self) -> None:
        """头部「打开图层样式面板」：给当前选中图层打开样式编辑。

        本应用无常驻样式 Dock，与图层右键菜单一致复用模态样式编辑器
        （:meth:`_show_style_editor`）。
        """
        try:
            layer = self.layer_dock.view.currentLayer()
        except Exception:  # noqa: BLE001
            layer = None
        if layer is None:
            layer = getattr(self, "_selected_layer", None)
        if layer is None:
            self.messageBar.pushMessage(
                "请先在图层列表中选择要设置样式的图层",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        self._show_style_editor(layer)

    def _on_header_remove(self) -> None:
        """头部「删除图层/组」：当前选中节点（图层或组）。

        - 选中图层 → 直接走 :meth:`_remove_layer`
        - 选中组 → 确认后递归移除组里全部图层（图层清空组自然消失）
        """
        from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer

        node = self._current_dock_node()
        if node is None:
            self.messageBar.pushMessage(
                "请先在图层列表中选择要删除的图层或组",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return

        if isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            if layer is not None:
                self._remove_layer(layer)
            return

        if isinstance(node, QgsLayerTreeGroup):
            # findLayers 默认递归收集所有子图层（嵌套子组下的也算）
            layers = [c.layer() for c in node.findLayers() if c.layer() is not None]
            if not layers:
                self.messageBar.pushMessage("选中的图层组为空", level=Qgis.MessageLevel.Info, duration=2)
                return
            ret = QMessageBox.question(
                self,
                "删除图层组",
                f"确定删除「{node.name()}」及其 {len(layers)} 个图层吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            for layer in layers:
                self._remove_layer(layer)
            return

        # 图例项等其它节点：当作无效选择
        self.messageBar.pushMessage(
            "请选择图层或图层组再删除",
            level=Qgis.MessageLevel.Warning,
            duration=3,
        )

    def _on_header_expand_all(self) -> None:
        """头部「全部展开」：展开整个图层树。"""
        self.layer_dock.view.expandAll()

    def _on_header_collapse_all(self) -> None:
        """头部「全部折叠」：折叠整个图层树（保留根节点）。"""
        self.layer_dock.view.collapseAll()

    def _on_add_layer_group(self) -> None:
        """新建图层组（与 QGIS 一致）：选中分组 → 建在该组最顶；

        选中图层 → 建在所在分组最顶；未选中 → 建在根最顶。新建后选中并尝试
        就地重命名（失败则保留默认名）。
        """
        from qgis.core import QgsLayerTreeGroup

        node = self._current_dock_node()
        if isinstance(node, QgsLayerTreeGroup):
            parent = node
        elif node is not None and isinstance(node.parent(), QgsLayerTreeGroup):
            parent = node.parent()
        else:
            parent = self.project.layerTreeRoot()

        # 默认名去重（父分组内查重）
        base = "新建图层组"
        name = base
        i = 2
        while parent.findGroup(name) is not None:
            name = f"{base} {i}"
            i += 1

        group = QgsLayerTreeGroup(name)
        parent.insertChildNode(0, group)
        view = self.layer_dock.view
        # QGIS 行为：新建组后选中并直接进入重命名。两者都只在视图已聚焦时做
        # （未显示/未聚焦的视图上强设"当前节点"或开编辑器，会在该节点随后被
        # 移除时令 Qt 持有失效持久索引，触发堆损坏 0xc0000374）。
        if not view.hasFocus():
            self.messageBar.pushMessage(f"已新建图层组：{name}", level=Qgis.MessageLevel.Info, duration=2)
            return
        try:
            index = view.node2index(group)
            if index.isValid():
                view.setCurrentIndex(index)
        except Exception:  # noqa: BLE001
            pass
        try:
            index = view.node2index(group)
            if index.isValid():
                view.edit(index)
        except Exception:  # noqa: BLE001
            pass
        self.messageBar.pushMessage(f"已新建图层组：{name}", level=Qgis.MessageLevel.Info, duration=2)

    def _on_move_layer_node(self, delta: int) -> None:
        """上移 / 下移当前选中的图层或组（仅在同分组内重排）。

        严格按 QGIS 桌面 ``qgslayertreeviewdefaultactions.cpp`` 里
        ``moveToTop / moveToBottom`` 的写法：**clone + insert + remove** 三步走，
        **不是** ``takeChild + insertChildNode``。

        为何不能直接 takeChild：``takeChild`` 不会发 ``willRemoveChildren`` 信号，
        ``QgsLayerTreeRegistryBridge`` 收不到"将要删节点"的钩子，于是直接
        ``project.removeMapLayer(layer)`` 把 layer 从工程中抹掉——肉眼看上去
        就是"图层消失"。

        正确顺序：先 ``insertChildNode`` 把克隆挂到新位置（layer 立刻在新位置
        有 node），再 ``removeChildNode`` 删旧 node（bridge 收到
        willRemoveChildren 时检查到 layer 还有 cloned 那个 node 挂着，**不**删
        layer，只删旧 node）。**insert 位置要按方向偏移一格**：

        - ``delta < 0``（上移）：原节点在 ``new_pos`` 之后，插到 ``new_pos`` 即
          落在目标位置之前，删原节点后自然到位。
        - ``delta > 0``（下移）：原节点在 ``new_pos`` 之前还占着位，插到
          ``new_pos`` 会被推到原节点**之后**的目标位，删原节点后又落回原
          ``pos``——下移等于没动。必须插到 ``new_pos + 1``（end-append 越界由
          上面 ``new_pos < len(siblings)`` 已挡住）。

        clone 是新对象，``view.setCurrentNode`` / messageBar 都用 cloned。
        """
        from qgis.core import QgsLayerTreeGroup

        node = self._current_dock_node()
        if node is None or node.parent() is None:
            self.messageBar.pushMessage(
                "请先在图层列表中选择要移动的图层或组",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return
        parent = node.parent()
        if not isinstance(parent, QgsLayerTreeGroup):
            return
        siblings = parent.children()
        try:
            pos = siblings.index(node)
        except ValueError:
            return
        new_pos = pos + delta
        if not 0 <= new_pos < len(siblings):
            return  # 已在顶/底

        cloned = node.clone()
        # 1) 先把克隆挂到新位置（layer 立即在树里有 node，bridge 不会干 layer）。
        #    下移时原节点还占着 ``pos``，insert 必须跳到 ``new_pos + 1``，
        #    否则删旧节点后 cloned 落回 ``pos``，下移等于没动（见 docstring）。
        insert_pos = new_pos + (1 if delta > 0 else 0)
        parent.insertChildNode(insert_pos, cloned)
        # 2) 再删旧 node（bridge 收到 willRemoveChildren 时发现 layer 还有
        #    cloned 引用，只摘旧 node，不删 layer）
        parent.removeChildNode(node)

        view = self.layer_dock.view
        try:
            view.setCurrentNode(cloned)
        except Exception:  # noqa: BLE001
            pass
        verb = "上移" if delta < 0 else "下移"
        self.messageBar.pushMessage(
            f"已{verb}：{getattr(cloned, 'name', lambda: '')()}",
            level=Qgis.MessageLevel.Info,
            duration=2,
        )

    # ---------------------------------------------------- 绘图工具回调（保留兼容）

    def _on_draw_finished(self, geometry) -> None:
        """DrawManager.finished 信号：跳到 DigitizingToolbar.finish_draw。"""
        if hasattr(self, "digitizing_toolbar"):
            self.digitizing_toolbar.finish_draw(geometry)

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

            dlg = QgsVectorLayerProperties(canvas=self.canvas, lyr=layer, messageBar=self.messageBar, parent=self)
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
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            # 对话框内部已把图层写出（exec 的 OK 槽触发保存）；记日志
            out = dlg.fileName() or ""
            logger.info("导出图层：%s → %s", layer.name(), out)

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
            self.current_attribute_dock = AttributeDock(
                layer, page_size=50, parent=self, filter_str=filter_str, show_ids=show_ids
            )
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.current_attribute_dock)
            self.current_attribute_dock.show()
        else:
            self.current_attribute_dock.add_new_layer(layer, self, filter_str, show_ids, 50, f"属性表 - {layer.name()}")

    # ===================================================== 关闭

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            # ZoomTileGuard 先停：冻结期间若直接关窗，worker 线程还在跑离屏渲染，
            # 关窗口会触发 QPainter/QImage 析构，可能撞上 native 崩溃。
            guard = getattr(self, "_zoom_tile_guard", None)
            if guard is not None:
                try:
                    guard.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self.project.clear()
        finally:
            self._clear_global_messagebar()
            event.accept()

    def _clear_global_messagebar(self, *args) -> None:
        """清理全局 ``messageBar`` 属性（防悬空指针 native 崩溃）。

        构造时把 ``self.messageBar`` 塞进 ``QgsApplication`` 全局属性（兼容旧
        ``sendMessageToTopParent``）。如果窗口销毁而不清理，属性里残留的
        ``QgsMessageBar*`` 就是悬空指针——之后任何 ``send()`` 访问该属性都会
        access violation（实测：测试里 MainWindow 被 GC 后，reproject 的
        ``send("success", ...)`` 在 ``property("messageBar")`` 处崩溃）。

        ``closeEvent`` 与 ``destroyed`` 信号都走这里；两者分别覆盖「用户主动
        关闭」和「被 GC / deleteLater 销毁」两条路径。
        """
        qgis_app = QgsApplication.instance()
        if qgis_app is None:
            return
        try:
            qgis_app.setProperty("messageBar", None)
        except RuntimeError:
            pass  # 对象已部分销毁，属性残留交给 Qt 自身兜底
