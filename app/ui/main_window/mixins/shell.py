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
from qgis.PyQt import sip
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
    QDockWidget,
    QHBoxLayout,
    QShortcut,
    QSizePolicy,
    QWidget,
)

from app.infra.logging import get_logger
from app.infra.messagebar import FloatingMessageBar
from app.ui.common.map_canvas import AppMapCanvas
from app.ui.common.selection_overlay import SelectionOverlay
from app.ui.common.theme import ThemeManager
from app.ui.main_window.browser_dock import BrowserDock
from app.ui.main_window.coord_locate_overlay import CoordLocateOverlay
from app.ui.main_window.layer_dock import LayerDock
from app.ui.main_window.side_toolbar import SideToolBar

logger = get_logger(__name__)


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
        # 编辑工具（点选/框选 Delete、要素编辑、合并、分割、属性录入）每个动作都
        # 是一条 beginEditCommand…endEditCommand 记录，存在图层 undoStack；这里
        # 把快捷键接到「当前编辑图层」的撤销/重做上（QGIS 删除选中同一套语义）。
        # 作用域限定画布子树（WidgetWithChildrenShortcut）：焦点在画布时才生效，
        # 不抢 dock / 对话框文本输入框的原生 Ctrl+Z（画布本身无文本输入）。
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), canvas)
        self._undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._undo_shortcut.activated.connect(self._undo_edit)
        self._redo_shortcuts = [QShortcut(QKeySequence(seq), canvas) for seq in ("Ctrl+Shift+Z", "Ctrl+Y")]
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
        # 走 set_appearance 而不是 setStyleSheet：QgsMessageBar 每次 push 都会把
        # 自带样式表盖回来，得由消息条在每次 push 后重新贴一遍。
        self.messageBar.set_appearance(
            "#FloatingMessageBar {"
            "  background-color: rgba(255, 255, 255, 240);"
            "  border: 1px solid rgba(120, 120, 120, 120);"
            "  border-radius: 6px;"
            "}"
            "#FloatingMessageBar QLabel { color: #202020; }"
            # 条目内的文字框是 QTextBrowser（QTextEdit 的子类），会被全局「输入框」
            # 规则套上 1px 边框 —— 浮动提示里看着就是个带框的输入框。在消息条作用域
            # 内去掉边框与底色，只留文字。
            "#FloatingMessageBar QTextBrowser, #FloatingMessageBar QTextEdit {"
            "  border: none; background: transparent;"
            "}"
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

        # ---- 选中要素置顶覆盖层 ----
        # QGIS 自带的选中高亮画在**各图层自己的渲染流程里**，下层被选中的图形会
        # 被上层图层 / 同层后画的图形盖住。画布橡皮筋在所有图层之后绘制，用它把
        # 选中几何重画一遍，保证"选中就看得见"。
        self.selection_overlay = SelectionOverlay(self.canvas, parent=self)
        self.selection_overlay.attach(self.project)
        self.destroyed.connect(lambda *_a: self.selection_overlay.detach())

        # 左侧竖排工具条：已实现工具接主窗口回调，未实现的由 SideToolBar 占位
        # 提示开发中；控件本体悬浮装配见 :meth:`_attach_side_toolbar_overlay`
        self.side_toolbar = SideToolBar(
            self,
            {
                "select_point": self._on_select_point,
                "select_box": self._on_select_box,
                "pan": self._on_pan,
                "side_zoom": self._on_side_zoom,
                "zoom_full": self._on_zoom_full,
                "identify": self._on_identify,
                "clear_selection": self._on_clear_selection,
                "select_mask": self._on_select_mask,
                "unmask": self._on_unmask,
                "condition_query": self._on_condition_query,
                "qmap_compare": self._on_qmap_compare,
                "coord_locate": self._on_coord_locate,
                "measure_line": self._on_measure_line,
                "measure_area": self._on_measure_area,
                "extent_clip": self._on_extent_clip,
            },
        )
        layout.addWidget(self.canvas)
        # 竖排工具条悬浮在地图画布左上角（left 4px / top 3px）
        self._attach_side_toolbar_overlay()
        # 坐标定位气泡悬浮在地图画布右上角；侧栏「坐标定位」按钮点亮时弹出
        self._attach_coord_locate_overlay()

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

    # ===================================================== 画布右上角坐标定位气泡

    def _attach_coord_locate_overlay(self) -> None:
        """把坐标定位气泡悬浮到画布右上角（与侧栏工具条互为对角覆盖层）。

        装配模式与 :meth:`_attach_side_toolbar_overlay` 一致：setParent 到画布
        父容器（与画布同级）→ raise_() 永远盖在画布及其所有子覆盖层之上。
        初始隐藏，由侧栏「坐标定位」按钮的回调 :meth:`_on_coord_locate` 唤起。
        """
        self._coord_locate_overlay = CoordLocateOverlay(self.canvas)
        # 父容器优先：有父才能提到画布**之上**（与侧栏同款）
        host = self.canvas.parentWidget() or self.canvas
        self._coord_locate_overlay.setParent(host)
        self._coord_locate_overlay.adjustSize()
        self._coord_locate_overlay.hide()
        # 用户点气泡 × 或外部 toggle 关闭时，同步侧栏按钮高亮
        self._coord_locate_overlay.closed.connect(self._on_coord_locate_overlay_closed)
        # 重绘 / 范围变化时跟随画布重摆位置 + 置顶（与侧栏同样的 keep-on-top 思路）
        self.canvas.mapCanvasRefreshed.connect(self._place_coord_locate_overlay)
        self.canvas.extentsChanged.connect(self._place_coord_locate_overlay)

    def _place_coord_locate_overlay(self) -> None:
        """把坐标定位气泡摆到画布右上角（与侧栏左上角互为对角，互不遮挡）。"""
        overlay = getattr(self, "_coord_locate_overlay", None)
        canvas = getattr(self, "canvas", None)
        if overlay is None or canvas is None:
            return
        # 隐藏态不摆位（避免 0 高度窗口闪一下）
        if not overlay.isVisible():
            return
        host = overlay.parentWidget()
        if host is None:
            return
        try:
            # 画布左上角在 host 坐标系里的位置
            origin = canvas.mapToGlobal(QPoint(0, 0)) - host.mapToGlobal(QPoint(0, 0))
        except RuntimeError:
            return  # 窗口关闭流程中 C++ 对象已销毁
        # 悬浮子控件首次 show() 后可能 height()==0；先 adjustSize 兜底
        if overlay.width() <= 0 or overlay.height() <= 0:
            overlay.adjustSize()
        margin = 8
        overlay.move(
            origin.x() + canvas.width() - overlay.width() - margin,
            origin.y() + margin,
        )
        overlay.raise_()

    def _on_coord_locate(self) -> None:
        """侧栏「坐标定位」按钮回调：toggle 气泡显示，按钮随显隐独立常亮。

        与 :meth:`_on_qmap_compare` 一致——按钮在 :data:`SideToolBar._SESSION_KEYS`
        里，由 :meth:`SideToolBar.set_session_active` 控高亮。
        """
        overlay = getattr(self, "_coord_locate_overlay", None)
        if overlay is None:
            return
        # overlay.toggle() 内部会 emit closed（如已显示则关闭）；先同步好侧栏按钮再
        # 让 overlay.open() / close() 自己在显隐后 emit closed 做"用户主动关闭"通知
        side = getattr(self, "side_toolbar", None)
        is_open = overlay.is_open()
        overlay.toggle()
        new_open = overlay.is_open()
        if side is not None:
            side.set_session_active("coord_locate", new_open)
        # 第一次打开时（或画布尺寸变了）气泡首次可见，需要摆位
        if new_open and not is_open:
            self._place_coord_locate_overlay()

    def _on_coord_locate_overlay_closed(self) -> None:
        """用户点 × 关闭气泡：同步把侧栏按钮弹起（与 toggle 关闭对称）。"""
        side = getattr(self, "side_toolbar", None)
        if side is not None:
            side.set_session_active("coord_locate", False)

    def _build_docks(self) -> None:
        # 1. 图层树
        self.layer_dock = LayerDock(self)
        self.layer_dock.context_menu_requested.connect(self._on_layer_context_menu)
        # 点击图层树即自动设为编辑目标（与 QGIS 桌面一致）：
        # 用户期望点完一层就能在工具条点"开始编辑"
        self.layer_dock.layer_clicked.connect(self._on_layer_clicked_in_dock)
        # 行尾「标注」开关（圆点 + 标注二字）：开关该图层标注（独立开关，不互斥）
        self.layer_dock.label_toggle_requested.connect(self._on_layer_label_toggle)
        # 头部工具条：渲染设置（自建对话框 → 渲染 tab）/ 添加图层组 / 删除 / 全部展开 / 全部折叠 / 上移 / 下移
        self.layer_dock.render_tab_requested.connect(self._on_header_render_tab)
        self.layer_dock.add_group_requested.connect(self._on_add_layer_group)
        self.layer_dock.remove_requested.connect(self._on_header_remove)
        self.layer_dock.expand_all_requested.connect(self._on_header_expand_all)
        self.layer_dock.collapse_all_requested.connect(self._on_header_collapse_all)
        self.layer_dock.move_up_requested.connect(lambda: self._on_move_layer_node(-1))
        self.layer_dock.move_down_requested.connect(lambda: self._on_move_layer_node(1))
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.layer_dock)
        self.watch_dock_layout(self.layer_dock)

        # 2. 数据浏览器
        self.browser_dock = BrowserDock(self)
        self.browser_dock.double_clicked.connect(self._on_browser_double_clicked)
        self.browser_dock.context_menu_requested.connect(self._on_browser_context_menu)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.browser_dock)
        self.watch_dock_layout(self.browser_dock)

        # 3. 日志（默认隐藏，由菜单唤起）
        self.log_dock = None  # 懒加载

    # ===================================================== 右侧 Dock 停靠

    def place_right_dock(self, dock) -> None:
        """把 ``dock`` 停靠到右侧；右侧已有面板时与之**叠放**（占满整条右侧）。

        Qt 的 ``addDockWidget`` 在同一区域默认是**上下堆叠**：先点「属性」再点
        「属性录入」，第二个会挤在第一个下面、各占一半高，两个都看不清。这里
        ``tabifyDockWidget`` + ``raise_()``：同一时刻只显示一个、占满整条右侧，
        点各自的按钮（或标签页）即可切换。

        **所有停靠到右侧的面板都应走本方法**，不要直接 ``addDockWidget``。
        """
        area = Qt.DockWidgetArea.RightDockWidgetArea
        placed = [
            other
            for other in getattr(self, "_right_docks", [])
            if other is not dock and not sip.isdeleted(other)
        ]
        self._right_docks = [*placed, dock]
        self.addDockWidget(area, dock)
        # 拖出浮动后 Qt 会停摆、不再重排（见 heal_stalled_layout），挂上自愈
        self.watch_dock_layout(dock)
        for other in placed:
            try:
                self.tabifyDockWidget(other, dock)
            except Exception:  # noqa: BLE001 —— 叠放失败只会退化成堆叠，不该中断
                logger.exception("右侧面板叠放失败: %s", type(dock).__name__)
        dock.show()
        dock.raise_()

    def place_bottom_dock(self, dock) -> None:
        """把 ``dock`` 停靠到底部；底部已有面板时与之**叠放**（占满整条底部）。

        与 :meth:`place_right_dock` 同一套逻辑、同一套理由：Qt 的 ``addDockWidget``
        在同一区域默认是**并排**——底部区会把两个面板拆成左右各一半宽，两个都看不清
        （右侧区则是上下各一半高）。这里 ``tabifyDockWidget`` + ``raise_()``：同一
        时刻只显示一个、占满整条底部，点各自的标签页切换。

        **所有停靠到底部的面板都应走本方法**，不要直接 ``addDockWidget``。

        叠放目标取「当前真的停在底部区、且没浮动」的面板——直接扫现役成员，不用登记
        表：用户可以把「工具 / 要素信息 / 在线底图」自己拖到底部（这几个都允许
        Bottom），登记表认不全它们。
        """
        area = Qt.DockWidgetArea.BottomDockWidgetArea
        self.addDockWidget(area, dock)
        # 拖出浮动后 Qt 会停摆、不再重排（见 heal_stalled_layout），挂上自愈
        self.watch_dock_layout(dock)
        for other in self.findChildren(QDockWidget):
            if other is dock or sip.isdeleted(other):
                continue
            if other.isFloating() or self.dockWidgetArea(other) != area:
                continue  # 不在底部区（多半还在右侧），别把新面板拽过去叠
            try:
                self.tabifyDockWidget(other, dock)
            except Exception:  # noqa: BLE001 —— 叠放失败只会退化成并排，不该中断
                logger.exception("底部面板叠放失败: %s", type(dock).__name__)
        dock.show()
        dock.raise_()

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
        if hasattr(bar, "has_message") and not bar.has_message():
            # 用户点 × 把条目关掉了。C++ 侧删条目没有对外信号（见
            # app/infra/messagebar/floating_bar.py 模块 docstring），我们这份
            # 「有消息」状态不会跟着变；借重排的时机对一次账，否则画布一变窄
            # （例如打开右侧某个 dock）就会把空条重新 show() 出来——空条没有条目、
            # 也就没有 × 可点，用户关不掉，只能重启。
            bar.dismiss()
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
                self._place_coord_locate_overlay()
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
