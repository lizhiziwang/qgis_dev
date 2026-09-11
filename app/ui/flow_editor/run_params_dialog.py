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

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.domain.flow.model import InputSpec, NodeSpec, unbound_inputs
from app.core.domain.flow.types import PortType
from app.ui.common.base_dialog import BaseDialog

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
        self.setMinimumWidth(520)
        #: (node_id, input_id, InputSpec, 控件)
        self._rows: list[tuple[str, str, InputSpec, QCheckBox | QLineEdit]] = []
        self._build(graph)

    # ------------------------------------------------------------ 构建

    def _build(self, graph) -> None:
        root = QVBoxLayout()

        hint = QLabel("以下输入未绑定上游，请填写本次运行使用的值（带 * 为必填）：")
        hint.setWordWrap(True)
        root.addWidget(hint)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)

        last_node: str | None = None
        for node_id, node_name, ins in unbound_inputs(graph):
            if node_id != last_node:
                hdr = QLabel(f"▎{node_name}")
                hdr.setStyleSheet("font-weight: bold;")
                v.addWidget(hdr)
                last_node = node_id
            self._add_row(v, graph.nodes.get(node_id), ins)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        run_btn = QPushButton("运行")
        run_btn.setDefault(True)
        run_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(run_btn)
        btns.addWidget(cancel_btn)
        root.addLayout(btns)
        self.setLayout(root)

    def _add_row(self, layout: QVBoxLayout, node: NodeSpec | None, ins: InputSpec) -> None:
        star = " *" if ins.required else ""
        label = QLabel(f"{ins.name}（{ins.type.value}）{star}")
        label.setMinimumWidth(160)

        choices = _layer_choices(node, ins)
        if ins.type is PortType.BOOLEAN:
            widget: QCheckBox | QLineEdit | QComboBox = QCheckBox()
            widget.setChecked(str(ins.default).strip().lower() in ("1", "true", "是", "yes"))
            widget.setToolTip("勾选 = true，不勾选 = false")
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(widget)
        elif choices:
            widget = self._make_layer_combo(ins, choices)
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(widget, 1)
        else:
            widget = QLineEdit(ins.default)
            widget.setPlaceholderText(ins.default or "（未设置）")
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(widget, 1)
            if ins.type in _FILE_FILTERS:
                # clicked 信号会传 bool 参数，用 _ 吞掉避免误绑到 edit
                browse = self._make_browse_button(
                    lambda _=False, w=widget, t=ins.type: self._browse_file(w, t)
                )
                row.addWidget(browse)
            elif ins.type is PortType.FOLDER:
                browse = self._make_browse_button(
                    lambda _=False, w=widget: self._browse_folder(w)
                )
                row.addWidget(browse)

        self._rows.append((node.id if node is not None else "", ins.id, ins, widget))
        layout.addLayout(row)

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
            QMessageBox.warning(self, "参数不完整", problem)
            return
        super().accept()

    def _validate(self) -> str | None:
        for _node_id, _port_id, ins, widget in self._rows:
            if isinstance(widget, QCheckBox):
                continue
            text = _row_text(widget).strip()
            if ins.required and not text:
                return f"「{ins.name}」是必填参数，不能为空。"
            if text and ins.type is PortType.NUMBER:
                try:
                    float(text)
                except ValueError:
                    return f"「{ins.name}」需要填数值，当前输入：{text}"
        return None

    def overrides(self) -> dict[str, dict[str, str]]:
        """本次运行覆盖值 ``{node_id: {input_id: 原始字符串}}``。"""
        out: dict[str, dict[str, str]] = {}
        for node_id, port_id, _ins, widget in self._rows:
            out.setdefault(node_id, {})[port_id] = _row_text(widget)
        return out
