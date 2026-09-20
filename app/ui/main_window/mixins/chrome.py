"""顶部 chrome、状态栏与主题。

标题栏/模式切换、主题 QSS 应用、用户信息、状态栏与图层加载指示，
以及顶部各工具条（通用/编辑/文件/查看/质检）与 Tab 切换器。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from qgis.gui import (
    QgsMapToolPan,
    QgsMapToolZoom,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import (
    QAction,
    QColor,
    QKeySequence,
)
from qgis.PyQt.QtWidgets import QToolBar

from app.infra.logging import get_logger
from app.ui.common.theme import (
    TOOLBAR_ITEM_GAP,
    ThemeManager,
    toolbar_qss,
    window_qss,
)
from app.ui.main_window.draw_toolbar import DigitizingToolbar
from app.ui.main_window.snapping_toolbar import QgisSnappingToolbar
from app.ui.main_window.status_bar import StatusBar
from app.ui.main_window.title_bar import (
    MODE_PROJECT,
    MODE_TASK,
)
from app.ui.main_window.tool_bar import ToolBar

logger = get_logger(__name__)


class WindowChromeMixin:
    """WindowChromeMixin：见模块 docstring。"""

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
        # 捕捉工具条单独放第二行（编辑工具条占第一行）
        self.insertToolBarBreak(self.snapping_toolbar.toolbar)

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
        self._quality_toolbar = self._build_quality_toolbar()

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
            ("质检", [self._quality_toolbar]),
        ):
            self._tab_toolbars[tab_bar.addTab(label)] = toolbars
            for tb in toolbars:
                tb.setStyleSheet(toolbar_qss(ThemeManager.instance().current()))

        tab_bar.currentChanged.connect(self._on_tab_changed)
        self._on_tab_changed(tab_bar.currentIndex())

    def _build_file_toolbar(self) -> QToolBar:
        """「文件」tab 工具条：新建工程 / 保存为工程 / 打开工程 / 打开文件 / 退出。"""
        tb = QToolBar("文件", self)
        tb.setObjectName("FileTabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        act_new = QAction("新建工程", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._on_new_project)
        tb.addAction(act_new)

        act_save_as = QAction("保存为工程", self)
        act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        act_save_as.triggered.connect(self._on_save_project_as)
        tb.addAction(act_save_as)

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

    def _build_quality_toolbar(self) -> QToolBar:
        """「质检」tab 工具条：当前只有一个「质检」工具，打开质检面板 dock。"""
        tb = QToolBar("质检", self)
        tb.setObjectName("QualityTabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        act_check = QAction("质检", self)
        act_check.setToolTip("打开质检面板")
        act_check.triggered.connect(self._on_quality_dock)
        tb.addAction(act_check)

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

        label = {MODE_PROJECT: "工程模式", MODE_TASK: "任务模式"}.get(mode, mode)
        logger.info("模式切换：%s", label)

    def _apply_theme_qss(self) -> None:
        """按当前主题重绘窗体 chrome（菜单栏/状态栏/Dock）、主工具条和画布底色。"""
        theme = ThemeManager.instance().current()
        self.setStyleSheet(window_qss(theme))
        # 5 个 tab 的工具条统一走 Ribbon 风格 QSS（构建后 / 切主题时都刷一遍）
        tab_toolbars = getattr(self, "_tab_toolbars", None)
        if tab_toolbars:
            for toolbars in tab_toolbars.values():
                for tb in toolbars:
                    tb.setStyleSheet(toolbar_qss(theme))
                    self._set_toolbar_gap(tb)
        else:
            tb = getattr(self, "tool_bar", None)
            if tb is not None:
                tb.setStyleSheet(toolbar_qss(theme))
                self._set_toolbar_gap(tb)
        # 工具条图标是 QPainter 画的，颜色固定烧在 PNG 里，切主题时需重刷
        tbb = getattr(self, "tool_bar_builder", None)
        if tbb is not None:
            tbb.apply_theme(theme)
        # 左侧竖排工具条图标同步重刷
        side_tb = getattr(self, "side_toolbar", None)
        if side_tb is not None:
            side_tb.apply_theme(theme)
        # 编辑工具条图标同步重刷
        dig_tb = getattr(self, "digitizing_toolbar", None)
        if dig_tb is not None:
            dig_tb.apply_theme(theme)
        # 画布底色跟随主题（setCanvasColor，不走 QSS）
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.setCanvasColor(QColor(theme.canvas_bg))
            canvas.refresh()

    def _on_theme_changed(self, theme) -> None:
        self._apply_theme_qss()

    @staticmethod
    def _set_toolbar_gap(tb: QToolBar) -> None:
        """程序化设工具栏项间距：QSS ``spacing`` 首次布局时可能未生效，见 theme.TOOLBAR_ITEM_GAP。"""
        layout = tb.layout()
        if layout is not None:
            layout.setSpacing(TOOLBAR_ITEM_GAP)

    def _apply_user_info_to_titlebar(self) -> None:
        """把本次登录会话（用户信息 + token 有效期）挂到标题栏。

        登录信息**不再落盘**（没有本地凭证），来源是 :func:`app.bootstrap.main`
        登录成功后传给 :class:`~app.ui.main_window.main_window.MainWindow` 的
        ``auth_result``；没传（如非启动路径构造窗口）就显示"未登录"。

        ``auth_result.user`` 是登录后 ``currentUserByToken`` 拉到的当前用户
        （姓名 / 单位 / 部门 / 上次登录）；拉取失败时为 ``None``，标题栏回退
        只显示登录账号。
        """
        result = getattr(self, "_auth_result", None)
        if result is None:
            self.title_bar.set_user("未登录")
        else:
            self.title_bar.set_user(
                result.username,
                expires_at=result.expires_at(),
                user=getattr(result, "user", None),
            )
