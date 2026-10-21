"""顶部 chrome、状态栏与主题。

标题栏/模式切换、主题 QSS 应用、用户信息、状态栏与图层加载指示，
以及顶部各工具条（通用/编辑/文件/查看/质检/深度学习）与 Tab 切换器。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from contextlib import suppress

from qgis.core import Qgis
from qgis.gui import (
    QgsMapToolPan,
    QgsMapToolZoom,
)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import (
    QAction,
    QColor,
    QKeySequence,
    QShortcut,
)
from qgis.PyQt.QtWidgets import QApplication, QDialog, QDockWidget, QMessageBox, QToolBar

from app.infra.logging import get_logger
from app.infra.shortcuts import DEFAULT_SHORTCUTS, effective_sequences, load_overrides
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
from app.ui.tools.pan_zoom import ZoomClickTool

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
            "create_data": self._on_create_data,
            "offline_download": self._on_offline_download,
            "zoom_full": self._on_zoom_full,
            "refresh": self._on_refresh,
            # 「测试」按钮已按用户要求从工具条移除（_TOOL_GROUPS 里不再有它），
            # 回调留着，需要时把那行加回去即可恢复按钮
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
        # 侧栏「缩放」（单击放大 / Shift+单击缩小）：与顶部「放大 / 缩小」是两个入口
        self._side_zoom_tool = ZoomClickTool(self.canvas)
        #: 启动测量前的常规画布工具（再次点测量按钮取消使用时还原）
        self._measure_previous_tool = None

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
        self._deep_learning_toolbar = self._build_deep_learning_toolbar()

        #: tab 索引 → 该 tab 下应显示的工具条列表（切换时互斥显示）
        self._tab_toolbars: dict[int, list] = {}
        #: 「深度学习」tab 索引（切走该 tab 时自动退出纯净地图模式）
        self._deep_learning_tab_index = -1
        #: 「通用」tab 索引（标注页关闭后回到普通地图工作区）
        self._general_tab_index = -1
        #: 纯净地图模式是否激活（由 tab 切换驱动）
        self._deep_learning_active = False
        for label, toolbars in (
            ("文件", [self._file_toolbar]),
            ("查看", [self._view_toolbar]),
            ("编辑", [
                self.digitizing_toolbar.digitizing_toolbar,
                self.snapping_toolbar.toolbar,
            ]),
            ("通用", [self.tool_bar]),
            ("质检", [self._quality_toolbar]),
            # 「深度学习」tab 本身即模式开关：切到该 tab 清空地图组件，切走即恢复，
            # 见 _on_tab_changed / _set_deep_learning_active；工具条只放业务入口
            ("深度学习", [self._deep_learning_toolbar]),
        ):
            index = tab_bar.addTab(label)
            self._tab_toolbars[index] = toolbars
            if label == "深度学习":
                self._deep_learning_tab_index = index
            elif label == "通用":
                self._general_tab_index = index
            for tb in toolbars:
                tb.setStyleSheet(toolbar_qss(ThemeManager.instance().current()))

        tab_bar.currentChanged.connect(self._on_tab_changed)
        self._on_tab_changed(tab_bar.currentIndex())

    def _build_file_toolbar(self) -> QToolBar:
        """「文件」tab 工具条：新建工程 / 保存工程 / 另存为工程 / 打开工程 / 打开文件 / 退出。"""
        tb = QToolBar("文件", self)
        tb.setObjectName("FileTabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        act_new = QAction("新建工程", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._on_new_project)
        tb.addAction(act_new)
        self._act_new_project = act_new

        act_save = QAction("保存工程", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._save_current_project)
        tb.addAction(act_save)
        self._act_save_project = act_save

        act_save_as = QAction("另存为工程", self)
        act_save_as.setShortcut("Ctrl+Shift+S")
        act_save_as.triggered.connect(self._on_save_project_as)
        tb.addAction(act_save_as)
        self._act_save_project_as = act_save_as

        act_open_proj = QAction("打开工程", self)
        act_open_proj.setShortcut("Ctrl+Shift+O")
        act_open_proj.triggered.connect(self._on_open_project)
        tb.addAction(act_open_proj)
        self._act_open_project = act_open_proj

        act_open = QAction("打开文件", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._on_open_file)
        tb.addAction(act_open)
        self._act_open_file = act_open

        tb.addSeparator()

        act_exit = QAction("退出", self)
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.close)
        tb.addAction(act_exit)
        self._act_exit = act_exit

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
        self._act_log = act_log

        act_flow = QAction("流程节点编辑器", self)
        act_flow.setShortcut("Ctrl+Shift+F")
        act_flow.triggered.connect(self._open_flow_editor)
        tb.addAction(act_flow)
        self._act_flow_editor = act_flow

        self.addToolBar(tb)
        return tb

    # ===================================================== 全局快捷键

    def _shortcuts_config_path(self):
        """用户自定义快捷键的落盘位置（与 ``app.yaml`` 同目录）。"""
        from pathlib import Path

        return Path(self._map_config_path()).with_name("shortcuts.yaml")

    def _make_toggle_shortcut(self, button, *extra_buttons):
        """给工具条开关按钮挂一个**窗口级**快捷键（S / T）。

        为什么不用 ``QToolButton.setShortcut``：那条路要求按钮**可见且可用**才
        生效，而捕捉工具条只在「编辑」页签下显示 —— 用户在别的页签按 S 毫无反应
        （实测）。窗口级 QShortcut 不受页签影响，代价是可能抢文本框的按键，
        所以额外按焦点开关，见 :meth:`_sync_toggle_shortcuts`。
        """
        if button is None:
            return None
        shortcut = QShortcut(self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        buttons = (button, *extra_buttons)
        shortcut.activated.connect(
            lambda bs=buttons: [b.toggle() for b in bs if b is not None]
        )
        return shortcut

    def _sync_toggle_shortcuts(self, *_args) -> None:
        """焦点在文本输入里时禁用 S / T，免得打字被快捷键吃掉。"""
        from qgis.PyQt.QtWidgets import (
            QAbstractSpinBox,
            QLineEdit,
            QPlainTextEdit,
            QTextEdit,
        )

        widget = QApplication.focusWidget()
        typing = isinstance(
            widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)
        )
        # 标注 / 训练页当前时同样禁用：S/T 是窗口级快捷键，会和页面内快捷键抢
        blocked = bool(
            getattr(self, "_annotation_page_visible", False)
            or getattr(self, "_training_page_visible", False)
            or getattr(self, "_inference_page_visible", False)
        )
        for attr in ("_snap_shortcut", "_trace_shortcut", "_imagery_shortcut"):
            shortcut = getattr(self, attr, None)
            if shortcut is not None:
                shortcut.setEnabled(not typing and not blocked)

    def _set_annotation_shortcuts_blocked(self, blocked: bool) -> None:
        """标注页当前时禁用主窗口同键 action。

        只把页面 action 改成 ``WidgetWithChildrenShortcut`` 不够：同一按键同时命中
        主窗口的 ``WindowShortcut`` 时 Qt 判为 ambiguous，**两个都不触发**。这里
        记录进入前的 enabled 状态，离开页面时原样恢复。
        """
        targets = getattr(self, "_shortcut_targets", {})
        keys = (
            "new_project",
            "save_project",
            "save_project_as",
            "open_project",
            "open_file",
            "undo",
            "redo",
        )
        if blocked:
            if not getattr(self, "_annotation_shortcut_state", None):
                self._annotation_shortcut_state = {
                    key: targets[key].isEnabled()
                    for key in keys
                    if targets.get(key) is not None
                }
            for key in keys:
                target = targets.get(key)
                if target is not None:
                    target.setEnabled(False)
        else:
            for key, enabled in getattr(self, "_annotation_shortcut_state", {}).items():
                target = targets.get(key)
                if target is not None:
                    target.setEnabled(enabled)
            self._annotation_shortcut_state = {}
        self._sync_toggle_shortcuts()

    def _init_shortcuts(self) -> None:
        """登记全系统快捷键并套用用户自定义值（启动时调一次）。

        登记表见 :data:`app.infra.shortcuts.DEFAULT_SHORTCUTS`；这里只把 ``key``
        映射到实际控件（QAction / QShortcut），值怎么套由
        :meth:`set_app_shortcut` 统一处理。
        """
        redo_shortcuts = list(getattr(self, "_redo_shortcuts", []))
        snap_tb = getattr(self, "snapping_toolbar", None)
        self._snap_shortcut = self._make_toggle_shortcut(
            getattr(snap_tb, "_enable_btn", None)
        )
        self._trace_shortcut = self._make_toggle_shortcut(
            getattr(snap_tb, "_trace_btn", None)
        )
        # 显隐首个影像图层：没有工具条按钮，纯键位（默认不绑定，用户在面板里录）
        self._imagery_shortcut = QShortcut(self)
        self._imagery_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._imagery_shortcut.activated.connect(self._toggle_first_imagery)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._sync_toggle_shortcuts)
        self._shortcut_targets: dict = {
            "undo": getattr(self, "_undo_shortcut", None),
            "redo": redo_shortcuts[0] if len(redo_shortcuts) > 0 else None,
            "new_project": getattr(self, "_act_new_project", None),
            "save_project": getattr(self, "_act_save_project", None),
            "save_project_as": getattr(self, "_act_save_project_as", None),
            "open_project": getattr(self, "_act_open_project", None),
            "open_file": getattr(self, "_act_open_file", None),
            "exit_app": getattr(self, "_act_exit", None),
            "log_dock": getattr(self, "_act_log", None),
            "flow_editor": getattr(self, "_act_flow_editor", None),
            # 捕捉 / 追踪走**窗口级 QShortcut**，不用 QToolButton.setShortcut
            # （后者要求按钮可见，只在「编辑」页签下才显示 —— 别的页签按 S 没反应）
            "snapping_toggle": self._snap_shortcut,
            "tracing_toggle": self._trace_shortcut,
            "toggle_first_imagery": self._imagery_shortcut,
        }
        overrides = load_overrides(self._shortcuts_config_path())
        for key, sequence in effective_sequences(overrides).items():
            self.set_app_shortcut(key, sequence)
        # 编辑设置项（常规页 / 系统设置组）：读进单例，各处按需取用
        from app.infra.edit_settings import EditSettingsManager, load_settings

        EditSettingsManager.instance().apply(
            load_settings(self._edit_settings_config_path())
        )
        # 选中高亮样式（「图层属性 → 选择」页配置）：读进单例并广播一次 ——
        # 覆盖层可能已用默认色建好，不广播它不会知道自己该换色（load 内部发 changed）
        from app.ui.common.selection_style import SelectionStyleManager

        SelectionStyleManager.instance().load(self._selection_style_config_path())

    def set_app_shortcut(self, key: str, sequence: str) -> None:
        """把 ``key`` 对应控件的快捷键设为 ``sequence``（空串 = 不绑定）。

        QAction 走 ``setShortcut``；撤销 / 重做是画布上作用域受限的 QShortcut
        （避免抢文本框的 Ctrl+Z），走 ``setKey``，空串时一并 ``setEnabled(False)``。
        """
        target = getattr(self, "_shortcut_targets", {}).get(key)
        if target is None:
            return
        if isinstance(target, QShortcut):
            target.setKey(QKeySequence(sequence) if sequence else QKeySequence())
            target.setEnabled(bool(sequence))
            return
        target.setShortcut(QKeySequence(sequence) if sequence else QKeySequence())

    def _edit_settings_config_path(self):
        """编辑设置文件与快捷键文件同目录（都在 map config 旁边）。"""
        from pathlib import Path

        return Path(self._map_config_path()).with_name("edit_settings.yaml")

    def _selection_style_config_path(self):
        """选中高亮样式文件，与编辑设置同目录（都在 map config 旁边）。"""
        from pathlib import Path

        return Path(self._map_config_path()).with_name("selection_style.yaml")

    def apply_edit_settings_panel(self) -> None:
        """「编辑设置」按钮：弹面板（常规 / 快捷键两页）编辑，保存后立即生效并落盘。

        快捷键页只展示**编辑工具栏下的工具快捷键**（见 ``EDIT_TOOL_SHORTCUTS``），但
        这里传进去的是**全量**当前值、收尾也按全量落盘 —— 否则一次保存就会把
        工程 / 退出 / 日志等项的覆盖值从配置文件里抹掉。
        """
        from app.infra.edit_settings import (
            EditSettingsManager,
            load_settings,
            save_settings,
        )
        from app.infra.shortcuts import diff_from_defaults, save_overrides
        from app.ui.dialogs.edit_settings import EditSettingsDialog

        current = {
            spec.key: self._current_shortcut_text(spec.key, spec.default)
            for spec in DEFAULT_SHORTCUTS
        }
        manager = EditSettingsManager.instance()
        dlg = EditSettingsDialog(current, manager.current(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        sequences = dlg.sequences()
        for key, sequence in sequences.items():
            self.set_app_shortcut(key, sequence)
        # 编辑设置项：立即生效（各处按需读单例），再落盘
        new_settings = dlg.settings()
        manager.apply(new_settings)
        try:
            save_settings(self._edit_settings_config_path(), new_settings)
        except Exception:  # noqa: BLE001 —— 写盘失败不该回滚已生效的设置
            logger.exception("保存编辑设置失败")
            self.messageBar.pushMessage(
                "编辑设置已生效，但写入配置文件失败（下次启动会回到上次保存的值）",
                level=Qgis.MessageLevel.Warning,
            )
        try:
            save_overrides(self._shortcuts_config_path(), diff_from_defaults(sequences))
        except Exception:  # noqa: BLE001 —— 写盘失败不该回滚已生效的快捷键
            logger.exception("保存快捷键配置失败")
            self.messageBar.pushMessage(
                "快捷键已生效，但写入配置文件失败（下次启动会回到默认）",
                level=Qgis.MessageLevel.Warning,
            )
            return
        self.messageBar.pushMessage("编辑设置已保存并生效")

    def _current_shortcut_text(self, key: str, default: str) -> str:
        """当前生效的键序列文本（读控件本身，保证面板显示的就是真实值）。"""
        target = getattr(self, "_shortcut_targets", {}).get(key)
        if isinstance(target, QShortcut):
            return target.key().toString(QKeySequence.SequenceFormat.PortableText)
        if target is not None:
            return target.shortcut().toString(QKeySequence.SequenceFormat.PortableText)
        return default

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

    def _build_deep_learning_toolbar(self) -> QToolBar:
        """「深度学习」tab 工具条：标注 / 训练 / 推理业务入口 + 统一设置。"""
        tb = QToolBar("深度学习", self)
        tb.setObjectName("DeepLearningTabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        for text, tip in (
            ("标注", "对样本数据进行标注"),
            ("训练", "训练深度学习模型"),
            ("推理", "用训练好的模型对新影像做预测"),
            ("设置", "统一管理标注 / 训练 / 推理的外部依赖、SAM 模型与缓存"),
        ):
            if text == "设置":
                tb.addSeparator()
            act = QAction(text, self)
            act.setToolTip(tip)
            # triggered 会带 bool 参数，用默认参数把按钮文案绑进去
            act.triggered.connect(
                lambda _checked=False, name=text: self._on_deep_learning_tool(name)
            )
            tb.addAction(act)

        self.addToolBar(tb)
        return tb

    def _on_deep_learning_tool(self, name: str) -> None:
        """「标注 / 训练 / 推理 / 设置」入口。"""
        if name == "标注":
            opener = getattr(self, "_open_annotation_page", None)
            if opener is not None:
                opener()
                return
            QMessageBox.warning(self, "深度学习", "标注窗口不可用（缺少入口方法）")
            return
        if name == "训练":
            opener = getattr(self, "_open_training_page", None)
            if opener is not None:
                opener()
                return
            QMessageBox.warning(self, "深度学习", "训练窗口不可用（缺少入口方法）")
            return
        if name == "推理":
            opener = getattr(self, "_open_inference_page", None)
            if opener is not None:
                opener()
                return
            QMessageBox.warning(self, "深度学习", "推理窗口不可用（缺少入口方法）")
            return
        if name == "设置":
            opener = getattr(self, "_open_deep_learning_settings", None)
            if opener is not None:
                opener()
                return
            QMessageBox.warning(self, "深度学习", "设置窗口不可用（缺少入口方法）")
            return
        QMessageBox.information(self, "深度学习", f"「{name}」功能正在开发中")

    def _on_tab_changed(self, index: int) -> None:
        """tab 切换：只显示当前 tab 对应的工具条，其余隐藏（模拟"换工具栏"）。

        「深度学习」tab 本身即纯净地图模式开关：切到它 → 隐藏地图画布、停靠面板
        与悬浮侧栏；切到任何其它 tab → 原样恢复（见 :meth:`_set_deep_learning_active`）。

        工具条显隐后由 ``QMainWindow`` 自己发 ``LayoutRequest`` 重算中央区，正常情况
        画布顶边依次为 213 / 89 / 159 / 89（编辑 / 文件 / 通用 / 查看），**不要**手动
        ``layout.activate()`` 插队——那只会在下面这种布局停摆的场景里帮倒忙。
        """
        self._set_deep_learning_active(
            index == getattr(self, "_deep_learning_tab_index", -1)
        )
        deep = index == getattr(self, "_deep_learning_tab_index", -1)
        if deep:
            # 回到「深度学习」页签：恢复最后活动的工作区（标注 / 训练 / 推理）
            if (
                getattr(self, "_last_deep_learning_page", "") == "training"
                and getattr(self, "_training_page_open", False)
            ):
                self._show_training_page()
            elif (
                getattr(self, "_last_deep_learning_page", "") == "inference"
                and getattr(self, "_inference_page_open", False)
            ):
                self._show_inference_page()
            elif getattr(self, "_annotation_page_open", False):
                self._show_annotation_page()
        elif getattr(self, "_central_stack", None) is not None:
            # 离开工作区：切回地图页，页面实例保留（下次进入可恢复）
            self._central_stack.setCurrentWidget(self._map_page)
            self._annotation_page_visible = False
            self._training_page_visible = False
            self._inference_page_visible = False
            self._set_annotation_shortcuts_blocked(False)
            self.canvas.show()
        for idx, toolbars in self._tab_toolbars.items():
            visible = idx == index
            for tb in toolbars:
                tb.setVisible(visible)
        # 兜底：布局一旦停摆（见 :meth:`heal_stalled_layout`），这里是用户最先看出来的地方
        self.heal_stalled_layout()

    # ================================================= 深度学习模式

    def _on_deep_learning(self) -> None:
        """「分析工具」面板的「深度学习」按钮：切到「深度学习」tab 即进入模式。

        模式完全由 tab 驱动（见 :meth:`_on_tab_changed`）：切到「深度学习」tab
        隐藏地图画布、全部停靠面板与悬浮侧栏，切到任何其它 tab 原样恢复。
        面板按钮在进入模式时会随面板一起隐藏，退出统一走「点其它 tab」。
        """
        index = getattr(self, "_deep_learning_tab_index", -1)
        if index >= 0:
            self._tab_bar.setCurrentIndex(index)

    def _set_deep_learning_active(self, active: bool) -> None:
        """按 tab 切换结果进入 / 退出纯净地图模式（幂等），并同步面板按钮状态。"""
        active = bool(active)
        if active != getattr(self, "_deep_learning_active", False):
            self._deep_learning_active = active
            if active:
                self._enter_deep_learning_mode()
            else:
                self._leave_deep_learning_mode()
        dock = getattr(self, "_analyse_dock", None)
        if dock is not None:
            dock.set_deep_learning_active(active)

    def _enter_deep_learning_mode(self) -> None:
        """记录并隐藏所有可见停靠面板、地图画布与悬浮竖排工具条。

        「日志信息」面板例外：深度学习页面不再自动打开它，只有用户主动
        「查看 → 日志信息」时才可见；此时保持原样，避免把用户刚打开的排查入口
        藏掉（LogDock 在不可见时会暂停自动刷新，见 ``LogDock._auto_reload``）。
        """
        log_dock = getattr(self, "log_dock", None)
        page = getattr(self, "_annotation_page", None)
        training = getattr(self, "_training_page", None)
        self._deep_learning_hidden_docks = [
            dock
            for dock in self.findChildren(QDockWidget)
            if dock.isVisible()
            and dock is not log_dock
            # 标注 / 训练页自己的 Dock 属于页面内部布局，不是主窗口的地图组件，
            # 不能一起隐藏
            and not (page is not None and page.isAncestorOf(dock))
            and not (training is not None and training.isAncestorOf(dock))
        ]
        for dock in self._deep_learning_hidden_docks:
            dock.hide()
        canvas = getattr(self, "canvas", None)
        self._deep_learning_canvas_visible = canvas is not None and canvas.isVisible()
        if canvas is not None:
            canvas.hide()
        side = getattr(self, "side_toolbar", None)
        side_widget = getattr(side, "widget", None)
        self._deep_learning_side_visible = (
            side_widget is not None and side_widget.isVisible()
        )
        if side_widget is not None:
            side_widget.hide()
        # 定位气泡浮在中央区上，不属于画布隐藏链，必须单独收起
        overlay = getattr(self, "_coord_locate_overlay", None)
        self._deep_learning_coord_visible = bool(
            overlay is not None and overlay.isVisible()
        )
        if overlay is not None:
            overlay.hide()

    def _leave_deep_learning_mode(self) -> None:
        """恢复进入模式时可见的停靠面板、地图画布与悬浮竖排工具条。

        ``show()`` 后按进入时的 z-order 依次 ``raise_()``：叠放（tabify）的面板里，
        原先在最前的那个（用户当时选中的标签页）保持不变，不会跳回默认页签。
        """
        for dock in getattr(self, "_deep_learning_hidden_docks", []):
            # 面板在模式存续期间被销毁时忽略
            with suppress(RuntimeError):
                dock.show()
                dock.raise_()
        self._deep_learning_hidden_docks = []
        canvas = getattr(self, "canvas", None)
        if canvas is not None and getattr(self, "_deep_learning_canvas_visible", False):
            with suppress(RuntimeError):
                canvas.show()
        side = getattr(self, "side_toolbar", None)
        side_widget = getattr(side, "widget", None)
        if side_widget is not None and getattr(self, "_deep_learning_side_visible", False):
            side_widget.show()
            self._keep_side_toolbar_on_top()
        overlay = getattr(self, "_coord_locate_overlay", None)
        if overlay is not None and getattr(self, "_deep_learning_coord_visible", False):
            with suppress(RuntimeError):
                overlay.show()

    # ================================================= dock 布局停摆的自愈

    def watch_dock_layout(self, dock) -> None:
        """Dock 停靠↔浮动 / 换停靠区后登记一次布局自愈。

        **不能同步做**：``topLevelChanged`` / ``dockLocationChanged`` 是 Qt 在收尾
        dock 拖拽的过程中同步发出来的，此刻布局处在拖拽中间态——这时套
        ``restoreState`` 会把其它浮动 dock 的位置一起搅乱甚至让它们消失。所以这里只
        排一个延迟任务，等鼠标松开、事件循环空下来再执行。
        """
        if dock is None:
            return
        for signal in (dock.topLevelChanged, dock.dockLocationChanged):
            try:
                signal.connect(lambda *_a: self.schedule_layout_heal())
            except Exception:  # noqa: BLE001 —— 非 Dock / 已销毁，忽略
                logger.warning("挂接 Dock 布局信号失败: %s", type(dock).__name__, exc_info=True)

    def schedule_layout_heal(self) -> None:
        """把自愈排到「鼠标已松开」之后（拖拽还在进行时反复重排会更乱）。"""
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            QTimer.singleShot(80, self.schedule_layout_heal)
            return
        self.heal_stalled_layout()

    def _expected_central_top(self) -> int:
        """按「菜单容器 + 可见的顶部工具条」算中央区应有的顶边（实测严丝合缝）。

        顶部**停靠区**（DockWidgetArea.TopDockWidgetArea）也有 dock 时不参与计算——
        那种情况下 ``central.y()`` 本就该比工具条更低，硬比会一路误判成停摆。
        """
        menu = self.menuWidget()
        top = menu.geometry().bottom() + 1 if menu is not None else 0
        for dock in self.findChildren(QDockWidget):
            if dock.isVisible() and self.dockWidgetArea(dock) == Qt.DockWidgetArea.TopDockWidgetArea:
                return -1  # 顶区还有 dock，这套算法不适用（返回哨兵，调用方跳过）
        for tb in self.findChildren(QToolBar):
            if tb.isVisible() and self.toolBarArea(tb) == Qt.ToolBarArea.TopToolBarArea:
                top = max(top, tb.geometry().bottom() + 1)
        return top

    def heal_stalled_layout(self) -> None:
        """中央区顶边跟顶部实际占用对不上时，重套一次 dock 布局把它救回来。

        **救的是什么**：拖 dock 出浮动之后，``QMainWindowLayout`` 会停止重排——
        工具条按 tab 正确显隐，但中央容器几何再也不更新：切 tab 在工具条下方留一条
        空白带，**连最大化 / 拖窗口边缘改尺寸都不动**，只有重启才恢复。

        **为什么会停摆**：``QMainWindowLayout::setGeometry()`` 开头有一道守卫

            if (savedState.isValid() || (restoredState && isInApplyState))
                return;

        拖 dock 时 Qt 用 ``savedState`` 存布局快照，正常由 ``animationFinished()``
        清掉；本项目跑的 Qt 6.11.0 上（见 QTBUG-147209，6.8/6.11 的回归）拖出成浮动
        之后它不再被清，于是 ``setGeometry()`` 永远早返回。

        **为什么这么救**：实测（``.tmp/verify_nudge.py``）能解开的只有
        ``saveState()`` 走一圈 ``restoreState()`` —— 让 Qt 重新套一遍布局状态，顺带把
        卡住的 ``savedState`` 清掉。副作用核对过：健康布局下往返一次，各 dock 的停靠区 /
        浮动状态 / 位置 / 尺寸、以及画布几何**逐项不变**。
        网上另外几种绕法（加个假 dock 再删、``layout().invalidate() + activate()``、
        ``resizeDocks()``、±1px 改窗口尺寸）在本仓库实测**全都无效**，别再试。
        """
        central = self.centralWidget()
        if central is None:
            return
        try:
            expected = self._expected_central_top()
        except RuntimeError:
            return  # 窗口销毁流程中，忽略
        if expected < 0:
            return  # 顶区有 dock，_expected_central_top 不适用
        if abs(central.y() - expected) <= 1:
            return
        logger.warning(
            "顶部布局停摆：中央区 y=%s，按顶部实际占用应为 %s，重套一次 dock 布局",
            central.y(), expected,
        )
        try:
            state = self.saveState()
            if not state.isEmpty():
                self.restoreState(state)
        except RuntimeError:
            pass  # 窗口销毁流程中，忽略

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
        # 所有 tab 的工具条统一走 Ribbon 风格 QSS（构建后 / 切主题时都刷一遍）
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
        # 浮动消息条的外观 QSS 里含主题色，切主题要重贴
        refresh_bar = getattr(self, "_apply_message_bar_appearance", None)
        if callable(refresh_bar):
            refresh_bar()

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
