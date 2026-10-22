"""流程编排：右侧节点参数面板。

选中节点后显示三块编辑区：

- **通用**：节点名称（重命名）、启用 / 跳过、删除、「选择处理算法」下拉与
  「插入常用算法」按钮（整体替换节点的输入输出与代码）。
- **输入配置**：每行 = 名称 / 类型 / 默认值 / 必填 / 绑定上游（类型不兼容项
  置灰禁用）/ 删除；底部「新增输入」。
- **输出配置**：每行 = 名称 / 类型 / 保存路径（可浏览）/ 写入临时图层 /
  加载到图层面板 / 删除；底部「新增输出」。
- **执行逻辑**：PyQGIS 代码编辑区 + 「单节点运行」。

面板只负责把控件改动写回 :class:`FlowGraph` 并通知场景重建端口；
运行类动作通过信号交给主窗口。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.flow.algorithm_library import (
    BUILTIN_TEMPLATES,
    describe_processing_algorithm,
    list_processing_algorithms,
)
from app.core.domain.flow.model import PortRef
from app.core.domain.flow.types import PortType, can_connect, parse_port_type
from app.infra.logging import get_logger
from app.ui.common.theme import ThemeManager, flow_panel_qss

logger = get_logger(__name__)

#: 常用算法快速插入按钮：模板 key → 显示名
_QUICK_ALGOS = [
    ("buffer", "缓冲区"),
    ("clip", "裁剪"),
    ("reproject", "投影转换"),
    ("reclassify", "重分类"),
    ("field_calc", "字段计算"),
    ("raster_merge", "栅格镶嵌"),
]


class ParamPanelDock(QDockWidget):
    """节点参数面板。"""

    run_node_requested = pyqtSignal(str)  # node_id

    def __init__(self, parent=None) -> None:
        super().__init__("节点参数", parent)
        self.setObjectName("FlowParamPanelDock")
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._scene = None
        self._node_id: str | None = None
        self._building = False  # 重建期间抑制控件信号回调
        self._panel_scroll: QScrollArea | None = None  # 当前内容滚动区（套 QSS 用）
        self._hint = QLabel("在画布中选中一个节点，这里显示它的输入 / 输出 / 执行逻辑。")
        self._hint.setWordWrap(True)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet(f"color:{ThemeManager.instance().current().text_secondary};")
        self.setWidget(self._hint)

    # ------------------------------------------------------------ 对外接口

    def set_scene(self, scene) -> None:
        self._scene = scene

    def apply_theme(self) -> None:
        """主题切换后刷新面板配色（空面板刷提示文字；有内容刷面板 QSS）。"""
        if self._panel_scroll is not None:
            self._panel_scroll.setStyleSheet(
                flow_panel_qss(ThemeManager.instance().current())
            )
        elif self._node_id is None:
            self._hint.setStyleSheet(
                f"color:{ThemeManager.instance().current().text_secondary};"
            )

    def set_node(self, node_id: str) -> None:
        self._node_id = node_id
        self._rebuild()

    def clear(self) -> None:
        self._node_id = None
        self._panel_scroll = None
        self.setWidget(self._hint)

    def refresh(self) -> None:
        """重建当前节点面板（外部结构变化后调用）。"""
        if self._node_id is not None:
            self._rebuild()

    def current_node_id(self) -> str | None:
        return self._node_id

    # ------------------------------------------------------------ 重建

    def _rebuild(self) -> None:
        scene = self._scene
        if scene is None or self._node_id is None:
            return
        node = scene.graph.nodes.get(self._node_id)
        if node is None:
            self.clear()
            return

        container = QWidget(self)
        container.setObjectName("ParamRoot")
        root = QVBoxLayout(container)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._building = True
        try:
            self._build_common(root, node)
            tabs = QTabWidget()
            self._build_inputs_tab(tabs, node)
            self._build_outputs_tab(tabs, node)
            self._build_code_tab(tabs, node)
            root.addWidget(tabs, 1)
        except Exception as exc:  # noqa: BLE001
            # 面板任何一处渲染失败都不应崩掉主窗口
            logger.error("参数面板重建失败 node=%s: %s", self._node_id, exc, exc_info=True)
            root.addWidget(QLabel(f"参数面板渲染失败：{exc}"))
        finally:
            self._building = False

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        self.setWidget(scroll)
        self._panel_scroll = scroll
        self._panel_scroll.setStyleSheet(
            flow_panel_qss(ThemeManager.instance().current())
        )

    # ------------------------------------------------------------ 通用区

    def _build_common(self, root: QVBoxLayout, node) -> None:
        scene = self._scene
        node_id = node.id

        card = QFrame()
        card.setObjectName("ParamCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        title = QLabel("通用设置")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("名称"))
        name_edit = QLineEdit(node.name)
        name_edit.setClearButtonEnabled(True)
        name_edit.editingFinished.connect(
            lambda: scene.rename_node(node_id, name_edit.text().strip()) if name_edit.text().strip() else None
        )
        name_row.addWidget(name_edit, 1)
        v.addLayout(name_row)

        flags_row = QHBoxLayout()
        enabled_cb = QCheckBox("启用")
        enabled_cb.setChecked(node.enabled)
        enabled_cb.toggled.connect(lambda chk: self._on_enabled(node_id, chk))
        flags_row.addWidget(enabled_cb)
        skip_cb = QCheckBox("跳过")
        skip_cb.setChecked(node.skip)
        skip_cb.toggled.connect(lambda chk: self._on_skip(node_id, chk))
        flags_row.addWidget(skip_cb)
        flags_row.addStretch(1)
        v.addLayout(flags_row)

        # 选择处理算法：内置模板 + 处理工具箱算法
        algo_combo = QComboBox()
        algo_combo.addItem("— 选择处理算法 —", "")
        for t in BUILTIN_TEMPLATES.values():
            algo_combo.addItem(f"[内置] {t.name}", t.key)
        for alg_id, name in list_processing_algorithms():
            algo_combo.addItem(name, alg_id)
        idx = algo_combo.findData(node.algorithm_id)
        if idx >= 0:
            algo_combo.setCurrentIndex(idx)
        algo_combo.activated.connect(lambda idx: self._on_algo_selected(node_id, algo_combo.itemData(idx)))
        v.addWidget(algo_combo)

        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        for key, label in _QUICK_ALGOS:
            btn = QPushButton(label)
            btn.setObjectName("ChipButton")
            btn.setToolTip("用该算法替换当前节点的输入 / 输出 / 执行逻辑")
            btn.clicked.connect(lambda _=False, k=key: self._on_algo_selected(node_id, k))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        v.addLayout(quick_row)

        del_btn = QPushButton("删除此节点")
        del_btn.setObjectName("DangerButton")
        del_btn.setToolTip("删除当前节点及其连线")
        del_btn.clicked.connect(lambda: scene.remove_node(node_id))
        v.addWidget(del_btn)

        root.addWidget(card)

    def _on_enabled(self, node_id: str, checked: bool) -> None:
        scene = self._scene
        if scene is None:
            return
        node = scene.graph.nodes.get(node_id)
        if node is None:
            return
        node.enabled = checked
        item = scene.node_item(node_id)
        if item is not None:
            item.setOpacity(0.55 if not checked else 1.0)
            item.update()
        scene.graph_changed.emit()

    def _on_skip(self, node_id: str, checked: bool) -> None:
        scene = self._scene
        if scene is None:
            return
        node = scene.graph.nodes.get(node_id)
        if node is None:
            return
        node.skip = checked
        item = scene.node_item(node_id)
        if item is not None:
            item.update()
        scene.graph_changed.emit()

    def _on_algo_selected(self, node_id: str, key) -> None:
        if not key or self._building:
            return
        scene = self._scene
        if scene is None:
            return
        template = BUILTIN_TEMPLATES.get(key)
        if template is None:
            template = describe_processing_algorithm(key)
        if template is None:
            scene.status_message.emit(f"无法生成算法模板：{key}")
            return
        scene.replace_node_from_template(node_id, template)
        self._rebuild()  # 端口结构已变，刷新面板
        scene.status_message.emit(f"节点已应用算法：{template.name}")

    # ------------------------------------------------------------ 输入配置

    def _build_inputs_tab(self, tabs: QTabWidget, node) -> None:
        scene = self._scene
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        headers = ["名称", "类型", "默认值", "必填", "绑定上游", ""]
        for col, text in enumerate(headers):
            hdr = QLabel(text)
            hdr.setProperty("panelHeader", True)
            grid.addWidget(hdr, 0, col)

        for row, ins in enumerate(node.inputs, start=1):
            self._build_input_row(grid, row, node, ins)

        layout.addLayout(grid)
        layout.addStretch(1)

        add_btn = QPushButton("＋ 新增输入")
        add_btn.setObjectName("AddButton")
        add_btn.setToolTip("给当前节点新增一个输入端口")
        add_btn.clicked.connect(
            lambda: self._on_add_input(node.id) if scene else None
        )
        layout.addWidget(add_btn)

        tabs.addTab(page, "输入配置")

    def _build_input_row(self, grid: QGridLayout, row: int, node, ins) -> None:
        node_id = node.id

        name_edit = QLineEdit(ins.name)
        name_edit.editingFinished.connect(
            lambda: self._rename_input(node_id, ins.id, name_edit.text().strip())
        )
        grid.addWidget(name_edit, row, 0)

        type_combo = self._make_type_combo(ins.type, node_id, ins.id, is_output=False)
        grid.addWidget(type_combo, row, 1)

        default_edit = QLineEdit(ins.default)
        default_edit.setPlaceholderText("默认值")
        default_edit.editingFinished.connect(
            lambda: setattr(ins, "default", default_edit.text())
        )
        grid.addWidget(default_edit, row, 2)

        required_cb = QCheckBox()
        required_cb.setChecked(ins.required)
        required_cb.toggled.connect(lambda chk: setattr(ins, "required", chk))
        grid.addWidget(required_cb, row, 3)

        bind_combo = self._make_bind_combo(node_id, ins)
        grid.addWidget(bind_combo, row, 4)

        del_btn = QToolButton()
        del_btn.setText("✕")
        del_btn.setObjectName("RowDel")
        del_btn.setToolTip("删除此输入")
        del_btn.clicked.connect(
            lambda: self._on_remove_port(node_id, ins.id, is_output=False)
        )
        grid.addWidget(del_btn, row, 5)

    def _make_bind_combo(self, node_id: str, ins):
        """绑定上游下拉：列出其它节点的输出端口，类型不兼容的置灰。"""
        scene = self._scene
        combo = QComboBox()
        combo.addItem("— 断开 —", None)
        current = None
        if scene is not None:
            current = scene.graph.input_binding(node_id, ins.id)
        for nid, outs in scene.graph.upstream_outputs(node_id):
            src_node = scene.graph.nodes[nid]
            label = f"{src_node.name} / {outs.name}（{outs.type.value}）"
            data = (nid, outs.id)
            combo.addItem(label, data)
            combo.model().item(combo.count() - 1).setEnabled(can_connect(outs.type, ins.type))
            if current is not None and current.node_id == nid and current.port_id == outs.id:
                combo.setCurrentIndex(combo.count() - 1)
        combo.activated.connect(
            lambda idx: self._on_bind_selected(node_id, ins, combo.itemData(idx))
        )
        return combo

    def _on_bind_selected(self, node_id: str, ins, data) -> None:
        scene = self._scene
        if scene is None:
            return
        try:
            if data is None:
                scene.graph.unbind_input(node_id, ins.id)
            else:
                src_node, src_port = data
                scene.graph.bind_input(node_id, ins.id, PortRef(src_node, src_port))
            scene._rebuild_edges()
            scene.graph_changed.emit()
        except ValueError as exc:  # noqa: BLE001
            scene.status_message.emit(f"绑定失败：{exc}")
            scene.graph_changed.emit()

    def _rename_input(self, node_id: str, port_id: str, new_name: str) -> None:
        scene = self._scene
        if scene is None or not new_name:
            return
        ins = scene.graph.input_port(node_id, port_id)
        if ins is not None and ins.name != new_name:
            ins.name = new_name
            scene.sync_node(node_id)

    def _on_add_input(self, node_id: str) -> None:
        scene = self._scene
        if scene is None:
            return
        scene.graph.add_input(node_id, "输入", PortType.STRING)
        scene.sync_node(node_id)
        self._rebuild()

    def _on_remove_port(self, node_id: str, port_id: str, is_output: bool) -> None:
        scene = self._scene
        if scene is None:
            return
        if is_output:
            scene.graph.remove_output(node_id, port_id)
        else:
            scene.graph.remove_input(node_id, port_id)
        scene.sync_node(node_id)
        self._rebuild()

    # ------------------------------------------------------------ 输出配置

    def _build_outputs_tab(self, tabs: QTabWidget, node) -> None:
        scene = self._scene
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        headers = ["名称", "类型", "保存路径", "临时", "入工程", ""]
        for col, text in enumerate(headers):
            hdr = QLabel(text)
            hdr.setProperty("panelHeader", True)
            grid.addWidget(hdr, 0, col)

        for row, outs in enumerate(node.outputs, start=1):
            self._build_output_row(grid, row, node, outs)

        layout.addLayout(grid)
        layout.addStretch(1)

        add_btn = QPushButton("＋ 新增输出")
        add_btn.setObjectName("AddButton")
        add_btn.setToolTip("给当前节点新增一个输出端口")
        add_btn.clicked.connect(
            lambda: self._on_add_output(node.id) if scene else None
        )
        layout.addWidget(add_btn)

        tabs.addTab(page, "输出配置")

    def _build_output_row(self, grid: QGridLayout, row: int, node, outs) -> None:
        node_id = node.id

        name_edit = QLineEdit(outs.name)
        name_edit.editingFinished.connect(
            lambda: self._rename_output(node_id, outs.id, name_edit.text().strip())
        )
        grid.addWidget(name_edit, row, 0)

        type_combo = self._make_type_combo(outs.type, node_id, outs.id, is_output=True)
        grid.addWidget(type_combo, row, 1)

        path_edit = QLineEdit(outs.save_path)
        path_edit.setPlaceholderText("输出文件路径（可选）")
        path_edit.editingFinished.connect(
            lambda: setattr(outs, "save_path", path_edit.text())
        )
        browse_btn = QToolButton()
        browse_btn.setText("…")
        browse_btn.setToolTip("浏览输出文件路径")
        browse_btn.clicked.connect(
            lambda: self._browse_output_path(outs, path_edit)
        )
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(path_edit, 1)
        path_row.addWidget(browse_btn)
        path_widget = QWidget()
        path_widget.setLayout(path_row)
        grid.addWidget(path_widget, row, 2)

        temp_cb = QCheckBox()
        temp_cb.setChecked(outs.write_temp)
        temp_cb.setToolTip("写入临时图层（内存，不落盘不入工程）")
        temp_cb.toggled.connect(lambda chk: setattr(outs, "write_temp", chk))
        grid.addWidget(temp_cb, row, 3)

        load_cb = QCheckBox()
        load_cb.setChecked(outs.load_to_canvas)
        load_cb.setToolTip("把结果加载到 QGIS 图层面板")
        load_cb.toggled.connect(lambda chk: setattr(outs, "load_to_canvas", chk))
        grid.addWidget(load_cb, row, 4)

        del_btn = QToolButton()
        del_btn.setText("✕")
        del_btn.setObjectName("RowDel")
        del_btn.setToolTip("删除此输出")
        del_btn.clicked.connect(
            lambda: self._on_remove_port(node_id, outs.id, is_output=True)
        )
        grid.addWidget(del_btn, row, 5)

    def _browse_output_path(self, outs, path_edit: QLineEdit) -> None:
        scene = self._scene
        if scene is None:
            return
        default_dir = path_edit.text() or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "选择输出文件", default_dir,
            "矢量/栅格文件 (*.gpkg *.shp *.geojson *.tif);;所有文件 (*)",
        )
        if path:
            outs.save_path = path
            path_edit.setText(path)

    def _rename_output(self, node_id: str, port_id: str, new_name: str) -> None:
        scene = self._scene
        if scene is None or not new_name:
            return
        outs = scene.graph.output_port(node_id, port_id)
        if outs is not None and outs.name != new_name:
            outs.name = new_name
            scene.sync_node(node_id)

    def _on_add_output(self, node_id: str) -> None:
        scene = self._scene
        if scene is None:
            return
        scene.graph.add_output(node_id, "输出", PortType.STRING)
        scene.sync_node(node_id)
        self._rebuild()

    # ------------------------------------------------------------ 端口类型

    def _make_type_combo(self, current: PortType, node_id: str, port_id: str, is_output: bool):
        combo = QComboBox()
        for pt in PortType:
            combo.addItem(pt.value, pt.name)
        combo.setCurrentIndex(combo.findData(current.name))
        combo.activated.connect(
            lambda idx: self._on_port_type_changed(
                node_id, port_id, combo.itemData(idx), is_output
            )
        )
        return combo

    def _on_port_type_changed(self, node_id: str, port_id: str, type_name: str, is_output: bool) -> None:
        scene = self._scene
        if scene is None or self._building or not type_name:
            return
        try:
            new_type = parse_port_type(type_name)
        except ValueError:  # noqa: BLE001
            return
        graph = scene.graph
        if is_output:
            outs = graph.output_port(node_id, port_id)
            if outs is None:
                return
            outs.type = new_type
            # 以该端口为源的边若不再兼容 → 删除并提示
            for e in list(graph.edges.values()):
                if e.src_node != node_id or e.src_port != port_id:
                    continue
                dst_in = graph.input_port(e.dst_node, e.dst_port)
                if dst_in is not None and not can_connect(new_type, dst_in.type):
                    graph.remove_edge(e.id)
                    scene.status_message.emit(f"输出端口类型改变，连线 {e.id} 已断开")
        else:
            ins = graph.input_port(node_id, port_id)
            if ins is None:
                return
            ins.type = new_type
            # 连入该端口的边若不再兼容 → 删除并提示
            edge = graph.edge_into(node_id, port_id)
            if edge is not None:
                src_out = graph.output_port(edge.src_node, edge.src_port)
                if src_out is not None and not can_connect(src_out.type, new_type):
                    graph.remove_edge(edge.id)
                    scene.status_message.emit(f"输入端口类型改变，连线 {edge.id} 已断开")
        scene.sync_node(node_id)

    # ------------------------------------------------------------ 执行逻辑

    def _on_code_changed(self, node, text: str) -> None:
        """代码变化写回模型并标记脏（重建面板期间不触发）。"""
        if self._building:
            return
        node.code = text
        scene = self._scene
        if scene is not None:
            scene.graph_changed.emit()

    def _build_code_tab(self, tabs: QTabWidget, node) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        code_edit = QPlainTextEdit()
        code_edit.setPlainText(node.code or "")
        code_edit.setPlaceholderText(
            "# 输入 PyQGIS 代码：\n# 引用 inputs 字典、给 outputs 字典赋值"
        )
        font = code_edit.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        code_edit.setFont(font)
        code_edit.textChanged.connect(
            lambda: self._on_code_changed(node, code_edit.toPlainText())
        )
        layout.addWidget(code_edit, 1)

        run_btn = QPushButton("单节点运行")
        run_btn.clicked.connect(lambda: self.run_node_requested.emit(node.id))
        layout.addWidget(run_btn)

        tabs.addTab(page, "执行逻辑")
