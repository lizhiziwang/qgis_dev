"""主窗口装配：画布、中心控件与停靠区。

画布与 central widget 的搭建、Qt 停靠区挂载、侧边工具条悬浮层的摆放，
以及悬浮消息条的重排与画布拖拽添加图层（``eventFilter`` / ``_on_canvas_drop``）。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import (
    QEvent,
    QPoint,
    Qt,
)
from qgis.PyQt.QtGui import (
    QColor,
    QKeySequence,
)
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QShortcut,
    QSizePolicy,
    QWidget,
)

from app.infra.messagebar import FloatingMessageBar
from app.ui.common.map_canvas import AppMapCanvas
from app.ui.common.theme import ThemeManager
from app.ui.main_window.browser_dock import BrowserDock
from app.ui.main_window.layer_dock import LayerDock
from app.ui.main_window.side_toolbar import SideToolBar


class WindowShellMixin:
    """WindowShellMixin：见模块 docstring。"""

    # ===================================================== 装配

    def _build_canvas(self) -> QgsMapCanvas:
        # AppMapCanvas：开图层渲染缓存；Python 侧 refresh() 仍为「清缓存 + 整幅重画」
        # （点选/框选/取消选择改选中集只重画脏图层，实测 260ms -> 13ms）
        canvas = AppMapCanvas(self)
        canvas.setAcceptDrops(True)
        # 拦截浏览器拖出的图层 URI，走与双击一致的添加路径（原生 drop 会把图层
        # 直接挂到工程根节点，不进"数据图层"分组、也不设 path_c）
        canvas.installEventFilter(self)
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

    def _build_central_widget(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        # 横向布局：地图画布占满中央区域（左侧竖排工具条不再占一列，改为悬浮
        # 盖在画布左上角，见 :meth:`_attach_side_toolbar_overlay`）
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

        # 左侧竖排工具条：已实现工具接主窗口回调，未实现的由 SideToolBar 占位
        # 提示开发中；控件本体悬浮装配见 :meth:`_attach_side_toolbar_overlay`
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
                "qmap_compare": self._on_qmap_compare,
            },
        )
        layout.addWidget(self.canvas)
        # 竖排工具条悬浮在地图画布左上角（left 4px / top 3px）
        self._attach_side_toolbar_overlay()

        crs = QgsCoordinateReferenceSystem("EPSG:4490")
        self.project.setCrs(crs)
        self.canvas.setDestinationCrs(crs)
        self.canvas.setProject(self.project)

    # ===================================================== 画布左上角悬浮工具条

    def _attach_side_toolbar_overlay(self) -> None:
        """把左侧竖排工具条悬浮到画布左上角（left 4px / top 3px）。

        悬浮实现与仓库既定覆盖层模式一致（见
        :class:`app.ui.common.canvas_zoom.CanvasZoomTool` / 悬浮消息条 /
        :mod:`app.ui.tools.swipe`）：

        - 工具条 ``setParent`` 到画布的**父容器**（中央控件）而非画布本身：
          这样它与画布同级，``raise_()`` 后永远盖在画布**及其所有子覆盖层**
          （悬浮消息条 / 卷帘 / 对比分隔条……）之上——挂到画布上时这些同级
          覆盖层会互相抢 z-order，工具条常被后 raise 的消息条等盖住；
        - 坐标须把画布左上角换算到父容器（用全局坐标之差）再偏移 (4, 3)，
          画布 resize / 容器变化时由 :meth:`_keep_side_toolbar_on_top` 重算；
        - 悬浮子控件不受父布局管理，``show()`` 不会自动套用 sizeHint，必须显式
          ``adjustSize()``，否则塌缩成默认高度、15 个按钮全叠在一起。
        """
        side_widget = self.side_toolbar.widget
        host = self.canvas.parentWidget() or self.canvas
        side_widget.setParent(host)
        side_widget.adjustSize()
        self._place_side_toolbar()
        side_widget.show()
        self._keep_side_toolbar_on_top()
        self.canvas.mapCanvasRefreshed.connect(self._keep_side_toolbar_on_top)
        self.canvas.extentsChanged.connect(self._keep_side_toolbar_on_top)

    def _place_side_toolbar(self) -> None:
        """把悬浮工具条摆到画布左上角（left 4px / top 3px）。

        工具条挂在画布的父容器上，因此先把画布左上角换算到该容器坐标系，
        再偏移 (4, 3)；画布缩放 / 容器变化时由
        :meth:`_keep_side_toolbar_on_top` 重算。
        """
        side = getattr(self, "side_toolbar", None)
        widget = getattr(side, "widget", None) if side is not None else None
        canvas = getattr(self, "canvas", None)
        if widget is None or canvas is None:
            return
        host = widget.parentWidget()
        if host is None:
            return
        try:
            # 画布左上角在 host 坐标系里的位置（画布与 host 可能是兄弟/父子，
            # 用全局坐标之差最稳妥）。
            origin = canvas.mapToGlobal(QPoint(0, 0)) - host.mapToGlobal(QPoint(0, 0))
        except RuntimeError:
            return  # 窗口关闭流程中 C++ 对象已销毁
        widget.move(origin.x() + 4, origin.y() + 3)

    def _keep_side_toolbar_on_top(self) -> None:
        """画布重绘后把悬浮工具条重新置顶（跟随画布摆位 + 盖住同级覆盖层）。"""
        side = getattr(self, "side_toolbar", None)
        if side is None:
            return
        try:
            self._place_side_toolbar()
            side.widget.raise_()
        except RuntimeError:
            pass  # 窗口关闭流程中 C++ 对象已销毁

    def _build_docks(self) -> None:
        # 1. 图层树
        self.layer_dock = LayerDock(self)
        self.layer_dock.context_menu_requested.connect(self._on_layer_context_menu)
        # 点击图层树即自动设为编辑目标（与 QGIS 桌面一致）：
        # 用户期望点完一层就能在工具条点"开始编辑"
        self.layer_dock.layer_clicked.connect(self._on_layer_clicked_in_dock)
        # 行尾「标注」开关（圆点 + 标注二字）：开关该图层标注（独立开关，不互斥）
        self.layer_dock.label_toggle_requested.connect(self._on_layer_label_toggle)
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
        """把悬浮消息条摆到画布顶部（左侧让出置顶工具条、高度随内容），仅在有消息时。"""
        bar = getattr(self, "messageBar", None)
        canvas = getattr(self, "canvas", None)
        if bar is None or canvas is None or not getattr(self, "_msg_active", False):
            return
        try:
            # 左侧让出永远置顶的悬浮工具条（画布坐标 (4, 3)、宽 52），消息条从
            # 其右侧开始，避免开头的图标/文字被工具条盖住；顶部 + 右侧各留 6px。
            left = 6
            side = getattr(self, "side_toolbar", None)
            side_widget = getattr(side, "widget", None) if side is not None else None
            if side_widget is not None:
                left = 4 + side_widget.width() + 4
            bar.setGeometry(left, 6, max(canvas.width() - left - 6, 0), bar.sizeHint().height())
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
                self._place_side_toolbar()
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
