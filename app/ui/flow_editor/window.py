"""流程编排：主窗口。

三栏布局：左侧节点库 dock / 中央画布（:class:`FlowView`）/ 右侧参数面板 dock，
底部日志 dock；顶部工具条覆盖运行 / 单步 / 重置 / JSON 导入导出 / 依赖跳转 /
缩放等操作。持有 :class:`FlowExecutor` 并把其信号转成画布节点状态 + 日志。
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QAction, QBrush, QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QToolBar,
)

from app.core.domain.flow.executor import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    FlowExecutor,
    FlowNodeExecutionError,
    FlowNodeSkip,
)
from app.core.domain.flow.model import FlowGraph, unbound_inputs
from app.core.domain.flow.serialization import FlowCodec
from app.core.domain.flow.topo import FlowCycleError, topo_sort
from app.ui.common import tool_icons
from app.ui.common.frameless import FramelessMixin
from app.ui.common.icons import Icons
from app.ui.common.theme import ThemeManager, toolbar_qss, window_qss

from .log_panel import LogDock
from .node_library import NodeLibraryDock
from .param_panel import ParamPanelDock
from .run_params_dialog import RunParamsDialog
from .scene import FlowScene
from .view import FlowView


class FlowEditorWindow(FramelessMixin, QMainWindow):
    """流程节点编辑器主窗口。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("流程节点编辑器")
        self.setWindowIcon(Icons.app())  # 与主窗口同款应用图标
        self.resize(1280, 820)
        # 无边框窗口 + 自绘标题栏（与主窗口同款边框，但只保留
        # 图标/标题/最小化/最大化/关闭；用户信息、主题切换、模式切换不进流程编辑器边框）
        self.title_bar = self.enable_frameless(
            show_user_button=False,
            show_theme_button=False,
            show_mode_switch=False,
        )
        self._dirty = False
        self._current_file = ""

        # ---- 中央画布
        self.scene = FlowScene()
        self.view = FlowView(self.scene, self)
        self.setCentralWidget(self.view)
        # Linux/X11 边缘缩放：FlowView 覆盖整个窗口，注册为缩放监控目标
        self.view.installEventFilter(self)
        self.monitor_resize_edges(self.view)

        # ---- 执行器（QObject，信号驱动 UI）
        self.executor = FlowExecutor(self)
        self._wire_executor()
        # ---- 增量运行驱动：QTimer 每滴答执行一个节点，中间把事件循环
        # 让给 Qt，节点状态 / 日志才能随执行进度实时刷新（而不是跑完才刷新）。
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(0)
        self._run_timer.timeout.connect(self._advance_run)

        # ---- 三侧 dock
        self.node_library = NodeLibraryDock(self)
        self.param_panel = ParamPanelDock(self)
        self.log_panel = LogDock(self)
        self.param_panel.set_scene(self.scene)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.node_library)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.param_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_panel)
        self.resizeDocks([self.node_library, self.param_panel], [260, 320], Qt.Orientation.Horizontal)

        self._build_toolbar()
        self._wire_scene()
        # 主题：跟随全局 ThemeManager，切换时重绘窗体 chrome / 工具条 / 画布
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._apply_theme_qss()
        self.statusBar().showMessage("就绪")
        self._update_title()

    # ------------------------------------------------------------ 装配

    def _build_toolbar(self) -> None:
        tb = QToolBar("流程工具", self)
        tb.setMovable(False)
        # 与主窗口 ToolBar 同款：纯图标按钮（QPainter 画 FA 图标，随主题变色）
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        tb.setIconSize(QSize(25, 25))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self._toolbar = tb
        #: 已建按钮 + 图标 key，主题切换时用它重刷图标颜色
        self._toolbar_icons: list[tuple[QAction, str]] = []
        color = ThemeManager.instance().current().toolbar_text

        def act(text: str, slot, icon_key: str, tip: str = "", shortcut: str = "") -> QAction:
            a = QAction(text, self)
            a.setIcon(tool_icons.icon(icon_key, color))
            a.setToolTip(tip or text)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            tb.addAction(a)
            self._toolbar_icons.append((a, icon_key))
            return a

        self.act_run = act("运行全部", self._run_flow, "play", "按拓扑序执行全部节点", "F5")
        self.act_step = act("单步执行", self._run_selected_node, "step", "只执行当前选中的节点（上游用缓存）")
        self.act_reset = act("重置参数", self._reset_params, "undo", "清空默认值 / 输出配置并断开全部连线")
        tb.addSeparator()
        self.act_validate = act("拓扑校验", self._validate_flow, "sitemap", "检查是否存在循环依赖")
        self.act_highlight = act("高亮依赖链", self._toggle_highlight, "link", "高亮选中节点的上下游链路")
        self.act_highlight.setCheckable(True)
        self.act_prev = act("上一级", self._go_up, "chevron_up", "跳到选中节点的直接上游")
        self.act_next = act("下一级", self._go_down, "chevron_down", "跳到选中节点的直接下游")
        self.act_delete = act("删除选中", self._delete_selected, "trash", "删除选中的节点 / 连线")
        tb.addSeparator()
        self.act_new = act("新建流程", self._new_flow, "file", "清空画布新建流程")
        self.act_open = act("打开…", self._open_flow, "folder_open", "从 JSON 加载流程")
        self.act_save = act("保存", self._save_flow, "save", "保存为 JSON")
        self.act_save_as = act("另存为…", self._save_flow_as, "archive", "另存为 JSON")
        tb.addSeparator()
        self.act_zoom_in = act("放大", self._zoom_in, "zoom_in", "放大画布")
        self.act_zoom_out = act("缩小", self._zoom_out, "zoom_out", "缩小画布")
        self.act_fit = act("适应窗口", self._fit_view, "expand", "让全部节点适配窗口")

    def _wire_scene(self) -> None:
        self.scene.node_selected.connect(self._on_node_selected)
        self.scene.node_double_clicked.connect(self._on_rename_requested)
        self.scene.status_message.connect(self._on_status_message)
        self.scene.graph_changed.connect(self._mark_dirty)
        self.view.template_dropped.connect(self.scene.add_template)
        self.param_panel.run_node_requested.connect(self._run_node)

    def _wire_executor(self) -> None:
        self.executor.flow_started.connect(self._on_flow_started)
        self.executor.flow_finished.connect(self._on_flow_finished)
        self.executor.node_started.connect(self._on_node_started)
        self.executor.node_finished.connect(self._on_node_finished)
        self.executor.node_skipped.connect(self._on_node_skipped)

    # ------------------------------------------------------------ 主题

    def _apply_theme_qss(self) -> None:
        """按当前全局主题重绘窗体 chrome（菜单/状态栏/Dock）、工具条与画布。

        与主窗口 :meth:`MainWindow._apply_theme_qss` 同构：本窗口是顶层
        QMainWindow，不吃主窗体 QSS，需要自己套 ``window_qss``；弹窗 / 右键
        菜单 / ToolTip 仍由 QApplication 级的 ``app_qss`` 覆盖。
        """
        theme = ThemeManager.instance().current()
        self.setStyleSheet(window_qss(theme))
        tb = getattr(self, "_toolbar", None)
        if tb is not None:
            tb.setStyleSheet(toolbar_qss(theme))
            # 图标由 QPainter 把颜色烧进像素，主题切换时按新 toolbar_text 重建
            color = theme.toolbar_text
            for a, key in self._toolbar_icons:
                a.setIcon(tool_icons.icon(key, color))
        # 画布底色与节点/连线配色跟随主题（QGraphicsView 不吃 QSS 背景）
        self.scene.apply_theme(theme)
        self.view.setBackgroundBrush(QBrush(QColor(theme.window_bg)))
        self.param_panel.apply_theme()

    def _on_theme_changed(self, theme) -> None:
        self._apply_theme_qss()

    # ------------------------------------------------------------ 场景联动

    def _on_node_selected(self, node_id: str) -> None:
        if node_id:
            self.param_panel.set_node(node_id)
        else:
            self.param_panel.clear()

    def _on_rename_requested(self, node_id: str) -> None:
        node = self.scene.graph.nodes.get(node_id)
        if node is None:
            return
        text, ok = QInputDialog.getText(
            self, "重命名节点", "新名称：", QLineEdit.EchoMode.Normal, node.name
        )
        if ok and text.strip():
            self.scene.rename_node(node_id, text.strip())

    def _on_status_message(self, msg: str) -> None:
        self.log_panel.log(msg, "info")
        self.statusBar().showMessage(msg)

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_title()

    # ------------------------------------------------------------ 执行器联动

    def _on_flow_started(self, planned: int) -> None:
        self.scene.reset_statuses()
        self.log_panel.log(f"开始运行流程（计划执行 {planned} 个节点）", "info")

    def _on_node_started(self, node_id: str, name: str) -> None:
        self.scene.apply_statuses({node_id: STATUS_RUNNING})
        self.log_panel.log(f"▶ 节点「{name}」开始执行", "info")

    def _on_node_finished(self, node_id: str, name: str, ok: bool, ms: float, summary: str, error: str) -> None:
        status = STATUS_SUCCESS if ok else STATUS_FAILED
        self.scene.apply_statuses({node_id: status})
        if ok:
            self.log_panel.log(f"✔ 节点「{name}」完成（{ms:.0f} ms） {summary}", "success")
        else:
            self.log_panel.log(f"✘ 节点「{name}」失败：{error}", "error")

    def _on_node_skipped(self, node_id: str, name: str, reason: str) -> None:
        self.scene.apply_statuses({node_id: STATUS_SKIPPED})
        self.log_panel.log(f"⏭ 节点「{name}」跳过：{reason}", "skip")

    def _on_flow_finished(self, statuses: dict) -> None:
        self.scene.apply_statuses(statuses)
        ok_n = sum(1 for s in statuses.values() if s == STATUS_SUCCESS)
        fail_n = sum(1 for s in statuses.values() if s == STATUS_FAILED)
        skip_n = sum(1 for s in statuses.values() if s in (STATUS_DISABLED, STATUS_SKIPPED))
        self.log_panel.log(f"流程结束：成功 {ok_n}，失败 {fail_n}，跳过 {skip_n}", "info")

    # ------------------------------------------------------------ 工具条动作

    def _run_flow(self) -> None:
        if self.executor.is_running:
            self.log_panel.log("流程正在运行中，请稍候…", "warn")
            return
        self.log_panel.clear()
        overrides = self._collect_run_overrides()
        if overrides is None:
            return  # 用户取消填写
        try:
            self.executor.prepare_run(self.scene.graph, overrides)
        except FlowCycleError as exc:  # noqa: BLE001
            self.log_panel.log(f"流程有环：{exc}", "error")
            QMessageBox.warning(self, "无法运行", f"流程中存在循环依赖：\n{exc}")
            return
        self._set_running(True)
        # interval=0：事件队列一空就执行下一个节点，节点变绿后立即重绘
        self._run_timer.start()

    def _advance_run(self) -> None:
        """QTimer 滴答：执行下一个节点；出错或跑完则收尾。

        真正的执行仍在主线程（QGIS processing / 图层入工程都不是线程安全的），
        但每次只跑一个节点就归还控制权，所以界面不会被整条流程卡住。
        """
        try:
            done = self.executor.step_run()
        except FlowNodeExecutionError as exc:
            self._finish_run()
            self.log_panel.log(f"流程中止：{exc}", "error")
            QMessageBox.critical(self, "运行失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._finish_run()
            self.log_panel.log(f"运行出错：{exc}", "error")
            return
        if done:
            self._finish_run()

    def _finish_run(self) -> None:
        """停止滴答并发 ``flow_finished`` 收尾（失败时状态里已有 failed）。"""
        self._run_timer.stop()
        self.executor.finish_run()
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        """运行期间禁掉「运行全部 / 单步」，避免重复触发 / 重入。"""
        self.act_run.setEnabled(not running)
        self.act_step.setEnabled(not running)
        if not running:
            self.statusBar().showMessage("就绪")

    def _collect_run_overrides(self) -> dict | None:
        """运行前填写未绑定输入的参数；用户取消返回 None。

        没有需要填写的输入时不弹窗、直接返回空字典。填写值只影响本次运行，
        不写回图模型 / JSON。
        """
        if not unbound_inputs(self.scene.graph):
            return {}
        dlg = RunParamsDialog(self.scene.graph, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.overrides()

    def _run_selected_node(self) -> None:
        if self.executor.is_running:
            self.log_panel.log("流程正在运行中，请稍候…", "warn")
            return
        node_id = self.scene.current_selected_node()
        if node_id is None:
            self.log_panel.log("请先在画布选中要单步执行的节点", "warn")
            return
        self._run_node(node_id)

    def _run_node(self, node_id: str) -> None:
        if self.executor.is_running:
            self.log_panel.log("流程正在运行中，请稍候…", "warn")
            return
        try:
            self.executor.run_node(self.scene.graph, node_id)
        except FlowNodeSkip as exc:  # noqa: BLE001
            self.scene.apply_statuses({node_id: STATUS_SKIPPED})
            self.log_panel.log(f"⏭ 节点跳过：{exc}", "skip")
        except FlowNodeExecutionError as exc:  # noqa: BLE001
            self.log_panel.log(str(exc), "error")
        except ValueError as exc:  # noqa: BLE001
            self.log_panel.log(f"无法单步执行：{exc}", "warn")

    def _reset_params(self) -> None:
        ret = QMessageBox.question(
            self, "重置参数",
            "确定重置所有节点的输入默认值 / 输出配置，并断开全部连线吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.scene.graph.reset_defaults()
        self.scene.rebuild()
        self.param_panel.refresh()
        self.log_panel.log("已重置全部参数并断开连线", "info")
        self._mark_dirty()

    def _validate_flow(self) -> None:
        try:
            order = topo_sort(self.scene.graph)
        except FlowCycleError as exc:  # noqa: BLE001
            self.log_panel.log(f"校验失败：{exc}", "error")
            QMessageBox.warning(self, "拓扑校验", f"存在循环依赖：\n{exc}")
            return
        self.log_panel.log(
            f"拓扑校验通过：{len(order)} 个节点 → {' → '.join(order)}", "success"
        )
        QMessageBox.information(self, "拓扑校验", f"校验通过，共 {len(order)} 个节点。")

    def _toggle_highlight(self, checked: bool) -> None:
        if checked:
            node_id = self.scene.current_selected_node()
            if node_id:
                self.scene.highlight_chain(node_id)
            else:
                self.log_panel.log("请先选中一个节点再高亮依赖链", "warn")
                self.act_highlight.setChecked(False)
        else:
            self.scene.clear_highlight()

    def _go_up(self) -> None:
        self.scene.select_ancestor()

    def _go_down(self) -> None:
        self.scene.select_descendant()

    def _delete_selected(self) -> None:
        self.scene.delete_selected()

    def _zoom_in(self) -> None:
        self.view.zoom_in()

    def _zoom_out(self) -> None:
        self.view.zoom_out()

    def _fit_view(self) -> None:
        self.view.fit_to_view()

    # ------------------------------------------------------------ JSON 导入导出

    def _new_flow(self) -> None:
        if not self._confirm_discard():
            return
        self.scene.set_graph(FlowGraph())
        self._current_file = ""
        self._dirty = False
        self.param_panel.clear()
        self.log_panel.clear()
        self._update_title()
        self.log_panel.log("已新建空白流程", "info")

    def _open_flow(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开流程配置", "", "流程配置 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
            graph, warnings = FlowCodec.from_json(text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "打开失败", f"无法读取流程配置：\n{exc}")
            return
        self.scene.set_graph(graph)
        self._current_file = path
        self._dirty = False
        self.param_panel.clear()
        for w in warnings:
            self.log_panel.log(w, "warn")
        if warnings:
            QMessageBox.warning(self, "导入提示", "\n".join(warnings))
        self._update_title()
        self.log_panel.log(f"已打开 {path}", "info")

    def _save_flow(self) -> bool:
        if not self._current_file:
            return self._save_flow_as()
        self._write_json(self._current_file)
        return True

    def _save_flow_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "保存流程配置", "", "流程配置 (*.json);;所有文件 (*)")
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".json"
        self._current_file = path
        self._write_json(path)
        return True

    def _write_json(self, path: str) -> None:
        Path(path).write_text(FlowCodec.to_json(self.scene.graph), encoding="utf-8")
        self._dirty = False
        self.log_panel.log(f"已保存 {path}", "success")
        self._update_title()

    # ------------------------------------------------------------ 关闭 / 标题

    def _update_title(self) -> None:
        name = Path(self._current_file).name if self._current_file else "未命名"
        mark = " *" if self._dirty else ""
        self.setWindowTitle(f"流程节点编辑器 - {name}{mark}")

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        ret = QMessageBox.question(
            self, "未保存的更改",
            "流程尚未保存，是否先保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if ret == QMessageBox.StandardButton.Save:
            return self._save_flow()
        return ret == QMessageBox.StandardButton.Discard

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        self._run_timer.stop()  # 关闭时别再滴答执行节点
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()
