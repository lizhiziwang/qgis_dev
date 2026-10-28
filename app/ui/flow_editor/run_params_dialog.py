"""流程编排：运行前参数填写对话框。

运行整条流程前，把所有「未绑定上游」的输入参数列出来，让用户填写 / 覆盖
本次运行的值，返回 ``{node_id: {input_id: 原始字符串}}``。

覆盖值只影响本次运行（执行器临时覆盖默认值），**不写回图模型、不持久化到
JSON**；下次运行仍用节点里保存的默认值 / 绑定。

图层类输入用**可编辑下拉框**承载：既可从工程图层树里选一个已加载的图层
（填入图层名，执行时按名回查还原成图层对象，内存图层也能用），也可以照旧
直接填 / 浏览文件路径。覆盖范围：

- ``VECTOR`` / ``RASTER`` 类型的输入（如「缓冲区」的「输入图层」）；
- 节点输出里有矢量 / 栅格端口的 ``FILE`` 输入（「加载矢量 / 加载栅格」的
  「图层路径」）。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.flow.model import InputSpec, NodeSpec, unbound_inputs
from app.core.domain.flow.topo import FlowCycleError, topo_sort
from app.core.domain.flow.types import PortType, validate_value
from app.ui.common.base_dialog import BaseDialog, make_scrollable
from app.ui.common.message_box import MessageBox

#: 文件类端口 → 浏览对话框文件过滤器
_FILE_FILTERS: dict[PortType, str] = {
    PortType.VECTOR: "矢量文件 (*.shp *.gpkg *.geojson *.gdb);;所有文件 (*)",
    PortType.RASTER: "栅格文件 (*.tif *.img);;所有文件 (*)",
    PortType.FILE: "所有文件 (*)",
    PortType.TABLE: "所有文件 (*)",
}


def _layer_kinds(node: NodeSpec | None, ins: InputSpec) -> tuple[bool, bool]:
    """判断某个输入该列哪些图层：``(要矢量, 要栅格)``。

    输入本身是 ``VECTOR`` / ``RASTER`` 时按输入类型列（如「缓冲区」的
    「输入图层」，运行时可直接选工程里已有的图层）；``FILE`` 这类没有类型
    信息的路径输入则看节点的输出端口（数据源节点：加载矢量的「图层路径」
    列矢量，加载栅格的列栅格）。数值 / 字符串等非图层输入不列。
    """
    if ins.type is PortType.VECTOR:
        return True, False
    if ins.type is PortType.RASTER:
        return False, True
    if ins.type is not PortType.FILE or node is None:
        return False, False
    return (
        any(outs.type is PortType.VECTOR for outs in node.outputs),
        any(outs.type is PortType.RASTER for outs in node.outputs),
    )


def _layer_choices(node: NodeSpec | None, ins: InputSpec) -> list[tuple[str, str]]:
    """列出工程图层树里与输入匹配的图层：``[(显示文本, 图层名)]``。

    顺序即图层树顺序；分层图层的显示文本带 ``[分组 / 子分组]`` 前缀，便于
    同名或深层图层区分，但**实际填入的值始终是纯图层名**（执行器按
    :meth:`QgsProject.mapLayersByName` 回查；同名多份时取第一个）。

    该输入没有矢量 / 栅格语义时返回空列表——那种 ``FILE`` 输入按普通文件
    路径处理，本对话框不给图层下拉。
    """
    wants_vector, wants_raster = _layer_kinds(node, ins)
    if not (wants_vector or wants_raster):
        return []

    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer  # type: ignore

    root = QgsProject.instance().layerTreeRoot()
    choices: list[tuple[str, str]] = []
    for tree_layer in root.findLayers():
        layer = tree_layer.layer()
        if isinstance(layer, QgsVectorLayer):
            if not wants_vector:
                continue
        elif isinstance(layer, QgsRasterLayer):
            if not wants_raster:
                continue
        else:
            continue  # 网格 / 矢量瓦片 / 点云等：源节点模板处理不了，不列
        groups: list[str] = []
        parent = tree_layer.parent()
        while parent is not None and parent is not root:
            groups.append(parent.name())
            parent = parent.parent()
        prefix = " / ".join(reversed(groups))
        choices.append((f"[{prefix}] {layer.name()}" if prefix else layer.name(), layer.name()))
    return choices


def _row_text(widget: QCheckBox | QLineEdit | QComboBox) -> str:
    """取一行的原始字符串值（复选框 → ``"1"`` / ``"0"``）。"""
    if isinstance(widget, QCheckBox):
        return "1" if widget.isChecked() else "0"
    if isinstance(widget, QComboBox):
        return widget.currentText()
    return widget.text()


class RunParamsDialog(BaseDialog):
    """运行前参数填写对话框。"""

    def __init__(self, graph, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("填写本次运行的输入参数")
        # 参数少时不必撑到基类的 800x600，按内容自适应高度
        self.setMinimumSize(560, 0)
        self.resize(640, 360)
        #: (node_id, input_id, InputSpec, 控件)
        self._rows: list[tuple[str, str, InputSpec, QCheckBox | QLineEdit | QComboBox]] = []
        self._build(graph)

    def _max_window_height(self) -> int | None:
        """参数多时也别铺满屏幕：在基类上限基础上再收 30%。"""
        base = super()._max_window_height()
        return None if base is None else max(360, int(base * 0.7))

    # ------------------------------------------------------------ 构建

    def _build(self, graph) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        hint = QLabel("以下输入未绑定上游，请填写本次运行使用的值（带 * 为必填）：")
        hint.setWordWrap(True)
        v.addWidget(hint)

        # 按拓扑序（连线确定的执行顺序）排列工具，而不是拖入画布的顺序。
        # 完全没连线的孤立节点不算进主流程，统一排到最后。
        # 有环时退回图的插入顺序，把环的错误留给运行阶段统一提示。
        rows = unbound_inputs(graph)
        connected = {e.src_node for e in graph.edges.values()} | {
            e.dst_node for e in graph.edges.values()
        }
        try:
            order = {nid: i for i, nid in enumerate(topo_sort(graph))}
        except FlowCycleError:
            order = {}
        rows.sort(
            key=lambda row: (
                0 if row[0] in connected else 1,
                order.get(row[0], len(order)),
            )
        )

        last_node: str | None = None
        form: QFormLayout | None = None
        for node_id, node_name, ins in rows:
            if node_id != last_node:
                # 每个工具（节点）一个分组卡片，参数用表单两列排布
                group = QGroupBox(node_name)
                form = QFormLayout(group)
                form.setContentsMargins(12, 16, 12, 12)
                form.setHorizontalSpacing(14)
                form.setVerticalSpacing(8)
                form.setLabelAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                form.setFieldGrowthPolicy(
                    QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
                )
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
                v.addWidget(group)
                last_node = node_id
            if form is not None:
                self._add_param_row(form, graph.nodes.get(node_id), ins)

        v.addStretch(1)

        root.addWidget(make_scrollable(container), 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        run_btn = QPushButton("运行")
        run_btn.setObjectName("DialogPrimary")
        run_btn.setDefault(True)
        run_btn.clicked.connect(self.accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(run_btn)
        root.addLayout(btns)
        self.setLayout(root)
        self.fit_height_to_content(container, extra=8)

    def _add_param_row(
        self, form: QFormLayout, node: NodeSpec | None, ins: InputSpec
    ) -> None:
        value_widget = self._make_value_widget(node, ins)
        field: QWidget = value_widget
        if isinstance(value_widget, QLineEdit):
            if ins.type in _FILE_FILTERS:
                # clicked 信号会传 bool 参数，用 _ 吞掉避免误绑到 edit
                field = self._wrap_with_browse(
                    value_widget,
                    lambda _=False, w=value_widget, t=ins.type: self._browse_file(w, t),
                )
            elif ins.type is PortType.FOLDER:
                field = self._wrap_with_browse(
                    value_widget,
                    lambda _=False, w=value_widget: self._browse_folder(w),
                )
        star = " *" if ins.required else ""
        label = QLabel(f"{ins.name}（{ins.type.value}）{star}")
        label.setMinimumWidth(150)
        form.addRow(label, field)
        self._rows.append(
            (node.id if node is not None else "", ins.id, ins, value_widget)
        )

    def _make_value_widget(
        self, node: NodeSpec | None, ins: InputSpec
    ) -> QCheckBox | QLineEdit | QComboBox:
        if ins.type is PortType.BOOLEAN:
            widget = QCheckBox()
            widget.setChecked(
                str(ins.default).strip().lower() in ("1", "true", "是", "yes")
            )
            widget.setToolTip("勾选 = true，不勾选 = false")
            return widget
        if ins.choices and ins.type is PortType.STRING:
            return self._make_choices_combo(ins)
        choices = _layer_choices(node, ins)
        if choices:
            return self._make_layer_combo(ins, choices)
        edit = QLineEdit(ins.default)
        edit.setPlaceholderText(ins.default or "（未设置）")
        return edit

    def _wrap_with_browse(self, edit: QLineEdit, slot) -> QWidget:
        field = QWidget()
        h = QHBoxLayout(field)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(edit, 1)
        h.addWidget(self._make_browse_button(slot))
        return field

    def _make_choices_combo(self, ins: InputSpec) -> QComboBox:
        """固定选项下拉框（如「距离单位」的 米 / 千米 / 度）。"""
        combo = QComboBox()
        for choice in ins.choices:
            combo.addItem(choice, choice)
        idx = combo.findData(ins.default)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        return combo

    def _make_layer_combo(self, ins: InputSpec, choices: list[tuple[str, str]]) -> QComboBox:
        """工程图层下拉框（可编辑）：选图层名，或照旧直接填文件路径。"""
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMinimumContentsLength(24)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for text, value in choices:
            combo.addItem(text, value)
        # 先摆好当前默认值、再连信号：addItem 会让第 0 项成为当前项并触发信号，
        # 若先连信号，默认值会被第一项冲掉。
        combo.setCurrentIndex(-1)
        combo.setEditText(ins.default)
        combo.currentIndexChanged.connect(
            lambda idx, c=combo: c.setEditText(c.itemData(idx)) if idx >= 0 else None
        )
        combo.setToolTip(
            "可直接填文件路径，或从下拉里选工程中已加载的图层（填入图层名，"
            "源节点会直接复用该图层，内存图层也支持）"
        )
        return combo

    def _make_browse_button(self, slot) -> QPushButton:
        btn = QPushButton("…")
        btn.setFixedWidth(32)
        btn.setToolTip("浏览选择")
        btn.clicked.connect(slot)
        return btn

    def _browse_file(self, edit: QLineEdit, ptype: PortType) -> None:
        flt = _FILE_FILTERS.get(ptype, "所有文件 (*)")
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", edit.text() or "", flt)
        if path:
            edit.setText(path)

    def _browse_folder(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text() or "")
        if path:
            edit.setText(path)

    # ------------------------------------------------------------ 校验 / 取值

    def accept(self) -> None:  # noqa: N802 —— Qt 命名
        problem = self._validate()
        if problem is not None:
            MessageBox.warning(self, "参数不完整", problem)
            return
        super().accept()

    def _validate(self) -> str | None:
        for _node_id, _port_id, ins, widget in self._rows:
            if isinstance(widget, QCheckBox):
                continue
            text = _row_text(widget).strip()
            if ins.required and not text:
                return f"「{ins.name}」是必填参数，不能为空。"
            problem = validate_value(text, ins.type)
            if problem is not None:
                return f"「{ins.name}」{problem}，当前输入：{text}"
        return None

    def overrides(self) -> dict[str, dict[str, str]]:
        """本次运行覆盖值 ``{node_id: {input_id: 原始字符串}}``。"""
        out: dict[str, dict[str, str]] = {}
        for node_id, port_id, _ins, widget in self._rows:
            out.setdefault(node_id, {})[port_id] = _row_text(widget)
        return out
